#!/usr/bin/env python3
"""Test :after <window-name> moves the current window to right after the named one.

Uses fake_claude.py to avoid real OAuth. Multi-tab mode only — :after
manipulates the window order which doesn't apply in --one-tab.
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
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout


def window_order(socket):
    """Returns list of (index, name) sorted by index."""
    out = tmux(socket, "list-windows", "-F", "#{window_index}:#{window_name}", check=False)
    pairs = []
    for line in out.splitlines():
        if ":" not in line:
            continue
        idx, name = line.split(":", 1)
        try:
            pairs.append((int(idx), name))
        except ValueError:
            pass
    return sorted(pairs)


def run_test():
    runner = TestRunner()

    def test_after_moves_current_window():
        profile = f"after-{int(time.time())}-{os.getpid()}"
        socket = f"clauthing-{profile}"
        profile_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
        clauthing_bin = shutil.which("clauthing") or "clauthing"
        child = None

        try:
            assert_true(FAKE_CLAUDE.exists(), "fake_claude.py missing")
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

            # Window 1: log in
            wait_for(lambda: "FAKE_LOGIN_SCREEN" in capture_pane(socket),
                     timeout=15, label="login screen")
            send_keys(socket, "login", literal=True)
            send_keys(socket, "Enter")
            wait_for(lambda: "FAKE_READY" in capture_pane(socket),
                     timeout=15, label="ready after login")
            time.sleep(1)

            # Rename window 1 to "alpha"
            tmux(socket, "rename-window", "-t", "1", "alpha")

            # Open two more windows named "beta" and "gamma".
            jail_dir = f"/tmp/clauthing-{UID}"
            for name in ("beta", "gamma"):
                tmux(socket, "new-window", "-c", jail_dir, "-n", name,
                     f"{clauthing_bin} --profile {profile} --new-claude")
            wait_for(lambda: len(window_order(socket)) >= 3, timeout=15,
                     label="3 windows present")
            time.sleep(1)

            before = window_order(socket)
            print(f"\n  [after] before: {before}", flush=True)
            names_before = [n for _, n in before]
            assert_true(names_before == ["alpha", "beta", "gamma"],
                       f"setup expected [alpha, beta, gamma], got {names_before}")

            # We're now in window 'gamma' (the last one created → focus).
            # Run :after alpha — gamma should land in alpha's neighbour slot.
            send_keys(socket, ":after alpha", literal=True)
            send_keys(socket, "Enter")
            time.sleep(3)

            after = window_order(socket)
            print(f"  [after] after: {after}", flush=True)

            # alpha must precede gamma. gamma must be immediately after alpha.
            order_names = [n for _, n in after]
            assert_true("alpha" in order_names and "gamma" in order_names,
                       f"both windows still present. order={order_names}")
            alpha_pos = order_names.index("alpha")
            gamma_pos = order_names.index("gamma")
            assert_true(gamma_pos == alpha_pos + 1,
                       f"gamma should be immediately after alpha. order={order_names}")

            # All three windows still exist (no kills)
            assert_true(set(order_names) == {"alpha", "beta", "gamma"},
                       f"all three windows should remain. order={order_names}")

            # Focus should remain on the moved window (gamma), not on whatever
            # window got swapped into gamma's old slot.
            active = tmux(socket, "display-message", "-p", "#{window_name}")
            print(f"  [after] active window: {active!r}", flush=True)
            assert_true(active == "gamma",
                       f"focus should stay on moved window 'gamma'. active={active!r}")

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

    runner.run_test("after_moves_current_window", test_after_moves_current_window)

    def _setup_three_windows(profile):
        """Start clauthing + tmux with 3 windows named alpha/beta/gamma.
        Returns (child, socket). Caller is responsible for cleanup."""
        socket = f"clauthing-{profile}"
        clauthing_bin = shutil.which("clauthing") or "clauthing"
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
        time.sleep(1)
        tmux(socket, "rename-window", "-t", "1", "alpha")
        jail_dir = f"/tmp/clauthing-{UID}"
        for name in ("beta", "gamma"):
            tmux(socket, "new-window", "-c", jail_dir, "-n", name,
                 f"{clauthing_bin} --profile {profile} --new-claude")
        wait_for(lambda: len(window_order(socket)) >= 3, timeout=15,
                 label="3 windows present")
        time.sleep(1)
        return child, socket

    def _cleanup(child, socket, profile):
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
        profile_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
        shutil.rmtree(profile_dir, ignore_errors=True)

    def test_before_moves_current_window():
        profile = f"before-{int(time.time())}-{os.getpid()}"
        child, socket = _setup_three_windows(profile)
        try:
            before = window_order(socket)
            names_before = [n for _, n in before]
            print(f"\n  [before] before: {before}", flush=True)
            assert_true(names_before == ["alpha", "beta", "gamma"],
                       f"setup expected [alpha, beta, gamma], got {names_before}")
            # We're in gamma. :before alpha should put gamma right before alpha.
            # But alpha is at index 1 → no slot before, should error.
            send_keys(socket, ":before alpha", literal=True)
            send_keys(socket, "Enter")
            time.sleep(2)
            mid = window_order(socket)
            print(f"  [before] after :before alpha (should error): {mid}", flush=True)
            assert_true([n for _, n in mid] == names_before,
                       f":before on first window should be a no-op. order={mid}")

            # :before beta should put gamma right before beta.
            send_keys(socket, ":before beta", literal=True)
            send_keys(socket, "Enter")
            time.sleep(3)
            after = window_order(socket)
            print(f"  [before] after :before beta: {after}", flush=True)
            order_names = [n for _, n in after]
            beta_pos = order_names.index("beta")
            gamma_pos = order_names.index("gamma")
            assert_true(gamma_pos == beta_pos - 1,
                       f"gamma should be immediately before beta. order={order_names}")
            assert_true(set(order_names) == {"alpha", "beta", "gamma"},
                       f"all three windows should remain. order={order_names}")
            active = tmux(socket, "display-message", "-p", "#{window_name}")
            assert_true(active == "gamma",
                       f"focus should stay on moved window 'gamma'. active={active!r}")
        finally:
            _cleanup(child, socket, profile)

    def test_first_cycles_window_list():
        profile = f"first-{int(time.time())}-{os.getpid()}"
        child, socket = _setup_three_windows(profile)
        try:
            before = window_order(socket)
            names_before = [n for _, n in before]
            print(f"\n  [first] before: {before}", flush=True)
            assert_true(names_before == ["alpha", "beta", "gamma"],
                       f"setup expected [alpha, beta, gamma], got {names_before}")
            # We're in gamma. :first should rotate so the order is gamma,alpha,beta.
            send_keys(socket, ":first", literal=True)
            send_keys(socket, "Enter")
            time.sleep(3)
            after = window_order(socket)
            print(f"  [first] after :first: {after}", flush=True)
            order_names = [n for _, n in after]
            assert_true(order_names == ["gamma", "alpha", "beta"],
                       f":first should cycle to [gamma, alpha, beta]. got={order_names}")
            active = tmux(socket, "display-message", "-p", "#{window_name}")
            assert_true(active == "gamma",
                       f"focus should stay on 'gamma'. active={active!r}")
        finally:
            _cleanup(child, socket, profile)

    runner.run_test("before_moves_current_window", test_before_moves_current_window)
    runner.run_test("first_cycles_window_list", test_first_cycles_window_list)

    def test_close_window_by_index_and_name():
        profile = f"close-{int(time.time())}-{os.getpid()}"
        child, socket = _setup_three_windows(profile)
        try:
            assert_true([n for _, n in window_order(socket)] == ["alpha", "beta", "gamma"],
                       f"setup expected [alpha, beta, gamma]")

            # :close by index — close window 2 (beta)
            send_keys(socket, ":close 2", literal=True)
            send_keys(socket, "Enter")
            time.sleep(2)
            after = window_order(socket)
            names = [n for _, n in after]
            print(f"\n  [close] after :close 2: {after}", flush=True)
            assert_true("beta" not in names,
                       f":close 2 should kill beta. got={names}")
            assert_true(set(names) == {"alpha", "gamma"},
                       f"only alpha and gamma should remain. got={names}")

            # :close by name — close gamma
            send_keys(socket, ":close gamma", literal=True)
            send_keys(socket, "Enter")
            time.sleep(2)
            after = window_order(socket)
            names = [n for _, n in after]
            print(f"  [close] after :close gamma: {after}", flush=True)
            assert_true(names == ["alpha"],
                       f":close gamma should leave only alpha. got={names}")

            # :close last window — should refuse
            send_keys(socket, ":close alpha", literal=True)
            send_keys(socket, "Enter")
            time.sleep(2)
            after = window_order(socket)
            names = [n for _, n in after]
            print(f"  [close] after :close alpha (last): {after}", flush=True)
            assert_true(names == ["alpha"],
                       f":close on last window should refuse. got={names}")
        finally:
            _cleanup(child, socket, profile)

    runner.run_test("close_window_by_index_and_name", test_close_window_by_index_and_name)
    return runner.summary()


if __name__ == "__main__":
    print(":after Tests")
    print("=" * 50)
    sys.exit(run_test())
