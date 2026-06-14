"""clauthing-api — the API a plugin calls to act on clauthing.

The single stable command a plugin uses to talk back to clauthing. clauthing
invokes the plugin with the socket + a credential in the environment:

    CLAUTHING_SOCKET / CLAUTHING_WORKFLOW_ID  — which tmux server
    CLAUTHING_PLUGIN_TOKEN                     — caller credential

Verbs:
    clauthing-api windows                  # JSON list (stable clauthing_window)
    clauthing-api open --resume <session>  # open/resume a window
    clauthing-api close  <window>          # close (id|name|clauthing_window)
    clauthing-api select <window>          # focus

Security is fictitious for now: the token is read (and will later identify the
caller and scope its permissions) but no access check is performed yet.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys

from clauthing.workflow import (
    _tmux, _window_record, _WINDOW_FORMAT, _resolve_window,
)
from clauthing.plugins_store import (
    load_plugin, save_plugin, issue_token, resolve_token, permissions_match,
)


def _socket():
    """Resolve the tmux socket. Plugins are handed CLAUTHING_SOCKET; fall back to
    the other names for direct CLI / workflow use."""
    s = (os.environ.get("CLAUTHING_SOCKET")
         or os.environ.get("CLAUTHING_TMUX_SOCKET")
         or os.environ.get("CLAUTHING_WORKFLOW_ID"))
    if not s:
        print("error: no clauthing socket in env (CLAUTHING_SOCKET)", file=sys.stderr)
        sys.exit(2)
    return s


def _profile(sock):
    return sock[len("clauthing-"):] if sock.startswith("clauthing-") else None


def _require(perm):
    """Enforce a permission for the calling token.

    Unauthenticated calls (no CLAUTHING_PLUGIN_TOKEN — e.g. direct CLI use) are
    allowed; a call carrying a token must have `perm` in its approved set. (The
    isolation is voluntary — a plugin could omit the token — but this stops an
    approved plugin from straying past what it asked for.)
    """
    token = os.environ.get("CLAUTHING_PLUGIN_TOKEN")
    if not token:
        return
    profile = _profile(_socket())
    entry = resolve_token(token, profile)
    if entry is None:
        print("error: unknown plugin token", file=sys.stderr)
        sys.exit(4)
    if perm not in entry.get("permissions", []):
        print(f"error: permission denied: {perm} "
              f"(plugin {entry.get('name')!r} not approved for it)", file=sys.stderr)
        sys.exit(4)


def _approve_popup(sock, name, permissions):
    perms = "\n".join(f"    - {p}" for p in permissions) or "    (none)"
    script = (
        "clear; echo; "
        f"echo \"Plugin '{name}' requests permissions:\"; echo; "
        f"printf '%s\\n' {shlex.quote(perms)}; echo; "
        "echo '  [Enter] approve   [q] deny'; "
        "read -n1 k; [ \"$k\" = q ] && exit 1; exit 0"
    )
    r = subprocess.run(
        ["tmux", "-L", sock, "display-popup", "-E", "-w", "60%", "-h", "40%",
         "bash", "-c", script],
        capture_output=True,
    )
    return r.returncode == 0


def cmd_ping(args):
    """Identify the plugin + request permissions; return its token.

    First sight of a plugin → approval popup, then record it in the plugin
    directory and issue a token. Known plugin → permissions must match the
    pinned set (else re-approval is needed); return the stored token.
    """
    sock = _socket()
    profile = _profile(sock)
    name = args.name
    requested = sorted({p.strip() for p in (args.permissions or "").split(",") if p.strip()})

    entry = load_plugin(name, profile)
    if entry is not None:
        if not permissions_match(entry, requested):
            print(f"error: {name!r} permissions changed "
                  f"(approved {entry.get('permissions')}, requested {requested}); "
                  f"re-approval needed", file=sys.stderr)
            sys.exit(3)
        print(entry["token"])
        return

    if not _approve_popup(sock, name, requested):
        print(f"error: {name!r} denied", file=sys.stderr)
        sys.exit(1)
    token = issue_token()
    save_plugin(name, requested, token, profile)
    print(token)


def cmd_windows(args):
    _require("windows")
    sock = _socket()
    r = _tmux(sock, "list-windows", "-F", _WINDOW_FORMAT, check=False)
    wins = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 6:
            try:
                wins.append(_window_record(parts))
            except (ValueError, IndexError):
                continue
    print(json.dumps(wins, indent=2))


def cmd_open(args):
    _require("window:open")
    sock = _socket()
    clauthing = shutil.which("clauthing") or "clauthing"
    inner = [clauthing]
    prof = _profile(sock)
    if prof:
        inner += ["--profile", prof]
    inner += ["--new-claude"]
    if args.resume:
        inner += ["--resume-session", args.resume]
    inner_str = " ".join(shlex.quote(p) for p in inner)
    new_window = ["new-window"]
    if args.cwd:
        new_window += ["-c", args.cwd]
    if args.name:
        new_window += ["-n", args.name]
    new_window += [inner_str]
    _tmux(sock, *new_window)
    print(f"opened (resume {args.resume or 'fresh'})")


def cmd_close(args):
    _require("window:close")
    sock = _socket()
    wid = _resolve_window(sock, args.window)
    if wid is None:
        print(f"error: no window matching {args.window!r}", file=sys.stderr)
        sys.exit(1)
    # Drop from open-sessions too (so a restart won't double-restore it).
    sess = _tmux(sock, "display-message", "-p", "-t", wid, "#{@session_id}",
                 check=False).stdout.strip()
    if sess:
        try:
            from clauthing.session import remove_open_session
            remove_open_session(sess, _profile(sock))
        except Exception:
            pass
    _tmux(sock, "kill-window", "-t", wid, check=False)
    print(f"closed {args.window}")


def cmd_select(args):
    sock = _socket()
    wid = _resolve_window(sock, args.window)
    if wid is None:
        print(f"error: no window matching {args.window!r}", file=sys.stderr)
        sys.exit(1)
    _tmux(sock, "select-window", "-t", wid)


def main():
    p = argparse.ArgumentParser(prog="clauthing-api",
                                description="API for clauthing plugins.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("ping", help="identify + request permissions; returns a token")
    sp.add_argument("--name", required=True)
    sp.add_argument("--permissions", default="", help="comma-separated, e.g. windows,window:open")
    sp.set_defaults(func=cmd_ping)
    sub.add_parser("windows", help="list windows as JSON").set_defaults(func=cmd_windows)
    sp = sub.add_parser("open", help="open/resume a window")
    sp.add_argument("--resume", metavar="SESSION_ID")
    sp.add_argument("--cwd")
    sp.add_argument("--name")
    sp.set_defaults(func=cmd_open)
    sp = sub.add_parser("close", help="close a window (id|name|clauthing_window)")
    sp.add_argument("window")
    sp.set_defaults(func=cmd_close)
    sp = sub.add_parser("select", help="focus a window (id|name|clauthing_window)")
    sp.add_argument("window")
    sp.set_defaults(func=cmd_select)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
