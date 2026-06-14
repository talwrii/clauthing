#!/usr/bin/env python3
"""A respawn must keep the window's @session_id — never mint a fresh one.

Regression for the "reload cleared my history" bug. When a window is respawned
WITHOUT the @startup_command boomerang carrying a SESSION_ID (a full restart, a
restore, or a mistargeted/global respawn), `clauthing --new-claude` fell through
to new_window(resume=None) and minted a BRAND-NEW empty session, overwriting the
window's @session_id. The window's real conversation was orphaned — history
"cleared" — and every subsequent reload resumed the empty session.

A genuinely fresh window (M-n / :clear) is created via `tmux new-window` and has
no @session_id, so it still mints. But a respawn-in-place keeps its @session_id
window option, and that must be reused.

This test logs in, has a turn (so the session holds a conversation), clears
@startup_command, then does a bare `respawn-pane -k` (what a restart does). It
asserts @session_id is unchanged afterwards. Pre-fix this FAILS (fresh uuid).
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


def wait_for(cond, timeout=30, poll=0.5, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = cond()
        if r:
            return r
        time.sleep(poll)
    raise TimeoutError(f"Timed out ({timeout}s) waiting for: {label}")


def tmux(socket, *args, check=True):
    r = subprocess.run(["tmux", "-L", socket, *args],
                       capture_output=True, text=True, timeout=10)
    if check and r.returncode != 0:
        raise RuntimeError(f"tmux {args} failed: {r.stderr!r}")
    return r.stdout.strip()


def send_keys(socket, keys, target=None, literal=False):
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
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout


def run_test():
    runner = TestRunner()

    def test_session_id_stable_across_respawn():
        profile = f"reload-sid-{int(time.time())}-{os.getpid()}"
        socket = f"clauthing-{profile}"
        profile_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
        clauthing_bin = shutil.which("clauthing") or "clauthing"
        child = None

        def sid():
            return tmux(socket, "display-message", "-p", "#{@session_id}", check=False)

        try:
            assert_true(FAKE_CLAUDE.exists(), "fake_claude.py missing")
            subprocess.run([clauthing_bin, "--profile", profile, "--set-claude", str(FAKE_CLAUDE)],
                           check=True, capture_output=True, timeout=10)

            child = pexpect.spawn(clauthing_bin, ["--no-kitty", "--profile", profile],
                                  encoding="utf-8", timeout=120, dimensions=(50, 200))
            child.logfile_read = sys.stderr
            wait_for(lambda: subprocess.run(["tmux", "-L", socket, "has-session"],
                                            capture_output=True).returncode == 0,
                     timeout=15, label="tmux session")
            time.sleep(2)
            wait_for(lambda: "FAKE_LOGIN_SCREEN" in capture_pane(socket), timeout=15,
                     label="login screen")
            send_keys(socket, "login", literal=True)
            send_keys(socket, "Enter")
            wait_for(lambda: "FAKE_READY" in capture_pane(socket), timeout=15,
                     label="ready after login")

            sid_before = wait_for(lambda: sid() or None, timeout=10, label="@session_id set")
            print(f"\n  sid_before={sid_before}", flush=True)
            assert_true(bool(sid_before), "window should have a @session_id")

            # Have a turn, so the window's session actually holds a conversation.
            send_keys(socket, "remember-this", literal=True)
            send_keys(socket, "Enter")
            wait_for(lambda: "FAKE_RESPONSE: remember-this" in capture_pane(socket),
                     timeout=15, label="assistant turn recorded")

            pane = tmux(socket, "display-message", "-p", "#{pane_id}")

            # Simulate the restart / mistargeted respawn: no @startup_command
            # boomerang, then a bare respawn-pane (reruns `clauthing --new-claude`).
            tmux(socket, "set-option", "-w", "-t", pane, "@startup_command", "")
            tmux(socket, "respawn-pane", "-k", "-t", pane)
            time.sleep(2)
            wait_for(lambda: "FAKE_READY" in capture_pane(socket), timeout=20,
                     label="ready after respawn")

            sid_after = wait_for(lambda: sid() or None, timeout=10,
                                 label="@session_id after respawn")
            print(f"  sid_after={sid_after}", flush=True)
            assert_true(sid_after == sid_before,
                        f"@session_id must survive a respawn (else history is "
                        f"orphaned). before={sid_before} after={sid_after}")

        finally:
            try:
                subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, timeout=5)
            except Exception:
                pass
            if child:
                try:
                    child.close(force=True)
                except Exception:
                    pass
            shutil.rmtree(profile_dir, ignore_errors=True)

    runner.run_test("session_id_stable_across_respawn",
                    test_session_id_stable_across_respawn)
    return runner.summary()


if __name__ == "__main__":
    print("clauthing reload/respawn session-stability Tests")
    print("=" * 50)
    sys.exit(run_test())
