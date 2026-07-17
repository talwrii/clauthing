"""Linked-tmux-window feature — self-contained so it could move to a plugin.

A clauthing window can "link" a window on the user's DEFAULT tmux server
(`tmux -L default`) so claude can switch to it, read it, or spawn/kill it.

The link is **window-scoped**: keyed by `clauthing_window` and stored in the
per-window state file. Because clauthing_window is stable across :cd, the link
survives :cd automatically — no carry-over needed.

Everything here is plain (clauthing_window, profile) → (ok, message). The only
clauthing dependencies are the per-window state store (swappable) and tmux on
the PATH, so this whole module could be lifted into a plugin.
"""
import subprocess
from pathlib import Path

from clauthing.session import load_window_state, save_window_state

# The user's own tmux server (the one they drive directly), distinct from the
# clauthing-owned server.
DEFAULT_SERVER = "default"

# Per-window-state keys.
_KEY_ONE = "linked_tmux_window"     # single linked window id (@N)
_KEY_LIST = "linked_tmux_windows"   # list of {id, name}


def _tmux(*args, check=True):
    return subprocess.run(
        ["tmux", "-L", DEFAULT_SERVER, *args],
        capture_output=True, text=True, check=check, timeout=10,
    )


# ── storage ──────────────────────────────────────────────────────────────────

def get_linked_window(cw, profile=None):
    """Return the linked window id for a clauthing_window, or None."""
    if not cw:
        return None
    return load_window_state(cw, profile).get(_KEY_ONE)


def set_linked_window(cw, profile, wid):
    state = load_window_state(cw, profile)
    state[_KEY_ONE] = wid
    save_window_state(cw, state, profile)


def clear_linked_window(cw, profile=None):
    state = load_window_state(cw, profile)
    if _KEY_ONE in state:
        del state[_KEY_ONE]
        save_window_state(cw, state, profile)
        return True
    return False


def get_linked_list(cw, profile=None):
    return load_window_state(cw, profile).get(_KEY_LIST, [])


# ── operations (each returns (ok: bool, message: str)) ───────────────────────

def current_default_path():
    """cwd of the focused window on the default server (no link needed)."""
    try:
        r = _tmux("display-message", "-p", "#{pane_current_path}")
        path = r.stdout.strip()
        if not path:
            return False, "❌ No path returned (no current window?)"
        return True, f"Current default-tmux window cwd: {path}"
    except subprocess.CalledProcessError as e:
        return False, f"❌ Could not get current path: {e.stderr or e}"


def toggle(cw, profile=None):
    """:tmux — switch to the linked window, or link the current one if none."""
    if not cw:
        return False, "❌ No window id"
    linked = get_linked_window(cw, profile)
    if linked:
        try:
            _tmux("select-window", "-t", linked)
            return True, f"✓ Switched to tmux window {linked}"
        except subprocess.CalledProcessError:
            return False, f"❌ Linked window {linked} not found - use :tmux-unlink to reset"
    try:
        r = _tmux("display-message", "-p", "#{window_id}:#{window_name}")
        wid, _, wname = r.stdout.strip().partition(":")
        set_linked_window(cw, profile, wid)
        return True, f"✓ Linked to tmux window '{wname or wid}' ({wid})"
    except subprocess.CalledProcessError:
        return False, "❌ Could not access default tmux server"


def focus(cw, profile=None):
    """:tmux-focus — switch to the linked window (never links a new one)."""
    if not cw:
        return False, "❌ No window id"
    linked = get_linked_window(cw, profile)
    if not linked:
        return False, "No tmux window linked (use :tmux to link one)"
    try:
        _tmux("select-window", "-t", linked)
        return True, f"✓ Focused tmux window {linked}"
    except subprocess.CalledProcessError:
        return False, f"❌ Linked window {linked} not found - use :tmux-unlink to reset"


def unlink(cw, profile=None):
    if not cw:
        return False, "❌ No window id"
    if clear_linked_window(cw, profile):
        return True, "✓ Unlinked tmux window"
    return False, "No tmux window linked"


def linked_path(cw, profile=None):
    linked = get_linked_window(cw, profile)
    if not linked:
        return False, "No tmux window linked. Use :tmux to link a window first."
    try:
        r = _tmux("display-message", "-p", "-t", linked, "#{pane_current_path}")
        path = r.stdout.strip()
        if not path:
            return False, "❌ Could not get path"
        return True, f"The linked tmux window ({linked}) is at: {path}"
    except subprocess.CalledProcessError:
        return False, f"❌ Linked window {linked} not found - use :tmux-unlink to reset"


def linked_screen(cw, profile=None):
    linked = get_linked_window(cw, profile)
    if not linked:
        return False, "No tmux window linked. Use :tmux to link a window first."
    try:
        r = _tmux("capture-pane", "-p", "-t", linked)
        lines = r.stdout.rstrip().split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        content = "\n".join(lines)
        if content:
            return True, f"Content of linked tmux window ({linked}):\n\n```\n{content}\n```"
        return True, f"Linked tmux window ({linked}) is empty."
    except subprocess.CalledProcessError:
        return False, f"❌ Linked window {linked} not found - use :tmux-unlink to reset"


def spawn(cw, profile, name_hint, shell_cmd=""):
    """Spawn a fresh default-server window owned by this clauthing window."""
    if not cw:
        return False, "❌ No window id"
    linked = get_linked_window(cw, profile)
    if linked:
        return False, f"❌ Already linked to {linked}. Use :tmux-unlink first."
    win_name = f"cl-{name_hint}" if name_hint else "cl-window"
    cmd = ["new-window", "-d", "-n", win_name, "-P", "-F", "#{window_id}"]
    if shell_cmd:
        cmd.append(shell_cmd)
    try:
        r = _tmux(*cmd)
        wid = r.stdout.strip()
        set_linked_window(cw, profile, wid)
        ran = f" running `{shell_cmd}`" if shell_cmd else ""
        return True, f"✓ Spawned and linked tmux window '{win_name}' ({wid}){ran}"
    except subprocess.CalledProcessError as e:
        return False, f"❌ Could not spawn window: {e.stderr or e}"


def kill(cw, profile=None):
    linked = get_linked_window(cw, profile)
    if not linked:
        return False, "No tmux window linked."
    try:
        _tmux("kill-window", "-t", linked, check=False)
    except Exception:
        pass  # window may already be gone — still clear the link
    clear_linked_window(cw, profile)
    return True, f"✓ Killed and unlinked tmux window {linked}"


def list_add(cw, profile=None):
    """:tmuxs-link — add the focused default-server window to the link list."""
    if not cw:
        return False, "❌ No window id"
    try:
        r = _tmux("display-message", "-p", "#{window_id}:#{window_name}")
        wid, _, wname = r.stdout.strip().partition(":")
        wname = wname or wid
        state = load_window_state(cw, profile)
        linked = state.get(_KEY_LIST, [])
        if not any(w["id"] == wid for w in linked):
            linked.append({"id": wid, "name": wname})
            state[_KEY_LIST] = linked
            save_window_state(cw, state, profile)
            return True, f"✓ Added tmux window '{wname}' ({wid})"
        return True, f"Already linked: '{wname}' ({wid})"
    except subprocess.CalledProcessError:
        return False, "❌ Could not access default tmux server"


def select_window(wid):
    """Switch the default server to window id `wid`."""
    try:
        _tmux("select-window", "-t", wid)
        return True, f"✓ Switched to {wid}"
    except subprocess.CalledProcessError:
        return False, f"❌ Window {wid} not found"
