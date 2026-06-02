#!/usr/bin/env python3
"""Test inter-window messaging (:msg <window-name> <body>).

This is the feature that reads windows.json (for the sender's `from` title)
and writes a per-session inbox under <runtime>/messages/<session_id>.jsonl.
It's currently the only consumer of windows.json with no functional test, so
this pins its behaviour down before any state-store refactor.

Flow (uses fake_claude.py, no real OAuth):
  1. launch clauthing, log in window 1 → rename it "alpha" (session A)
  2. open a second window "beta" with its own claude session (session B)
  3. from alpha, `:msg beta <body>` → message lands in beta's inbox ONLY
     (no keystroke injection into beta's pane)
  4. from alpha, `:type beta <text>` → text IS typed into beta's pane
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


def window_sessions(socket):
    """Return {window_name: session_id} from live @session_id options."""
    out = tmux(socket, "list-windows", "-F",
               "#{window_name}\t#{@session_id}", check=False)
    d = {}
    for line in out.splitlines():
        if "\t" in line:
            name, sid = line.split("\t", 1)
            if sid:
                d[name] = sid
    return d


def window_clauthing_ids(socket):
    """Return {window_name: clauthing_window} from live @clauthing_window options."""
    out = tmux(socket, "list-windows", "-F",
               "#{window_name}\t#{@clauthing_window}", check=False)
    d = {}
    for line in out.splitlines():
        if "\t" in line:
            name, cw = line.split("\t", 1)
            if cw:
                d[name] = cw
    return d


def runtime_dir():
    """Mirror events.get_runtime_dir: /var/run/<uid>/clauthing else /tmp fallback."""
    for cand in (Path(f"/var/run/{UID}/clauthing"),
                 Path(f"/run/{UID}/clauthing"),
                 Path(f"/tmp/clauthing-{UID}")):
        if cand.exists():
            return cand
    return Path(f"/tmp/clauthing-{UID}")


def login_pane(socket, target):
    """Drive fake_claude through login (if needed) until FAKE_READY."""
    def state():
        pane = capture_pane(socket, target)
        if "FAKE_READY" in pane:
            return "ready"
        if "FAKE_LOGIN_SCREEN" in pane:
            return "login"
        return None

    s = wait_for(state, timeout=20, label=f"login/ready on {target}")
    if s == "login":
        send_keys(socket, "login", target=target, literal=True)
        send_keys(socket, "Enter", target=target)
        wait_for(lambda: "FAKE_READY" in capture_pane(socket, target),
                 timeout=20, label=f"ready after login on {target}")


def run_test():
    runner = TestRunner()

    def test_msg_inbox_and_type_injection():
        profile = f"msg-{int(time.time())}-{os.getpid()}"
        socket = f"clauthing-{profile}"
        profile_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
        clauthing_bin = shutil.which("clauthing") or "clauthing"
        jail_dir = f"/tmp/clauthing-{UID}"
        body = "hello-from-alpha"
        child = None
        beta_window = None

        try:
            assert_true(FAKE_CLAUDE.exists(), "fake_claude.py missing")
            subprocess.run(
                [clauthing_bin, "--profile", profile, "--set-claude", str(FAKE_CLAUDE)],
                check=True, capture_output=True, timeout=10,
            )

            # ── launch + log in window 1, rename it "alpha" ─────────────────
            child = pexpect.spawn(
                clauthing_bin, ["--no-kitty", "--profile", profile],
                encoding="utf-8", timeout=120, dimensions=(50, 200),
            )
            child.logfile_read = sys.stderr
            wait_for(lambda: subprocess.run(
                ["tmux", "-L", socket, "has-session"], capture_output=True
            ).returncode == 0, timeout=15, label="tmux session")
            time.sleep(2)
            login_pane(socket, ":1")
            tmux(socket, "rename-window", "-t", "1", "alpha")

            # ── open window 2 "beta" with its own claude session ────────────
            tmux(socket, "new-window", "-c", jail_dir, "-n", "beta",
                 f"{clauthing_bin} --profile {profile} --new-claude")
            wait_for(lambda: "beta" in window_sessions(socket),
                     timeout=15, label="beta window registered @session_id")
            login_pane(socket, ":2")

            # Both windows must carry an @session_id before we can route.
            sids = wait_for(
                lambda: window_sessions(socket)
                if {"alpha", "beta"} <= window_sessions(socket).keys() else None,
                timeout=15, label="alpha+beta session ids",
            )
            alpha_session = sids["alpha"]
            beta_session = sids["beta"]
            print(f"\n  alpha={alpha_session}  beta={beta_session}", flush=True)
            assert_true(alpha_session != beta_session, "sessions must differ")

            # Inboxes are keyed by the stable clauthing_window, not session_id.
            cws = wait_for(
                lambda: window_clauthing_ids(socket)
                if {"alpha", "beta"} <= window_clauthing_ids(socket).keys() else None,
                timeout=15, label="alpha+beta clauthing_window ids",
            )
            alpha_window = cws["alpha"]
            beta_window = cws["beta"]
            print(f"  alpha_window={alpha_window}  beta_window={beta_window}", flush=True)
            assert_true(alpha_window != beta_window, "window ids must differ")

            # ── :msg alpha → beta: records to inbox, does NOT type the pane ──
            send_keys(socket, f":msg beta {body}", target=":1", literal=True)
            send_keys(socket, "Enter", target=":1")

            inbox = runtime_dir() / "messages" / f"{beta_window}.jsonl"
            wait_for(lambda: inbox.exists(), timeout=15,
                     label=f"inbox for beta ({inbox})")

            entries = [json.loads(l) for l in inbox.read_text().splitlines() if l]
            print(f"  inbox entries: {entries}", flush=True)
            assert_true(len(entries) >= 1, "expected at least one inbox entry")
            entry = entries[-1]
            assert_true(entry.get("message") == body,
                        f"message body should be {body!r}. got {entry.get('message')!r}")
            assert_true(entry.get("read") is False,
                        f"new message should be unread. got {entry.get('read')!r}")
            # Sender labelled by its stable clauthing_window + live window name.
            assert_true(entry.get("from_window") == alpha_window,
                        f"from_window should be alpha {alpha_window}. got {entry.get('from_window')!r}")
            assert_true(entry.get("from") == "alpha",
                        f"from should be the sender window name 'alpha'. got {entry.get('from')!r}")

            # :msg must NOT inject into beta's pane. Give it a moment to (not) appear.
            time.sleep(1.5)
            beta_pane = capture_pane(socket, ":2")
            assert_true(body not in beta_pane,
                        f":msg should not type {body!r} into beta's pane, but it appeared:\n{beta_pane}")

            # ── :type alpha → beta: this one DOES type into beta's pane ──────
            typed = "typed-by-alpha"
            send_keys(socket, f":type beta {typed}", target=":1", literal=True)
            send_keys(socket, "Enter", target=":1")
            wait_for(lambda: typed in capture_pane(socket, ":2"),
                     timeout=10, label="typed text visible in beta's pane")

            # ── :save in beta bookmarks its last message into window-state ───
            ws_file = profile_dir / "window-state" / f"{beta_window}.json"
            send_keys(socket, ":save", target=":2", literal=True)
            send_keys(socket, "Enter", target=":2")
            wait_for(lambda: ws_file.exists(), timeout=10,
                     label=f"window-state file ({ws_file})")
            ws = json.loads(ws_file.read_text())
            print(f"  window-state: {ws}", flush=True)
            bookmarks = ws.get("bookmarks", [])
            assert_true(any(b.get("message") == body for b in bookmarks),
                        f"saved bookmark should contain {body!r}. got {bookmarks}")

            # ── :save/:peek are non-destructive — inbox stays unread ────────
            inbox_entries = [json.loads(l) for l in inbox.read_text().splitlines() if l]
            assert_true(any(e.get("message") == body and e.get("read") is False
                            for e in inbox_entries),
                        f":save must not mark the message read. got {inbox_entries}")

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
            # Clean the (non-profile-scoped) inbox we created.
            if beta_window:
                try:
                    (runtime_dir() / "messages" / f"{beta_window}.jsonl").unlink(missing_ok=True)
                except Exception:
                    pass

    runner.run_test("msg_inbox_and_type_injection", test_msg_inbox_and_type_injection)
    return runner.summary()


if __name__ == "__main__":
    print("clauthing inter-window messaging Tests")
    print("=" * 50)
    sys.exit(run_test())
