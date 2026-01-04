#!/usr/bin/env python3
# kitty-claude
import os
import sys
import shutil
import subprocess
import argparse
import json
from pathlib import Path

def send_tmux_message(message):
    """Send a message via tmux display-message"""
    try:
        subprocess.run([
            "tmux", "-L", "kitty-claude",
            "display-message", message
        ], stderr=subprocess.DEVNULL)
    except:
        pass

def handle_user_prompt_submit(claude_data_dir=None):
    """Handle UserPromptSubmit hook - process custom commands like :cd"""
    try:
        # Get claude data dir from environment variable if not provided
        if claude_data_dir is None:
            config_env = os.environ.get('CLAUDE_CONFIG_DIR')
            if config_env:
                claude_data_dir = Path(config_env)
            else:
                # Fallback to default
                claude_data_dir = Path.home() / ".config" / "kitty-claude" / "claude-data"
        
        # Read JSON from stdin
        input_data = json.loads(sys.stdin.read())
        prompt = input_data.get('prompt', '').strip()
        
        # Check for :cd command
        if prompt.startswith(':cd '):
            target_dir = prompt[4:].strip()
            current_dir = input_data.get('cwd', os.getcwd())
            
            # Encode paths
            encoded_current = current_dir.replace('/', '-')
            encoded_target = target_dir.replace('/', '-')
            
            # Find current session
            projects_dir = claude_data_dir / "projects" / encoded_current
            if not projects_dir.exists():
                send_tmux_message("❌ No session found in current directory")
                response = {
                    "continue": False,
                    "stopReason": "❌ No session found in current directory"
                }
                print(json.dumps(response))
                return
            
            session_files = sorted(projects_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not session_files:
                send_tmux_message("❌ No session found in current directory")
                response = {
                    "continue": False,
                    "stopReason": "❌ No session found in current directory"
                }
                print(json.dumps(response))
                return
            
            session_id = session_files[0].stem
            
            # Clone session
            target_projects_dir = claude_data_dir / "projects" / encoded_target
            target_projects_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(session_files[0], target_projects_dir / f"{session_id}.jsonl")
            
            # Open new tmux window
            subprocess.run([
                "tmux", "-L", "kitty-claude",
                "new-window", "-c", target_dir,
                f"claude --resume {session_id}"
            ])
            
            send_tmux_message(f"✓ Opened new window in {target_dir}")
            response = {
                "continue": False,
                "stopReason": f"✓ Opened new window in {target_dir}"
            }
            print(json.dumps(response))
            return
        
        # Not a custom command, pass through
        print(prompt)
        
    except Exception as e:
        # Log error and send notification
        error_msg = f"Hook error: {str(e)}"
        send_tmux_message(f"❌ {error_msg}")
        with open("/tmp/kitty-claude-hook-error.log", "a") as f:
            f.write(f"{error_msg}\n")
        # Pass through the original prompt on error
        try:
            input_data = json.loads(sys.stdin.read()) if 'input_data' not in locals() else input_data
            print(input_data.get('prompt', ''))
        except:
            pass

def setup_claude_config(config_dir):
    """Set up isolated Claude Code configuration on first run."""
    claude_data_dir = config_dir / "claude-data"
    commands_dir = claude_data_dir / "commands"
    
    # Create directories
    commands_dir.mkdir(parents=True, exist_ok=True)
    
    # Symlink credentials from main Claude config if they exist
    main_credentials = Path.home() / ".claude" / ".credentials.json"
    isolated_credentials = claude_data_dir / ".credentials.json"
    
    if main_credentials.exists() and not isolated_credentials.exists():
        try:
            isolated_credentials.symlink_to(main_credentials)
            print(f"Linked credentials from {main_credentials}")
        except Exception as e:
            print(f"Warning: Could not link credentials: {e}")
    
    # Create settings.json with UserPromptSubmit hook
    settings_file = claude_data_dir / "settings.json"
    if not settings_file.exists():
        # Get the kitty-claude executable path
        kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
        
        settings_file.write_text(json.dumps({
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{kitty_claude_path} --user-prompt-submit"
                            }
                        ]
                    }
                ]
            }
        }, indent=2))
        print(f"Created settings with UserPromptSubmit hook at {settings_file}")
    
    return claude_data_dir

def setup_jail_directory():
    """Create and return the jail directory path."""
    uid = os.getuid()
    jail_dir = Path(f"/var/run/{uid}/kitty-claude")
    
    # Create the jail directory if it doesn't exist
    try:
        jail_dir.mkdir(parents=True, exist_ok=True)
        print(f"Jail directory: {jail_dir}")
    except PermissionError:
        # Fallback to /tmp if /var/run/$UID doesn't work
        jail_dir = Path(f"/tmp/kitty-claude-{uid}")
        jail_dir.mkdir(parents=True, exist_ok=True)
        print(f"Using fallback jail directory: {jail_dir}")
    
    return jail_dir

def reinstall(config_dir):
    """Remove all kitty-claude config except credentials."""
    claude_data_dir = config_dir / "claude-data"
    credentials_file = claude_data_dir / ".credentials.json"
    
    # Backup credentials if it's a real file (not a symlink)
    credentials_backup = None
    if credentials_file.exists() and not credentials_file.is_symlink():
        credentials_backup = credentials_file.read_bytes()
        print(f"Backed up credentials")
    
    # Remove entire config directory
    if config_dir.exists():
        print(f"Removing {config_dir}...")
        shutil.rmtree(config_dir)
        print("✓ Removed kitty-claude configuration")
    
    # Restore credentials if we backed them up
    if credentials_backup:
        claude_data_dir.mkdir(parents=True, exist_ok=True)
        credentials_file.write_bytes(credentials_backup)
        print(f"✓ Restored credentials")
    
    print("\nReinstall complete! Run 'kitty-claude' to recreate configuration.")

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
    parser = argparse.ArgumentParser(description="Launch Claude Code in isolated kitty+tmux environment")
    parser.add_argument("--reinstall", action="store_true", help="Remove all config except credentials and exit")
    parser.add_argument("--user-prompt-submit", action="store_true", help="Handle UserPromptSubmit hook (internal use)")
    args = parser.parse_args()
    
    config_dir = Path.home() / ".config" / "kitty-claude"
    claude_data_dir = config_dir / "claude-data"
    
    # Handle user prompt submit hook
    if args.user_prompt_submit:
        handle_user_prompt_submit()
        sys.exit(0)
    
    # Handle reinstall command
    if args.reinstall:
        reinstall(config_dir)
        sys.exit(0)
    
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
    kitty_config_path = config_dir / "kitty.conf"
    tmux_config_path = config_dir / "tmux.conf"
    
    # Set up isolated Claude config
    claude_data_dir = setup_claude_config(config_dir)
    
    # Set up jail directory
    jail_dir = setup_jail_directory()
    
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
# Bind C-n directly (no prefix) to open new window with claude in jail
bind -n C-n new-window -c "{jail_dir}" claude
# Also override default C-b c
bind c new-window -c "{jail_dir}" claude
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
            f"shell tmux -L kitty-claude -f {tmux_config_path} new-session -As kitty-claude -c {jail_dir} claude\n"
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