"""External interface for workflow programs that orchestrate clauthing windows.

Designed for "workflow windows" — programs that live in a tmux window inside
clauthing and manage other windows (e.g. a goals doc with clickable wikilinks
that jump between sessions). Stateless: each invocation reads
$CLAUTHING_WORKFLOW_ID (the tmux socket name) from the env, talks to tmux,
exits. No daemon, no long-lived connection.

Typical use:
    $ clauthing-workflow connect           # prints the workflow id
    $ export CLAUTHING_WORKFLOW_ID=<id>
    $ clauthing-workflow list-windows      # JSON list
    $ clauthing-workflow select-window god

Or launch a program inside clauthing with the id pre-set:
    $ clauthing --launch-workflow my-tui --name goals
"""

import argparse
import json
import os
import subprocess
import sys


def _socket_or_die():
    socket = os.environ.get("CLAUTHING_WORKFLOW_ID")
    if not socket:
        socket = os.environ.get("CLAUTHING_TMUX_SOCKET")
    if not socket:
        print("error: CLAUTHING_WORKFLOW_ID not set "
              "(run `clauthing-workflow connect` and export the value, "
              "or launch via `clauthing --launch-workflow`)", file=sys.stderr)
        sys.exit(2)
    return socket


def _tmux(socket, *args, check=True):
    return subprocess.run(
        ["tmux", "-L", socket, *args],
        capture_output=True, text=True, timeout=10,
        check=check,
    )


_WINDOW_FORMAT = ("#{window_index}\t#{window_name}\t#{window_id}\t"
                  "#{@clauthing_window}\t#{@session_id}\t#{pane_current_path}")


def _window_record(parts):
    """parts: [index, name, window_id, @clauthing_window, @session_id, path]"""
    return {
        "index": int(parts[0]),
        "name": parts[1],
        "window_id": parts[2],              # tmux window id (@N) — not stable across restart
        "clauthing_window": parts[3] or None,   # stable window id — survives :cd; the handle to store
        "session_id": parts[4] or None,         # current claude conversation (rotates on :cd)
        "path": parts[5],
    }


def cmd_connect(args):
    socket = os.environ.get("CLAUTHING_TMUX_SOCKET")
    if not socket:
        print("error: must be invoked from inside a clauthing tmux session "
              "(CLAUTHING_TMUX_SOCKET not set)", file=sys.stderr)
        sys.exit(2)
    print(socket)


def cmd_list_windows(args):
    socket = _socket_or_die()
    r = _tmux(socket, "list-windows", "-F", _WINDOW_FORMAT)
    windows = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 6:
            try:
                windows.append(_window_record(parts))
            except (ValueError, IndexError):
                continue
    print(json.dumps(windows, indent=2))


def cmd_current_window(args):
    socket = _socket_or_die()
    r = _tmux(socket, "display-message", "-p", _WINDOW_FORMAT)
    parts = r.stdout.strip().split("\t")
    if len(parts) < 6:
        print(json.dumps(None))
        return
    print(json.dumps(_window_record(parts), indent=2))


def _resolve_window(socket, arg):
    """Return tmux window-id (@N) for arg (clauthing_window, index, or name).

    A clauthing_window id is the stable handle workflow programs should store,
    so an exact match on it wins over index/name.
    """
    r = _tmux(
        socket, "list-windows",
        "-F", "#{window_index}\t#{window_name}\t#{window_id}\t#{@clauthing_window}",
        check=False,
    )
    if r.returncode != 0:
        return None
    matches = []
    by_index = None
    is_num = arg.isdigit()
    target_idx = int(arg) if is_num else None
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        name, wid, cw = parts[1], parts[2], parts[3]
        if cw and cw == arg:
            return wid  # exact stable-id match wins
        if is_num and idx == target_idx:
            by_index = wid
        if name == arg:
            matches.append(wid)
    if by_index:
        return by_index
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"error: {len(matches)} windows named {arg!r} — use the index",
              file=sys.stderr)
        sys.exit(2)
    return None


def cmd_select_window(args):
    socket = _socket_or_die()
    wid = _resolve_window(socket, args.window)
    if wid is None:
        print(f"error: no window matching {args.window!r}", file=sys.stderr)
        sys.exit(1)
    _tmux(socket, "select-window", "-t", wid)


def cmd_close_window(args):
    socket = _socket_or_die()
    wid = _resolve_window(socket, args.window)
    if wid is None:
        print(f"error: no window matching {args.window!r}", file=sys.stderr)
        sys.exit(1)
    # Best-effort: pull session_id off the window, drop it from open-sessions
    sess_r = _tmux(
        socket, "display-message", "-p", "-t", wid, "#{@session_id}",
        check=False,
    )
    sess_id = sess_r.stdout.strip() if sess_r.returncode == 0 else ""
    if sess_id:
        try:
            from clauthing.session import remove_open_session
            # Derive profile from socket (clauthing-X → X; clauthing → None)
            profile = None
            if socket.startswith("clauthing-"):
                profile = socket[len("clauthing-"):]
            remove_open_session(sess_id, profile)
        except Exception:
            pass
    _tmux(socket, "kill-window", "-t", wid)


def main():
    p = argparse.ArgumentParser(
        prog="clauthing-workflow",
        description="External interface for workflow programs that manage clauthing windows.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("connect", help="Print the workflow id for the current clauthing instance")
    sp.set_defaults(func=cmd_connect)

    sp = sub.add_parser("list-windows", help="List all windows as JSON")
    sp.set_defaults(func=cmd_list_windows)

    sp = sub.add_parser("current-window", help="Print current window info as JSON")
    sp.set_defaults(func=cmd_current_window)

    sp = sub.add_parser("select-window", help="Focus a window by index or name")
    sp.add_argument("window")
    sp.set_defaults(func=cmd_select_window)

    sp = sub.add_parser("close-window", help="Close a window by index or name")
    sp.add_argument("window")
    sp.set_defaults(func=cmd_close_window)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
