#!/usr/bin/env python3
"""Auth propagation across windows.

After logging in once in window 1, a freshly-opened window 2 should land
straight at the prompt (no login screen). The SessionStart hook saves
auth fields into claude-auth.json, which setup_session_config picks up
when a new window's session config is built.

Uses fake_claude.py so no real OAuth is needed — typing "login" in the
fake login screen flips hasCompletedOnboarding etc. on .claude.json.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pexpect

sys.path.insert(0, str(Path(__file__).parent))
from harness import TestRunner, assert_true

UID = os.getuid()
FAKE_CLAUDE = Path(__file__).parent.parent / "live_tests" / "fake_claude.py"


def wait_for(condition, timeout=30, poll=0.5, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
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
    out = tmux(socket, "list-windows", "-F", "#{window_index}", check=False)
    return [l for l in out.splitlines() if l]


def run_test():
    runner = TestRunner()

    def test_login_propagates_to_new_window():
        profile = f"fake-auth-{int(time.time())}-{os.getpid()}"
        socket = f"clauthing-{profile}"
        profile_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
        clauthing_bin = shutil.which("clauthing") or "clauthing"
        child = None

        try:
            assert_true(FAKE_CLAUDE.exists(), f"fake_claude.py not found at {FAKE_CLAUDE}")

            # Point this profile at the fake claude binary BEFORE launch.
            subprocess.run(
                [clauthing_bin, "--profile", profile, "--set-claude", str(FAKE_CLAUDE)],
                check=True, capture_output=True, timeout=10,
            )

            # Sanity: claude-auth.json shouldn't exist yet (fresh profile).
            auth_file = profile_dir / "claude-auth.json"
            assert_true(not auth_file.exists(),
                       f"profile should be fresh, but {auth_file} exists")

            # Launch via pexpect — tmux needs a real pty.
            child = pexpect.spawn(
                clauthing_bin,
                ["--no-kitty", "--profile", profile],
                encoding="utf-8",
                timeout=120,
                dimensions=(50, 200),
            )
            child.logfile_read = sys.stderr

            print(f"  [auth] waiting for tmux session ({socket})...", flush=True)
            wait_for(lambda: subprocess.run(
                ["tmux", "-L", socket, "has-session"],
                capture_output=True).returncode == 0,
                timeout=15, label="tmux session")
            time.sleep(2)

            # Window 1 should be showing fake login screen
            print("  [auth] waiting for FAKE_LOGIN_SCREEN...", flush=True)
            try:
                wait_for(lambda: "FAKE_LOGIN_SCREEN" in capture_pane(socket),
                         timeout=15, label="login screen visible")
            except TimeoutError:
                pane = capture_pane(socket)
                print(f"  [auth] TIMEOUT, pane content:\n{pane!r}", flush=True)
                # also check if claude config exists & what binary is being used
                cfg_file = profile_dir / "config.json"
                if cfg_file.exists():
                    print(f"  [auth] config.json: {cfg_file.read_text()}", flush=True)
                raise
            print("  [auth] login screen visible — sending 'login'", flush=True)

            send_keys(socket, "login", literal=True)
            send_keys(socket, "Enter")

            # Wait for FAKE_READY (post-login)
            print("  [auth] waiting for FAKE_READY after login...", flush=True)
            wait_for(lambda: "FAKE_READY" in capture_pane(socket),
                     timeout=15, label="ready after login")
            time.sleep(1)

            # Send a marker prompt so SessionStart and Stop both fire
            print("  [auth] sending marker prompt...", flush=True)
            send_keys(socket, "hello-from-test", literal=True)
            send_keys(socket, "Enter")
            wait_for(lambda: "FAKE_RESPONSE: hello-from-test" in capture_pane(socket),
                     timeout=15, label="marker response")
            time.sleep(1)

            # claude-auth.json should now exist (SessionStart hook fired)
            print(f"  [auth] checking {auth_file}...", flush=True)
            assert_true(auth_file.exists(),
                       f"claude-auth.json should be saved after SessionStart")
            import json
            saved = json.loads(auth_file.read_text())
            assert_true(saved.get("hasCompletedOnboarding") is True,
                       f"claude-auth.json missing hasCompletedOnboarding: {saved}")
            assert_true("oauthAccount" in saved,
                       f"claude-auth.json missing oauthAccount: {saved}")
            print("  [auth] claude-auth.json populated correctly", flush=True)

            # Open a 2nd window with the same default-command
            jail_dir = f"/tmp/clauthing-{UID}"
            print("  [auth] opening 2nd window...", flush=True)
            tmux(socket, "new-window", "-c", jail_dir,
                 f"{clauthing_bin} --profile {profile} --new-window")
            wait_for(lambda: len(list_windows(socket)) >= 2, timeout=10,
                     label="2nd window")
            time.sleep(2)

            # Find the new window's index and capture its pane
            wins = list_windows(socket)
            new_idx = max(int(w) for w in wins)
            print(f"  [auth] new window index={new_idx}; waiting for FAKE_READY...", flush=True)
            wait_for(lambda: "FAKE_READY" in capture_pane(socket, target=str(new_idx)),
                     timeout=20, label="ready in new window")

            # CRITICAL: it must NOT have shown the login screen
            pane2 = capture_pane(socket, target=str(new_idx))
            assert_true("FAKE_LOGIN_SCREEN" not in pane2,
                       f"new window should skip login. Pane:\n{pane2}")
            print("  [auth] new window logged in directly — no LOGIN screen", flush=True)

        finally:
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

    runner.run_test("login_propagates_to_new_window", test_login_propagates_to_new_window)
    return runner.summary()


if __name__ == "__main__":
    print("Fake-auth Tests")
    print("=" * 50)
    sys.exit(run_test())
