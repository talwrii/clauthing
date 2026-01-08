#!/usr/bin/env python3
"""Window management utilities for kitty-claude."""
import os
import json
import subprocess
from kitty_claude.logging import run
from pathlib import Path


def find_and_focus_window():
    """Try to find and focus existing kitty-claude window using xdotool."""
    try:
        result = run(
            ["xdotool", "search", "--class", "kitty-claude"],
            capture_output=True,
            text=True
        )

        window_ids = result.stdout.strip().split('\n')
        if window_ids and window_ids[0]:
            window_id = window_ids[0]
            run(["xdotool", "windowactivate", window_id])
            print(f"Focused existing kitty-claude window")
            return True

        return False

    except FileNotFoundError:
        print("Warning: xdotool not found. Install with: sudo apt install xdotool")
        return False
    except Exception as e:
        print(f"Warning: Could not search for window: {e}")
        return False


def open_session_notes(get_runtime_tmux_state_file):
    """Open session notes in vim via tmux popup.

    Args:
        get_runtime_tmux_state_file: Function to get the runtime state file path
    """
    profile = os.environ.get('KITTY_CLAUDE_PROFILE')
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    state_file = get_runtime_tmux_state_file(profile)

    # Get current window index
    try:
        result = run(
            ["tmux", "-L", "kitty-claude", "display-message", "-p", "#{window_index}"],
            capture_output=True,
            text=True,
            check=True
        )
        window_index = result.stdout.strip()
    except:
        run(
            ["tmux", "-L", "kitty-claude", "display-message", "Could not get window index"],
            stderr=subprocess.DEVNULL
        )
        return

    # Load state to get session ID
    if not state_file.exists():
        run(
            ["tmux", "-L", "kitty-claude", "display-message", "No session found"],
            stderr=subprocess.DEVNULL
        )
        return

    try:
        state = json.loads(state_file.read_text())
        windows = state.get("windows", {})
        window_data = windows.get(window_index)

        if not window_data:
            run(
                ["tmux", "-L", "kitty-claude", "display-message", "No session data for this window"],
                stderr=subprocess.DEVNULL
            )
            return

        session_id = window_data.get("session_id")
        if not session_id:
            run(
                ["tmux", "-L", "kitty-claude", "display-message", "No session ID found"],
                stderr=subprocess.DEVNULL
            )
            return

        # Create notes file path
        notes_dir = config_dir / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        notes_file = notes_dir / f"{session_id}.md"

        # Open vim in tmux popup
        run([
            "tmux", "-L", "kitty-claude",
            "display-popup", "-E", "-w", "80%", "-h", "80%",
            f"vim {notes_file}"
        ])

    except Exception as e:
        run(
            ["tmux", "-L", "kitty-claude", "display-message", f"Error opening notes: {str(e)}"],
            stderr=subprocess.DEVNULL
        )
