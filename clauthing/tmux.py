#!/usr/bin/env python3
"""Tmux operations for clauthing."""
import os
import subprocess
from pathlib import Path
from clauthing.logging import run

def send_tmux_message(message, socket="clauthing"):
    """Send a message via tmux display-message"""
    try:
        run([
            "tmux", "-L", socket,
            "display-message", message
        ], stderr=subprocess.DEVNULL)
    except:
        pass


def focus_mcp_origin(socket):
    """Switch the tmux client to the window where this MCP server lives.

    tmux's display-popup is client-relative: it always renders on whatever
    window the client is currently showing, regardless of -t. So if a popup
    fires from claude-3 (tmux window 3) while the user is on window 5, the
    popup lands on window 5 — describing a tool call the user wasn't even
    looking at. We work around it by selecting the originating window first.

    The MCP server inherits $TMUX_PANE from claude (which tmux sets to the
    pane id where claude is running). No-op if $TMUX_PANE is unset.

    Returns the window ID that was active before the switch, so callers can
    restore it after the popup closes.

    TODO: drop this once we move off tmux (see todos.md — Zellij has
    window-attached floating panes that don't need this dance).
    """
    # Record the currently active window so callers can switch back after.
    prev_window = None
    try:
        r = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "#{window_id}"],
            capture_output=True, text=True, timeout=2,
        )
        prev_window = r.stdout.strip() or None
    except Exception:
        pass

    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return prev_window
    try:
        subprocess.run(
            ["tmux", "-L", socket, "select-window", "-t", pane],
            capture_output=True, timeout=2,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return prev_window

def get_window_and_pane_for_session(socket, session_id):
    """Look up (window_id, pane_id) for the tmux window running session_id.

    Returns (window_id, pane_id) or (None, None) if not found.
    """
    try:
        result = subprocess.run(
            ["tmux", "-L", socket, "list-windows", "-F",
             "#{window_id}\t#{pane_id}\t#{@session_id}"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split('\t', 2)
            if len(parts) == 3 and parts[2] == session_id:
                return parts[0], parts[1]
    except Exception:
        pass
    return None, None


def log_window_snapshot(socket, label, profile=None):
    """Log every window's index/name/window_id/pane/@session_id/@clauthing_window.

    A point-in-time picture of the tmux layout for debugging window/pane
    targeting (e.g. when :cd lands on the wrong window). Greppable as WINSNAP.
    """
    from clauthing.logging import log
    try:
        r = subprocess.run(
            ["tmux", "-L", socket, "list-windows", "-F",
             "  #{window_index} #{window_name} win=#{window_id} pane=#{pane_id} "
             "sid=#{@session_id} cw=#{@clauthing_window}"],
            capture_output=True, text=True, timeout=5,
        )
        log(f"WINSNAP [{label}]:\n{r.stdout.rstrip() or '  (none)'}", profile)
    except Exception as e:
        log(f"WINSNAP [{label}] failed: {e}", profile)


def backfill_clauthing_windows(socket):
    """Ensure every live window on `socket` that runs a claude session has an
    @clauthing_window option set.

    Windows created before clauthing_window existed (or after an in-place
    upgrade) carry @session_id but no @clauthing_window. This mints/derives the
    stable id from session metadata and sets the option, so window-scoped state
    (messages, attention, notes) keys correctly. Idempotent. Returns the number
    of windows updated.
    """
    import uuid
    from clauthing.session import ensure_clauthing_window, set_clauthing_window
    try:
        result = subprocess.run(
            ["tmux", "-L", socket, "list-windows", "-F",
             "#{window_id}\t#{@session_id}\t#{@clauthing_window}"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return 0
    updated = 0
    seen = set()   # clauthing_windows already claimed by an earlier window
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        win_id = parts[0]
        sid = parts[1]
        cw = parts[2] if len(parts) > 2 else ""
        if not sid:
            continue
        new_cw = None
        if not cw:
            new_cw = ensure_clauthing_window(sid)            # missing → derive/mint
        elif cw in seen:
            new_cw = str(uuid.uuid4())                       # duplicate → re-mint
            set_clauthing_window(sid, new_cw)
        else:
            seen.add(cw)
            continue                                         # already unique
        if new_cw:
            try:
                subprocess.run(
                    ["tmux", "-L", socket, "set-option", "-w", "-t", win_id,
                     "@clauthing_window", new_cw],
                    capture_output=True, timeout=5,
                )
                seen.add(new_cw)
                updated += 1
            except Exception:
                pass
    return updated


def get_runtime_tmux_state_file(profile=None):
    """Get the runtime tmux state file path (for window restoration)."""
    uid = os.getuid()
    # Try /var/run first
    try:
        runtime_dir = Path(f"/var/run/{uid}/clauthing")
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # Fallback to /tmp
        runtime_dir = Path(f"/tmp/clauthing-{uid}")
        runtime_dir.mkdir(parents=True, exist_ok=True)
    
    # Use profile-specific state file if profile is set
    if profile:
        return runtime_dir / f"tmux-state-{profile}.json"
    else:
        return runtime_dir / "tmux-state.json"