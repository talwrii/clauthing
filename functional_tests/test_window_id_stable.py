#!/usr/bin/env python3
"""@clauthing_window must stay stable when a window is respawned (:reload).

Regression for the churn bug: after a respawn, new_window re-derived the window
id from session metadata and overwrote @clauthing_window. If the resumed
session's metadata was missing clauthing_window (e.g. after an odd session
copy), it minted a FRESH id — so the window's identity changed across :login /
:reload, orphaning its messages/notes.

Fix: the window owns the id — new_window reuses the existing @clauthing_window
window option. This test reproduces the mismatch (delete clauthing_window from
the session metadata while the window option is set), runs :reload, and asserts
the id is unchanged.
"""

import json
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

    def test_clauthing_window_stable_across_reload():
        profile = f"winid-{int(time.time())}-{os.getpid()}"
        socket = f"clauthing-{profile}"
        profile_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
        sessions_dir = Path.home() / ".local" / "state" / "clauthing" / "sessions"
        clauthing_bin = shutil.which("clauthing") or "clauthing"
        child = None
        touched_meta = None

        def cw():
            return tmux(socket, "display-message", "-p", "#{@clauthing_window}", check=False)

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

            cw_before = wait_for(lambda: cw() or None, timeout=10, label="@clauthing_window set")
            sid = tmux(socket, "display-message", "-p", "#{@session_id}")
            print(f"\n  cw_before={cw_before}  sid={sid}", flush=True)
            assert_true(bool(cw_before), "window should have a clauthing_window")

            # Reproduce the mismatch: strip clauthing_window from session metadata
            # while the window option keeps it. Pre-fix, :reload would mint fresh.
            meta_file = sessions_dir / f"{sid}.json"
            touched_meta = meta_file
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
                meta.pop("clauthing_window", None)
                meta_file.write_text(json.dumps(meta, indent=2))

            # :reload respawns claude in the SAME tmux window.
            send_keys(socket, ":reload", literal=True)
            send_keys(socket, "Enter")
            time.sleep(2)
            wait_for(lambda: "FAKE_READY" in capture_pane(socket), timeout=20,
                     label="ready after reload")

            cw_after = wait_for(lambda: cw() or None, timeout=10,
                                label="@clauthing_window after reload")
            print(f"  cw_after={cw_after}", flush=True)
            assert_true(cw_after == cw_before,
                        f"clauthing_window must be stable across :reload. "
                        f"before={cw_before} after={cw_after}")

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
            if touched_meta:
                try:
                    touched_meta.unlink(missing_ok=True)
                except Exception:
                    pass

    runner.run_test("clauthing_window_stable_across_reload",
                    test_clauthing_window_stable_across_reload)
    return runner.summary()


if __name__ == "__main__":
    print("clauthing window-id stability Tests")
    print("=" * 50)
    sys.exit(run_test())
