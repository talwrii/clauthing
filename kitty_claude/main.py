#!/usr/bin/env python3
# kitty-claude
import os
import sys
import shutil
import subprocess
from pathlib import Path

def find_and_focus_window():
    """Try to find and focus existing kitty-claude window using xdotool."""
    try:
        result = subprocess.run(
            ["xdotool", "search", "--class", "kitty-claude"],
            capture_output=True,
            text=True
        )
        
        window_ids = result.stdout.strip().split('\n')
        if window_ids and window_ids[0]:
            window_id = window_ids[0]
            subprocess.run(["xdotool", "windowactivate", window_id])
            print(f"Focused existing kitty-claude window")
            return True
        
        return False
        
    except FileNotFoundError:
        print("Warning: xdotool not found. Install with: sudo apt install xdotool")
        return False
    except Exception as e:
        print(f"Warning: Could not search for window: {e}")
        return False

def main():
    # Check if tmux exists
    if not shutil.which("tmux"):
        print("Error: tmux not found. Please install tmux first.")
        sys.exit(1)
    
    # Check if kitty exists
    if not shutil.which("kitty"):
        print("Error: kitty not found. Please install kitty first.")
        sys.exit(1)
    
    # Try to find and focus existing window
    if find_and_focus_window():
        sys.exit(0)
    
    # Window doesn't exist, create config and launch
    config_dir = Path.home() / ".config" / "kitty-claude"
    kitty_config_path = config_dir / "kitty.conf"
    tmux_config_path = config_dir / "tmux.conf"
    
    # Create config dir if it doesn't exist
    config_dir.mkdir(parents=True, exist_ok=True)
    
    # Create tmux config if it doesn't exist
    if not tmux_config_path.exists():
        tmux_config_path.write_text("""\
# kitty-claude tmux config (isolated server)

# Kill session when kitty window closes
set -g destroy-unattached on

# Default command is claude
set -g default-command "claude"

# Bind C-n directly (no prefix) to open new window with claude
bind -n C-n new-window claude

# Also override default C-b c
bind c new-window claude

# C-w closes current window, but not the last one
bind -n C-w if-shell "[ $(tmux list-windows | wc -l) -gt 1 ]" "kill-window" "display-message 'Cannot close last window'"

# C-v passthrough for paste
bind -n C-v send-keys C-v

# Some sensible defaults
set -g mouse on
set -g history-limit 10000
set -g base-index 1
setw -g pane-base-index 1

# Easier window switching
bind -n C-j previous-window
bind -n C-k next-window
""")
        print(f"Created tmux config at {tmux_config_path}")
    
    # Create kitty config if it doesn't exist
    if not kitty_config_path.exists():
        kitty_config_path.write_text(
            f"include {Path.home()}/.config/kitty/kitty.conf\n"
            f"shell tmux -L kitty-claude -f {tmux_config_path} new-session -As kitty-claude claude\n"
        )
        print(f"Created kitty config at {kitty_config_path}")
    
    # Launch kitty
    os.execvp("kitty", [
        "kitty",
        "--class=kitty-claude",
        f"--config={kitty_config_path}"
    ])

if __name__ == "__main__":
    main()