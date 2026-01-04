#!/usr/bin/env python3
# kitty-claude
import os
import sys
import shutil
import subprocess
from pathlib import Path

def setup_claude_config(config_dir):
    """Set up isolated Claude Code configuration on first run."""
    claude_data_dir = config_dir / "claude-data"
    commands_dir = claude_data_dir / "commands"
    
    # Create directories
    commands_dir.mkdir(parents=True, exist_ok=True)
    
    # Create /cd command if it doesn't exist
    cd_command_path = commands_dir / "cd.md"
    if not cd_command_path.exists():
        cd_command_path.write_text("""\
You are being asked to change the working directory for this Claude Code session.

Steps to execute:
1. Get the current session ID from the system context
2. Determine the current working directory 
3. Take the target directory from the user's command (the argument after /cd)
4. Clone the current session to the target directory:
   - Encode both current and target paths (replace / with -)
   - Create $CLAUDE_CONFIG_DIR/projects/[encoded-target]/ if needed
   - Copy the session .jsonl file from current to target
5. Open a new tmux window in the kitty-claude server:
   - Use: `tmux -L kitty-claude new-window -c [target-dir] "claude --resume [session-id]"`
6. Confirm to the user that the new window opened

Execute these steps using bash commands. Be concise in your response.

Example usage by user: /cd /home/user/other-project
""")
        print(f"Created /cd command at {cd_command_path}")
    
    return claude_data_dir

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
    
    # Check if claude exists
    if not shutil.which("claude"):
        print("Error: claude not found. Please install Claude Code first.")
        sys.exit(1)
    
    # Try to find and focus existing window
    if find_and_focus_window():
        sys.exit(0)
    
    # Window doesn't exist, create config and launch
    config_dir = Path.home() / ".config" / "kitty-claude"
    kitty_config_path = config_dir / "kitty.conf"
    tmux_config_path = config_dir / "tmux.conf"
    
    # Set up isolated Claude config
    claude_data_dir = setup_claude_config(config_dir)
    
    # Create config dir if it doesn't exist
    config_dir.mkdir(parents=True, exist_ok=True)
    
    # Create tmux config if it doesn't exist
    if not tmux_config_path.exists():
        tmux_config_path.write_text(f"""\
# kitty-claude tmux config (isolated server)
# Kill session when kitty window closes
set -g destroy-unattached on
# Set CLAUDE_CONFIG_DIR for isolated Claude data
set-environment -g CLAUDE_CONFIG_DIR "{claude_data_dir}"
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