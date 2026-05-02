#!/usr/bin/env python3
"""Test :resume behaviour in both --one-tab and multi-tab modes.

Uses fake_claude.py so we don't need real OAuth. The test:
1. Launches clauthing with the fake binary
2. Logs in, creates session A (the active session)
3. Manually plants a second resumable session B on disk
4. Types `:resume <session_b_id>` from session A's window
5. Verifies the resume targeted session B (and not some other session)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pexpect

sys.path.insert(0, str(Path(__file__).parent))
from harness import TestRunner, assert_true

UID = os.getuid()
FAKE_CLAUDE = Path(__file__).parent.parent / "live_tests" / "fake_claude.py"


def wait_for(condition, timeout=30, poll=0.5, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(poll)
    raise TimeoutError(f"Timed out ({timeout}s) waiting for: {label}")


def tmux(socket, *args, check=True):
    r = subprocess.run(["tmux", "-L", socket, *args],
                       capture_output=True, text=True, timeout=10)
    if check and r.returncode != 0:
        raise RuntimeError(f"tmux {args} failed: {r.stderr!r}")
    return r.stdout.strip()


def send_keys(socket, keys, literal=False, target=None):
    cmd = ["tmux", "-L", socket, "send-keys"]
    if target:
        cmd.extend(["-t", target])
    if literal:
        cmd.append("-l")
    cmd.append(keys)
    subprocess.run(cmd, check=True, timeout=5)


def capture_pane(socket, target=None):
    cmd = ["tmux", "-L", socket, "capture-pane", "-p"]
    if target:
        cmd.extend(["-t", target])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return r.stdout


def list_windows(socket):
    out = tmux(socket, "list-windows",
               "-F", "#{window_index}:#{window_name}:#{@session_id}", check=False)
    return [l for l in out.splitlines() if l]


def find_one_tab_socket(profile, after_time, timeout=15):
    tmux_dir = Path(f"/tmp/tmux-{UID}")
    prefix = f"cl1-{profile}-"
    def check():
        if not tmux_dir.exists():
            return None
        for s in tmux_dir.iterdir():
            if s.name.startswith(prefix) and s.stat().st_ctime >= after_time - 1:
                return s.name
        return None
    return wait_for(check, timeout=timeout, label=f"tmux socket {prefix}*")


def plant_resumable_session(*, profile, name, cwd):
    """Drop a session metadata file and a session jsonl with a user message
    so :resume can find / use this session id. Returns the session id."""
    session_id = str(uuid.uuid4())

    # session metadata
    state_dir = Path.home() / ".local" / "state" / "clauthing" / "sessions"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{session_id}.json").write_text(json.dumps({
        "name": name,
        "path": cwd,
        "created": "2026-05-02T00:00:00+00:00",
        "has_messages": True,
    }, indent=2))

    # session file (jsonl) under the profile's claude-data/projects/<encoded>/
    if profile:
        base_config = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "clauthing"
    encoded = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
    proj_dir = base_config / "claude-data" / "projects" / encoded
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / f"{session_id}.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "previous-message"}}) + "\n"
    )
    return session_id


def cleanup_planted_session(session_id):
    state_file = Path.home() / ".local" / "state" / "clauthing" / "sessions" / f"{session_id}.json"
    state_file.unlink(missing_ok=True)


def run_test():
    runner = TestRunner()

    def _resume_test(*, mode, label):
        """Common :resume test. mode = 'one-tab' or 'multi-tab'."""
        profile = f"resume-{mode}-{int(time.time())}-{os.getpid()}"
        profile_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
        clauthing_bin = shutil.which("clauthing") or "clauthing"
        child = None
        socket = None
        planted_id = None

        try:
            assert_true(FAKE_CLAUDE.exists(), f"fake_claude.py missing")
            subprocess.run(
                [clauthing_bin, "--profile", profile, "--set-claude", str(FAKE_CLAUDE)],
                check=True, capture_output=True, timeout=10,
            )

            # Launch clauthing
            t0 = time.time()
            args = ["--no-kitty"]
            if mode == "one-tab":
                args.append("--one-tab")
            args.extend(["--profile", profile])
            child = pexpect.spawn(
                clauthing_bin, args,
                encoding="utf-8", timeout=120, dimensions=(50, 200),
            )
            child.logfile_read = sys.stderr

            # Find socket
            if mode == "one-tab":
                print(f"\n  [{label}] waiting for cl1- socket...", flush=True)
                socket = find_one_tab_socket(profile, after_time=t0, timeout=15)
            else:
                socket = f"clauthing-{profile}"
                print(f"\n  [{label}] waiting for tmux session ({socket})...", flush=True)
                wait_for(
                    lambda: subprocess.run(
                        ["tmux", "-L", socket, "has-session"], capture_output=True
                    ).returncode == 0,
                    timeout=15, label="tmux session"
                )
            time.sleep(2)

            # Login + create active session
            print(f"  [{label}] waiting for FAKE_LOGIN_SCREEN...", flush=True)
            wait_for(lambda: "FAKE_LOGIN_SCREEN" in capture_pane(socket),
                     timeout=15, label="login screen")
            send_keys(socket, "login", literal=True)
            send_keys(socket, "Enter")
            wait_for(lambda: "FAKE_READY" in capture_pane(socket),
                     timeout=15, label="ready after login")
            time.sleep(1)

            send_keys(socket, "hello", literal=True)
            send_keys(socket, "Enter")
            wait_for(lambda: "FAKE_RESPONSE: hello" in capture_pane(socket),
                     timeout=15, label="response to hello")
            time.sleep(1)

            # Plant a 2nd resumable session on disk (NOT in open-sessions yet)
            cwd = "/tmp/clauthing-1000"
            planted_id = plant_resumable_session(
                profile=profile, name="planted-name", cwd=cwd,
            )
            print(f"  [{label}] planted session {planted_id} (name='planted-name')", flush=True)

            before_windows = list_windows(socket)
            before_count = len(before_windows)
            # Capture the original @session_id of the active window so we can
            # verify :resume doesn't clobber it.
            orig_window_idx, orig_window_name, orig_session_id = (
                before_windows[0].split(":", 2)
            )
            print(f"  [{label}] windows before :resume: {before_windows}", flush=True)

            # ── :resume <planted_id> ─────────────────────────────────────────
            print(f"  [{label}] sending :resume {planted_id}", flush=True)
            send_keys(socket, f":resume {planted_id}", literal=True)
            send_keys(socket, "Enter")
            time.sleep(3)

            # Find the resumed window: it should have @session_id == planted_id
            def planted_window_present():
                for line in list_windows(socket):
                    parts = line.split(":", 2)
                    if len(parts) == 3 and parts[2] == planted_id:
                        return parts
                return None
            try:
                target = wait_for(planted_window_present, timeout=15, poll=0.5,
                                  label=f"window with @session_id={planted_id}")
            except TimeoutError:
                after = list_windows(socket)
                raise AssertionError(
                    f":resume should attach @session_id={planted_id} to a window. "
                    f"Got windows: {after}"
                )

            print(f"  [{label}] target window: {target}", flush=True)
            assert_true(target[1] == "planted-name",
                       f"resumed window should be named 'planted-name'. Got: {target}")

            after_windows = list_windows(socket)
            print(f"  [{label}] windows after :resume: {after_windows}", flush=True)

            if mode == "multi-tab":
                # Multi-tab: :resume should open a NEW window, leaving original intact
                assert_true(len(after_windows) > before_count,
                           f"multi-tab :resume should open a new window. "
                           f"before={before_count}, after={len(after_windows)}")
                orig_after = next(
                    (w for w in after_windows if w.split(":", 1)[0] == orig_window_idx),
                    None,
                )
                assert_true(orig_after is not None,
                           f"original window {orig_window_idx} should still exist. after={after_windows}")
                orig_after_session = orig_after.split(":", 2)[2]
                assert_true(orig_after_session == orig_session_id,
                           f"original window's @session_id should be unchanged. "
                           f"was={orig_session_id!r}, now={orig_after_session!r}")
            else:
                # One-tab: only one window allowed. :resume should boomerang-
                # replace the active session with the planted one.
                assert_true(len(after_windows) == 1,
                           f"one-tab :resume should keep window count at 1. "
                           f"got: {after_windows}")

        finally:
            if planted_id:
                cleanup_planted_session(planted_id)
            if socket:
                try:
                    subprocess.run(["tmux", "-L", socket, "kill-server"],
                                   capture_output=True, timeout=5)
                except Exception:
                    pass
            if child:
                try:
                    child.close(force=True)
                except Exception:
                    pass
            shutil.rmtree(profile_dir, ignore_errors=True)

    def test_resume_multi_tab():
        _resume_test(mode="multi-tab", label="resume-mt")

    def test_resume_one_tab():
        _resume_test(mode="one-tab", label="resume-1t")

    runner.run_test("resume_multi_tab", test_resume_multi_tab)
    runner.run_test("resume_one_tab", test_resume_one_tab)
    return runner.summary()


if __name__ == "__main__":
    print(":resume Tests")
    print("=" * 50)
    sys.exit(run_test())
