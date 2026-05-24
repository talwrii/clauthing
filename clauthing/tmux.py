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