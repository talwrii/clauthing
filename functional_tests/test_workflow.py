#!/usr/bin/env python3
"""Test clauthing-workflow external CLI and --launch-workflow integration.

Uses fake_claude.py so no real OAuth is needed.
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


def wait_for(condition, timeout=30, poll=0.5, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = condition()
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


def send_keys(socket, keys, literal=False):
    cmd = ["tmux", "-L", socket, "send-keys"]
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

    def test_workflow_cli_and_launch():
        profile = f"workflow-{int(time.time())}-{os.getpid()}"
        socket = f"clauthing-{profile}"
        profile_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
        clauthing_bin = shutil.which("clauthing") or "clauthing"
        wf_bin = shutil.which("clauthing-workflow")
        child = None

        try:
            assert_true(FAKE_CLAUDE.exists(), "fake_claude.py missing")
            assert_true(wf_bin is not None, "clauthing-workflow not on PATH")
            subprocess.run(
                [clauthing_bin, "--profile", profile, "--set-claude", str(FAKE_CLAUDE)],
                check=True, capture_output=True, timeout=10,
            )

            child = pexpect.spawn(
                clauthing_bin,
                ["--no-kitty", "--profile", profile],
                encoding="utf-8", timeout=120, dimensions=(50, 200),
            )
            child.logfile_read = sys.stderr
            wait_for(lambda: subprocess.run(
                ["tmux", "-L", socket, "has-session"], capture_output=True
            ).returncode == 0, timeout=15, label="tmux session")
            time.sleep(2)

            wait_for(lambda: "FAKE_LOGIN_SCREEN" in capture_pane(socket),
                     timeout=15, label="login screen")
            send_keys(socket, "login", literal=True)
            send_keys(socket, "Enter")
            wait_for(lambda: "FAKE_READY" in capture_pane(socket),
                     timeout=15, label="ready after login")
            tmux(socket, "rename-window", "-t", "1", "main")
            time.sleep(1)

            # ── clauthing-workflow list-windows (via CLAUTHING_WORKFLOW_ID) ──
            env = {**os.environ, "CLAUTHING_WORKFLOW_ID": socket}
            r = subprocess.run([wf_bin, "list-windows"],
                               capture_output=True, text=True, env=env, timeout=10)
            assert_true(r.returncode == 0,
                       f"list-windows failed: {r.stderr}")
            windows = json.loads(r.stdout)
            print(f"\n  [workflow] list-windows: {windows}", flush=True)
            assert_true(len(windows) == 1, f"expected 1 window. got {windows}")
            assert_true(windows[0]["name"] == "main",
                       f"expected window 'main'. got {windows[0]}")
            # The stable clauthing_window handle must be reported.
            assert_true(windows[0].get("clauthing_window"),
                       f"window should report clauthing_window. got {windows[0]}")

            # ── clauthing --launch-workflow opens a new window ───────────────
            # Use 'sleep 60' as a stub workflow program — it just keeps the
            # window alive long enough to inspect.
            r = subprocess.run(
                [clauthing_bin, "--profile", profile,
                 "--launch-workflow", "sleep 60", "--workflow-name", "goals"],
                capture_output=True, text=True, timeout=10,
            )
            assert_true(r.returncode == 0,
                       f"--launch-workflow failed: {r.stderr}")
            time.sleep(1)

            r = subprocess.run([wf_bin, "list-windows"],
                               capture_output=True, text=True, env=env, timeout=10)
            windows = json.loads(r.stdout)
            print(f"  [workflow] after launch-workflow: {windows}", flush=True)
            names = sorted(w["name"] for w in windows)
            assert_true("goals" in names,
                       f"goals window should be present. got {names}")

            # ── select-window by name ───────────────────────────────────────
            r = subprocess.run([wf_bin, "select-window", "goals"],
                               capture_output=True, text=True, env=env, timeout=10)
            assert_true(r.returncode == 0, f"select-window failed: {r.stderr}")
            active = tmux(socket, "display-message", "-p", "#{window_name}")
            assert_true(active == "goals", f"active should be 'goals'. got {active!r}")

            # ── select-window by the stable clauthing_window handle ─────────
            # 'main' is a real claude window so it has a clauthing_window;
            # 'goals' is a stub `sleep` program and won't. We're currently on
            # 'goals' — addressing 'main' by its stable id should jump back.
            main_cw = next(w["clauthing_window"] for w in windows if w["name"] == "main")
            r = subprocess.run([wf_bin, "select-window", main_cw],
                               capture_output=True, text=True, env=env, timeout=10)
            assert_true(r.returncode == 0, f"select by clauthing_window failed: {r.stderr}")
            active = tmux(socket, "display-message", "-p", "#{window_name}")
            assert_true(active == "main",
                       f"select by clauthing_window should focus main. got {active!r}")
            # restore focus to goals for the subsequent steps
            tmux(socket, "select-window", "-t", "goals")

            # ── current-window returns the just-selected window ─────────────
            r = subprocess.run([wf_bin, "current-window"],
                               capture_output=True, text=True, env=env, timeout=10)
            cur = json.loads(r.stdout)
            print(f"  [workflow] current-window: {cur}", flush=True)
            assert_true(cur["name"] == "goals", f"expected name 'goals'. got {cur}")

            # ── close-window removes 'goals' ────────────────────────────────
            r = subprocess.run([wf_bin, "close-window", "goals"],
                               capture_output=True, text=True, env=env, timeout=10)
            assert_true(r.returncode == 0, f"close-window failed: {r.stderr}")
            time.sleep(1)
            r = subprocess.run([wf_bin, "list-windows"],
                               capture_output=True, text=True, env=env, timeout=10)
            windows = json.loads(r.stdout)
            names = [w["name"] for w in windows]
            assert_true("goals" not in names,
                       f"'goals' should be closed. got {names}")

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

    runner.run_test("workflow_cli_and_launch", test_workflow_cli_and_launch)
    return runner.summary()


if __name__ == "__main__":
    print("clauthing-workflow Tests")
    print("=" * 50)
    sys.exit(run_test())
