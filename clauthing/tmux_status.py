#!/usr/bin/env python3
"""Tmux status bar window display."""
import json
import subprocess
import sys
from pathlib import Path


def _unread_counts(profile=None):
    """Return {session_id: unread_count} for sessions with pending messages."""
    try:
        from clauthing.events import get_runtime_dir
        msgs_dir = get_runtime_dir(profile) / "messages"
    except Exception:
        return {}
    if not msgs_dir.exists():
        return {}
    counts = {}
    for inbox in msgs_dir.glob("*.jsonl"):
        try:
            n = 0
            for line in inbox.read_text().splitlines():
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if not msg.get("read"):
                    n += 1
            if n:
                counts[inbox.stem] = n
        except Exception:
            continue
    return counts


def get_window_display(line_num, socket="clauthing", profile=None):
    """Get formatted window list for the specified line.

    Args:
        line_num: 1 or 2 - which line to display
        socket: Tmux socket name
        profile: clauthing profile (for messages dir lookup)
    """
    try:
        # Get terminal width
        result = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "#{client_width}"],
            capture_output=True,
            text=True
        )
        width = int(result.stdout.strip())

        # Get current window
        result = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "#{window_index}"],
            capture_output=True,
            text=True
        )
        current = result.stdout.strip()

        # Get all windows (with session id, so we can mark inbox status)
        result = subprocess.run(
            ["tmux", "-L", socket, "list-windows", "-F",
             "#{window_index}\t#{window_name}\t#{@session_id}"],
            capture_output=True,
            text=True
        )
        windows = result.stdout.strip().split('\n')

        unread = _unread_counts(profile)

        # Format windows and split across two lines
        line1 = ""
        line2 = ""
        line_width = 0
        current_line = 1

        for window in windows:
            if not window:
                continue

            parts = window.split('\t')
            if len(parts) < 2:
                continue
            idx = parts[0]
            name = parts[1]
            sess_id = parts[2] if len(parts) > 2 else ""

            count = unread.get(sess_id, 0) if sess_id else 0
            suffix = f" ({count})" if count else ""
            label = f"{idx}:{name}{suffix}"

            # Format with styling
            if idx == current:
                formatted = f"#[bg=colour39,fg=colour235,bold] {label} #[default]"
            else:
                formatted = f"#[bg=colour235,fg=colour248] {label} #[default]"

            # Calculate visible width (approximate - ignoring tmux format codes)
            visible_width = len(label) + 2

            # Check if it fits on current line
            if line_width + visible_width < width:
                if current_line == 1:
                    line1 += formatted
                else:
                    line2 += formatted
                line_width += visible_width
            else:
                # Move to line 2
                current_line = 2
                line2 += formatted
                line_width = visible_width
        
        # Return requested line
        if line_num == 1:
            print(line1)
        else:
            print(line2)
    
    except Exception as e:
        # Silent failure - don't break tmux status bar
        print("")

def handle_tmux_status(line_num, profile=None):
    """Handle --tmux-status command."""
    if profile:
        socket = f"clauthing-{profile}"
    else:
        socket = "clauthing"

    get_window_display(line_num, socket, profile=profile)