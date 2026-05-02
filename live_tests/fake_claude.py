#!/usr/bin/env python3
"""Minimal fake `claude` binary for testing clauthing without real OAuth.

Honors --session-id UUID and --resume UUID. Checks $CLAUDE_CONFIG_DIR/.claude.json
for auth state. Prints LOGIN_SCREEN / READY markers so tests can detect what
the user would see. Fires the hooks configured in $CLAUDE_CONFIG_DIR/settings.json
so clauthing's UserPromptSubmit / SessionStart / Stop machinery runs.

Designed to be set via `clauthing --set-claude /path/to/fake_claude.py`.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path


def parse_args(argv):
    session_id = None
    mode = "fresh"
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--session-id" and i + 1 < len(argv):
            session_id = argv[i + 1]
            mode = "session-id"
            i += 2
        elif a == "--resume" and i + 1 < len(argv):
            session_id = argv[i + 1]
            mode = "resume"
            i += 2
        else:
            i += 1
    return session_id or str(uuid.uuid4()), mode


def fire_hook(hooks_cfg, name, data):
    """Run hook commands and return False if any returned continue=false."""
    for entry in hooks_cfg.get(name, []):
        for h in entry.get("hooks", []):
            if h.get("type") != "command":
                continue
            try:
                proc = subprocess.run(
                    shlex.split(h["command"]),
                    input=json.dumps(data),
                    capture_output=True, text=True, timeout=15,
                )
            except Exception as e:
                print(f"FAKE_HOOK_ERROR: {name}: {e}", file=sys.stderr)
                continue
            out = (proc.stdout or "").strip()
            if not out:
                continue
            try:
                # Hook output may have extra lines — last JSON object wins
                last = out.splitlines()[-1]
                parsed = json.loads(last)
                if parsed.get("continue") is False:
                    return False, parsed.get("stopReason", "")
            except json.JSONDecodeError:
                pass
    return True, ""


def main():
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", ""))
    if not config_dir or not config_dir.exists():
        print("FAKE_ERROR: CLAUDE_CONFIG_DIR not set or missing", file=sys.stderr)
        sys.exit(1)

    session_id, mode = parse_args(sys.argv[1:])

    # Read auth state
    claude_json_path = config_dir / ".claude.json"
    claude_json = {}
    if claude_json_path.exists():
        try:
            claude_json = json.loads(claude_json_path.read_text())
        except json.JSONDecodeError:
            pass
    logged_in = bool(claude_json.get("hasCompletedOnboarding")) and "oauthAccount" in claude_json

    # Read hook config
    settings_path = config_dir / "settings.json"
    hooks_cfg = {}
    if settings_path.exists():
        try:
            hooks_cfg = json.loads(settings_path.read_text()).get("hooks", {})
        except json.JSONDecodeError:
            pass

    cwd = os.getcwd()

    if not logged_in:
        print("FAKE_LOGIN_SCREEN", flush=True)
        print("type 'login' and press Enter to complete OAuth:", flush=True)
        line = sys.stdin.readline()
        if not line:
            return
        if line.strip() != "login":
            print("FAKE_LOGIN_CANCELLED", flush=True)
            return
        claude_json.update({
            "hasCompletedOnboarding": True,
            "lastOnboardingVersion": "1.0",
            "oauthAccount": {
                "accountUuid": "fake-acct-uuid",
                "emailAddress": "fake@test.local",
            },
            "userID": "fake-user-id",
        })
        claude_json_path.write_text(json.dumps(claude_json, indent=2))
        # Also write a fake .credentials.json so propagate_credentials has something
        creds = config_dir / ".credentials.json"
        if not creds.exists() or creds.is_symlink():
            try:
                if creds.is_symlink():
                    creds.unlink()
                creds.write_text(json.dumps({
                    "claudeAiOauth": {
                        "accessToken": "fake-token",
                        "refreshToken": "fake-refresh",
                        "expiresAt": 9999999999000,
                    }
                }))
                creds.chmod(0o600)
            except Exception:
                pass
        print("FAKE_LOGIN_DONE", flush=True)

    # Fire SessionStart hook
    fire_hook(hooks_cfg, "SessionStart", {"session_id": session_id, "cwd": cwd})

    print("FAKE_READY", flush=True)

    encoded_cwd = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
    proj_dir = config_dir / "projects" / encoded_cwd
    proj_dir.mkdir(parents=True, exist_ok=True)
    session_file = proj_dir / f"{session_id}.jsonl"

    while True:
        sys.stdout.write("❯ ")  # ❯
        sys.stdout.flush()
        try:
            line = sys.stdin.readline()
        except (KeyboardInterrupt, EOFError):
            break
        if not line:
            break
        prompt = line.rstrip("\n")
        if not prompt:
            continue

        cont, _stop_reason = fire_hook(hooks_cfg, "UserPromptSubmit", {
            "session_id": session_id, "cwd": cwd, "prompt": prompt,
        })
        if not cont:
            continue

        # Append user + assistant turns to session file
        with open(session_file, "a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": prompt}}) + "\n")
            f.write(json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": f"echo: {prompt}"}]},
            }) + "\n")

        print(f"FAKE_RESPONSE: {prompt}", flush=True)
        fire_hook(hooks_cfg, "Stop", {"session_id": session_id})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
