#!/usr/bin/env python3
"""Full-stack live tests with injected credentials.

Tests:
  cd_changes_directory  — :cd changes the pane's working directory; !pwd confirms it
  new_window_opens      — C-n (multi-tab mode) opens a second claude window

Usage:
    python live_tests/test_cd_live.py [/path/to/creds.json]

Get creds first:
    creds-for-claude get > /tmp/creds.json
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pexpect

sys.path.insert(0, str(Path(__file__).parent.parent / "functional_tests"))
from harness import TestRunner, assert_true

CREDS_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/creds.json")
PROFILE = "cd-live-test"
UID = os.getuid()


# ── Shared helpers ────────────────────────────────────────────────────────────

def wait_for(condition, timeout=30, poll=0.5, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(poll)
    raise TimeoutError(f"Timed out ({timeout}s) waiting for: {label}")


def tmux(socket, *args, check=True):
    r = subprocess.run(["tmux", "-L", socket, *args], capture_output=True, text=True, timeout=10)
    if check and r.returncode != 0:
        raise RuntimeError(f"tmux {args} failed: {r.stderr!r}")
    return r.stdout.strip()


def send_keys(socket, keys, literal=False):
    cmd = ["tmux", "-L", socket, "send-keys"]
    if literal:
        cmd.append("-l")
    cmd.append(keys)
    subprocess.run(cmd, check=True, timeout=5)


def list_windows(socket):
    out = tmux(socket, "list-windows", "-F", "#{window_index}:#{window_name}:#{pane_current_path}", check=False)
    return [l for l in out.splitlines() if l]


def window_names(socket):
    out = tmux(socket, "list-windows", "-F", "#{window_name}", check=False)
    return [l for l in out.splitlines() if l]


def capture_pane(socket):
    return tmux(socket, "capture-pane", "-p", check=False)


def wait_for_claude_prompt(socket, timeout=60):
    """Wait for ❯ prompt, pressing Enter through any trust dialogs."""
    def check():
        pane = capture_pane(socket)
        if "I trust this folder" in pane or "trust this" in pane.lower():
            send_keys(socket, "Enter")
            return False
        lines = pane.strip().splitlines()
        for line in reversed(lines):
            stripped = line.strip()
            if stripped == "❯" or stripped.startswith("❯ "):
                return True
        return False
    wait_for(check, timeout=timeout, poll=1, label="claude prompt")


def find_socket(profile, after_time, timeout=15):
    tmux_dir = Path(f"/tmp/tmux-{UID}")
    prefix = f"cl1-{profile}-"
    def check():
        if not tmux_dir.exists():
            return None
        for s in tmux_dir.iterdir():
            if s.name.startswith(prefix):
                try:
                    if s.stat().st_ctime >= after_time - 1:
                        return s.name
                except Exception:
                    pass
        return None
    return wait_for(check, timeout=timeout, label=f"tmux socket {prefix}*")


def find_session_file_with_messages(claude_data_dir, cwd, timeout=90):
    encoded = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
    projects_dir = Path(claude_data_dir) / "projects" / encoded

    def check():
        if not projects_dir.exists():
            return None
        for f in sorted(projects_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            if '"type":"user"' in f.read_text() or '"type": "user"' in f.read_text():
                return f
        return None

    return wait_for(check, timeout=timeout, poll=1, label=f"session file under {projects_dir}")


def get_claude_config(socket, profile):
    """Get CLAUDE_CONFIG_DIR from tmux env or filesystem fallback."""
    try:
        env_line = tmux(socket, "show-environment", "-g", "CLAUDE_CONFIG_DIR")
        if "=" in env_line and not env_line.startswith("-"):
            return env_line.split("=", 1)[1]
    except Exception:
        pass
    profile_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    session_configs = profile_dir / "session-configs"
    if session_configs.exists():
        dirs = sorted(session_configs.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if dirs:
            return str(dirs[0])
    return None


# ── Tests ─────────────────────────────────────────────────────────────────────

def run_test():
    runner = TestRunner()

    def test_cd_changes_directory():
        """
        :cd moves the session to a new directory.
        Verified two ways: pane_current_path and !pwd output inside claude.
        """
        target_dir = Path(tempfile.mkdtemp(prefix="cd-live-target-"))
        child = None
        socket = None

        try:
            clauthing_bin = shutil.which("clauthing") or "clauthing"
            t0 = time.time()

            child = pexpect.spawn(
                clauthing_bin,
                ["--no-kitty", "--one-tab",
                 "--inject-credentials", str(CREDS_PATH),
                 "--profile", PROFILE],
                encoding="utf-8",
                timeout=120,
                dimensions=(50, 200),
            )
            child.logfile_read = sys.stderr

            print(f"\n  [cd] waiting for tmux socket...", flush=True)
            socket = find_socket(PROFILE, after_time=t0, timeout=15)
            print(f"  [cd] socket: {socket}", flush=True)

            time.sleep(3)

            claude_config = get_claude_config(socket, PROFILE)
            print(f"  [cd] CLAUDE_CONFIG_DIR: {claude_config}", flush=True)

            cwd = tmux(socket, "display-message", "-p", "#{pane_current_path}")
            print(f"  [cd] pane cwd: {cwd}", flush=True)

            print("  [cd] waiting for claude prompt...", flush=True)
            wait_for_claude_prompt(socket, timeout=60)
            time.sleep(1)

            if not claude_config:
                claude_config = get_claude_config(socket, PROFILE)
                print(f"  [cd] CLAUDE_CONFIG_DIR (retry): {claude_config}", flush=True)

            # Send a message so a session file with messages exists
            print("  [cd] sending message...", flush=True)
            send_keys(socket, "say the single word 'ready'", literal=True)
            send_keys(socket, "Enter")
            time.sleep(2)

            assert claude_config, "CLAUDE_CONFIG_DIR not found"
            print("  [cd] waiting for session file...", flush=True)
            find_session_file_with_messages(claude_config, cwd, timeout=90)

            def response_done():
                pane = capture_pane(socket)
                return "ready" in pane.lower() and ("❯" in pane or "> " in pane.split("ready")[-1])
            try:
                wait_for(response_done, timeout=60, poll=1, label="response done")
            except TimeoutError:
                pass
            time.sleep(2)

            before_windows = list_windows(socket)
            before_count = len(before_windows)
            before_names = window_names(socket)
            print(f"  [cd] before :cd — windows: {before_windows}, names: {before_names}", flush=True)

            # ── :cd ──────────────────────────────────────────────────────────
            print(f"  [cd] sending :cd {target_dir}", flush=True)
            send_keys(socket, f":cd {target_dir}", literal=True)
            send_keys(socket, "Enter")

            # Boomerang replaces in-place — wait for pane_current_path to change
            print("  [cd] waiting for pane cwd to change...", flush=True)
            def pane_in_target():
                # path is the third field: index:name:path
                parts_list = [w.split(":", 2) for w in list_windows(socket) if w.count(":") >= 2]
                return any(str(target_dir) in p[2] for p in parts_list)
            wait_for(pane_in_target, timeout=30, poll=0.5, label="pane cwd = target_dir")

            after_windows = list_windows(socket)
            after_names = window_names(socket)
            print(f"  [cd] after :cd — windows: {after_windows}, names: {after_names}", flush=True)

            paths = [w.split(":", 2)[2] for w in after_windows if w.count(":") >= 2]
            assert_true(any(str(target_dir) in p for p in paths),
                       f"pane_current_path should be {target_dir}. Paths: {paths}")
            assert_true(len(after_windows) == 1,
                       f":cd should leave exactly one window. Got: {after_windows}")
            assert_true(after_names == before_names,
                       f"window name should not change. Before: {before_names}, after: {after_names}")

            # Wait for claude to restart in the new dir
            print("  [cd] waiting for claude prompt in new dir...", flush=True)
            wait_for_claude_prompt(socket, timeout=30)
            time.sleep(1)

            # ── Previous message visible in resumed session ────────────────────
            pane = capture_pane(socket)
            print(f"  [cd] pane on resume:\n{pane[-400:]}", flush=True)
            assert_true("ready" in pane.lower(),
                       f"Resumed session should show previous 'ready' message in pane")
            print("  [cd] confirmed: previous message visible after :cd", flush=True)

            # ── !pwd verification ─────────────────────────────────────────────
            print("  [cd] verifying directory with !pwd...", flush=True)
            send_keys(socket, "!pwd", literal=True)
            send_keys(socket, "Enter")

            def pwd_shows_target():
                return str(target_dir) in capture_pane(socket)
            wait_for(pwd_shows_target, timeout=15, poll=0.5, label="!pwd output contains target_dir")

            pane = capture_pane(socket)
            assert_true(str(target_dir) in pane,
                       f"!pwd should show {target_dir} in pane output")
            print(f"  [cd] confirmed: !pwd shows {target_dir}", flush=True)

        finally:
            shutil.rmtree(target_dir, ignore_errors=True)
            if socket:
                try:
                    tmux(socket, "kill-server", check=False)
                except Exception:
                    pass
            if child:
                try:
                    child.close(force=True)
                except Exception:
                    pass

    runner.run_test("cd_changes_directory", test_cd_changes_directory)

    def test_new_window_opens():
        """
        C-n in multi-tab (--no-kitty) mode opens a second window with claude.
        """
        nw_profile = f"nw-live-{int(time.time())}"
        socket = f"clauthing-{nw_profile}"
        child = None

        try:
            clauthing_bin = shutil.which("clauthing") or "clauthing"

            child = pexpect.spawn(
                clauthing_bin,
                ["--no-kitty",
                 "--inject-credentials", str(CREDS_PATH),
                 "--profile", nw_profile],
                encoding="utf-8",
                timeout=120,
                dimensions=(50, 200),
            )
            child.logfile_read = sys.stderr

            print(f"\n  [new-win] waiting for tmux session ({socket})...", flush=True)
            def session_exists():
                r = subprocess.run(["tmux", "-L", socket, "has-session"],
                                   capture_output=True)
                return r.returncode == 0
            wait_for(session_exists, timeout=15, label="tmux session")

            time.sleep(2)

            print("  [new-win] waiting for claude prompt...", flush=True)
            wait_for_claude_prompt(socket, timeout=60)
            time.sleep(1)

            before_windows = list_windows(socket)
            print(f"  [new-win] windows before: {before_windows}", flush=True)

            # Trigger the C-n binding action directly. (`send-keys C-n` would
            # send the key to claude inside the pane, not to tmux's keybinding
            # handler, so the binding wouldn't fire.) The binding configured
            # in handle_no_kitty is: new-window -c {jail_dir} {clauthing_cmd}
            jail_dir = f"/tmp/clauthing-{UID}"
            clauthing_bin = shutil.which("clauthing") or "clauthing"
            tmux(socket, "new-window", "-c", jail_dir,
                 f"{clauthing_bin} --profile {nw_profile} --new-window")

            print("  [new-win] waiting for new window...", flush=True)
            def new_win_appeared():
                return len(list_windows(socket)) > len(before_windows)
            wait_for(new_win_appeared, timeout=15, poll=0.5, label="new tmux window")

            after_windows = list_windows(socket)
            print(f"  [new-win] windows after: {after_windows}", flush=True)
            assert_true(len(after_windows) > len(before_windows),
                       f"new-window should add a window. Before: {before_windows}, after: {after_windows}")

            print("  [new-win] waiting for claude prompt in new window...", flush=True)
            new_idx = max(int(w.split(":")[0]) for w in after_windows)
            tmux(socket, "select-window", "-t", str(new_idx))
            wait_for_claude_prompt(socket, timeout=60)
            print("  [new-win] new window has claude prompt", flush=True)

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

    runner.run_test("new_window_opens", test_new_window_opens)

    def test_cd_changes_directory_multi_tab():
        """
        :cd in multi-tab (--no-kitty without --one-tab) mode replaces in-place.
        Same boomerang+respawn-pane mechanism as one-tab.
        """
        mt_profile = f"cd-mt-live-{int(time.time())}"
        socket = f"clauthing-{mt_profile}"
        target_dir = Path(tempfile.mkdtemp(prefix="cd-mt-target-"))
        child = None

        try:
            clauthing_bin = shutil.which("clauthing") or "clauthing"

            child = pexpect.spawn(
                clauthing_bin,
                ["--no-kitty",
                 "--inject-credentials", str(CREDS_PATH),
                 "--profile", mt_profile],
                encoding="utf-8",
                timeout=120,
                dimensions=(50, 200),
            )
            child.logfile_read = sys.stderr

            print(f"\n  [cd-mt] waiting for tmux session ({socket})...", flush=True)
            def session_exists():
                r = subprocess.run(["tmux", "-L", socket, "has-session"],
                                   capture_output=True)
                return r.returncode == 0
            wait_for(session_exists, timeout=15, label="tmux session")
            time.sleep(2)

            print("  [cd-mt] waiting for claude prompt...", flush=True)
            wait_for_claude_prompt(socket, timeout=60)
            time.sleep(1)

            # Get CLAUDE_CONFIG_DIR for this multi-tab window's session
            cwd = tmux(socket, "display-message", "-p", "#{pane_current_path}")
            print(f"  [cd-mt] pane cwd: {cwd}", flush=True)
            claude_config = get_claude_config(socket, mt_profile)
            print(f"  [cd-mt] CLAUDE_CONFIG_DIR: {claude_config}", flush=True)

            # Send a message so the session has messages (required by :cd)
            print("  [cd-mt] sending message...", flush=True)
            send_keys(socket, "say the single word 'ready'", literal=True)
            send_keys(socket, "Enter")
            time.sleep(2)

            assert claude_config, "CLAUDE_CONFIG_DIR not found"
            print("  [cd-mt] waiting for session file...", flush=True)
            find_session_file_with_messages(claude_config, cwd, timeout=90)

            def response_done():
                pane = capture_pane(socket)
                return "ready" in pane.lower() and "❯" in pane
            try:
                wait_for(response_done, timeout=60, poll=1, label="response done")
            except TimeoutError:
                pass
            time.sleep(2)

            before_windows = list_windows(socket)
            before_count = len(before_windows)
            before_names = window_names(socket)
            print(f"  [cd-mt] before :cd — windows: {before_windows}, names: {before_names}", flush=True)

            # ── :cd ──────────────────────────────────────────────────────────
            print(f"  [cd-mt] sending :cd {target_dir}", flush=True)
            send_keys(socket, f":cd {target_dir}", literal=True)
            send_keys(socket, "Enter")

            print("  [cd-mt] waiting for pane cwd to change...", flush=True)
            def pane_in_target():
                parts_list = [w.split(":", 2) for w in list_windows(socket) if w.count(":") >= 2]
                return any(str(target_dir) in p[2] for p in parts_list)
            wait_for(pane_in_target, timeout=30, poll=0.5, label="pane cwd = target_dir")

            after_windows = list_windows(socket)
            after_names = window_names(socket)
            print(f"  [cd-mt] after :cd — windows: {after_windows}, names: {after_names}", flush=True)

            paths = [w.split(":", 2)[2] for w in after_windows if w.count(":") >= 2]
            assert_true(any(str(target_dir) in p for p in paths),
                       f"pane_current_path should be {target_dir}. Paths: {paths}")
            assert_true(len(after_windows) == before_count,
                       f":cd should not open a new window. Before: {before_count}, after: {len(after_windows)}")

            # Wait for claude to restart in the new dir
            print("  [cd-mt] waiting for claude prompt in new dir...", flush=True)
            try:
                wait_for_claude_prompt(socket, timeout=90)
            except TimeoutError:
                pane = capture_pane(socket)
                print(f"  [cd-mt] TIMEOUT pane content:\n{pane}", flush=True)
                raise
            time.sleep(1)

            # Previous message should be visible
            pane = capture_pane(socket)
            assert_true("ready" in pane.lower(),
                       f"Resumed session should show previous 'ready' message")
            print("  [cd-mt] confirmed: previous message visible", flush=True)

            # !pwd verification
            print("  [cd-mt] verifying directory with !pwd...", flush=True)
            send_keys(socket, "!pwd", literal=True)
            send_keys(socket, "Enter")

            def pwd_shows_target():
                return str(target_dir) in capture_pane(socket)
            wait_for(pwd_shows_target, timeout=15, poll=0.5, label="!pwd output contains target_dir")
            print(f"  [cd-mt] confirmed: !pwd shows {target_dir}", flush=True)

        finally:
            shutil.rmtree(target_dir, ignore_errors=True)
            try:
                subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, timeout=5)
            except Exception:
                pass
            if child:
                try:
                    child.close(force=True)
                except Exception:
                    pass

    runner.run_test("cd_changes_directory_multi_tab", test_cd_changes_directory_multi_tab)

    return runner.summary()


if __name__ == "__main__":
    if not CREDS_PATH.exists():
        print(f"ERROR: {CREDS_PATH} not found. Run: creds-for-claude get > /tmp/creds.json", file=sys.stderr)
        sys.exit(1)

    print("clauthing Live Tests")
    print("=" * 50)
    print(f"  creds:   {CREDS_PATH}")
    print(f"  profile: {PROFILE}")
    print()
    sys.exit(run_test())
