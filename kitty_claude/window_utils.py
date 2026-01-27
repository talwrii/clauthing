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


def open_session_notes(get_runtime_tmux_state_file, session_id=None):
    """Open session notes in vim via tmux popup.

    Args:
        get_runtime_tmux_state_file: Function to get the runtime state file path
        session_id: Optional session ID. If not provided, will try to find from state file
    """
    # Get the actual tmux socket from environment
    socket = os.environ.get('KITTY_CLAUDE_TMUX_SOCKET', 'kitty-claude')

    profile = os.environ.get('KITTY_CLAUDE_PROFILE')
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"

    # If session_id is provided, use it directly
    if session_id:
        notes_dir = config_dir / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        notes_file = notes_dir / f"{session_id}.md"

        # Open vim in tmux popup
        run([
            "tmux", "-L", socket,
            "display-popup", "-E", "-w", "80%", "-h", "80%",
            f"vim {notes_file}"
        ])
        return

    # Otherwise, try to find session_id from state file
    state_file = get_runtime_tmux_state_file(profile)

    # Get current window index
    try:
        result = run(
            ["tmux", "-L", socket, "display-message", "-p", "#{window_index}"],
            capture_output=True,
            text=True,
            check=True
        )
        window_index = result.stdout.strip()
    except:
        run(
            ["tmux", "-L", socket, "display-message", "Could not get window index"],
            stderr=subprocess.DEVNULL
        )
        return

    # Load state to get session ID
    if not state_file.exists():
        # Try to find session ID from running Claude process in current pane (for one-tab mode)
        try:
            # Get the PID of the process in the current pane
            result = run(
                ["tmux", "-L", socket, "display-message", "-p", "#{pane_pid}"],
                capture_output=True,
                text=True,
                check=True
            )
            pane_pid = result.stdout.strip()

            # First check if the pane process itself is Claude
            result = run(
                ["ps", "-p", pane_pid, "-o", "args="],
                capture_output=True,
                text=True
            )
            cmdline = result.stdout.strip()
            if 'claude' in cmdline and '--resume' in cmdline:
                parts = cmdline.split()
                resume_idx = parts.index('--resume')
                if resume_idx + 1 < len(parts):
                    session_id = parts[resume_idx + 1]
                    notes_dir = config_dir / "notes"
                    notes_dir.mkdir(parents=True, exist_ok=True)
                    notes_file = notes_dir / f"{session_id}.md"

                    run([
                        "tmux", "-L", socket,
                        "display-popup", "-E", "-w", "80%", "-h", "80%",
                        f"vim {notes_file}"
                    ])
                    return

            # If not, check child processes
            result = run(
                ["pgrep", "-P", pane_pid],
                capture_output=True,
                text=True
            )
            child_pids = result.stdout.strip().split('\n') if result.stdout.strip() else []

            # Check if any child is a Claude process with --resume
            for pid in child_pids:
                result = run(
                    ["ps", "-p", pid, "-o", "args="],
                    capture_output=True,
                    text=True
                )
                cmdline = result.stdout.strip()
                if 'claude' in cmdline and '--resume' in cmdline:
                    parts = cmdline.split()
                    resume_idx = parts.index('--resume')
                    if resume_idx + 1 < len(parts):
                        session_id = parts[resume_idx + 1]
                        notes_dir = config_dir / "notes"
                        notes_dir.mkdir(parents=True, exist_ok=True)
                        notes_file = notes_dir / f"{session_id}.md"

                        run([
                            "tmux", "-L", socket,
                            "display-popup", "-E", "-w", "80%", "-h", "80%",
                            f"vim {notes_file}"
                        ])
                        return
        except:
            pass

        run(
            ["tmux", "-L", socket, "display-message", "No session found"],
            stderr=subprocess.DEVNULL
        )
        return

    try:
        state = json.loads(state_file.read_text())
        windows = state.get("windows", {})
        window_data = windows.get(window_index)

        if not window_data:
            run(
                ["tmux", "-L", socket, "display-message", "No session data for this window"],
                stderr=subprocess.DEVNULL
            )
            return

        session_id = window_data.get("session_id")
        if not session_id:
            run(
                ["tmux", "-L", socket, "display-message", "No session ID found"],
                stderr=subprocess.DEVNULL
            )
            return

        # Create notes file path
        notes_dir = config_dir / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        notes_file = notes_dir / f"{session_id}.md"

        # Open vim in tmux popup
        run([
            "tmux", "-L", socket,
            "display-popup", "-E", "-w", "80%", "-h", "80%",
            f"vim {notes_file}"
        ])

    except Exception as e:
        run(
            ["tmux", "-L", socket, "display-message", f"Error opening notes: {str(e)}"],
            stderr=subprocess.DEVNULL
        )
