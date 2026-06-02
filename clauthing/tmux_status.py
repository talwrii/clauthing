#!/usr/bin/env python3
"""Tmux status bar window display."""
import json
import subprocess
import sys
from pathlib import Path


def _unread_counts(profile=None):
    """Return {clauthing_window: unread_count} for windows with pending messages.

    Inboxes are keyed by clauthing_window, so the inbox stem is the window id.
    """
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
        # Single tmux call: active flag, client width, index, name, session id.
        # client_width is a client property but tmux expands it per-window too.
        result = subprocess.run(
            ["tmux", "-L", socket, "list-windows", "-F",
             "#{window_active}\t#{client_width}\t#{window_index}\t#{window_name}\t#{@clauthing_window}"],
            capture_output=True,
            text=True
        )
        raw_windows = result.stdout.strip().split('\n')

        width = 80
        current = None
        windows = []
        for line in raw_windows:
            parts = line.split('\t', 4)
            if len(parts) < 5:
                continue
            active, w, idx, name, cw = parts
            if active == '1':
                current = idx
                try:
                    width = int(w)
                except ValueError:
                    pass
            windows.append(f"{idx}\t{name}\t{cw}")

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