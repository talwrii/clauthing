#!/usr/bin/env python3
# kitty-claude
import os
import sys
import shutil
import subprocess
import argparse
import json
import uuid
import shlex
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
    """Handle UserPromptSubmit hook - process custom commands like :cd and :fork"""
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
        
        # Check for :fork command
        if prompt.startswith(':fork'):
            current_dir = input_data.get('cwd', os.getcwd())
            session_id = input_data.get('session_id')
            
            # Encode path
            encoded_current = current_dir.replace('/', '-')
            
            # Find current session file
            projects_dir = claude_data_dir / "projects" / encoded_current
            if not projects_dir.exists():
                send_tmux_message("❌ No session found")
                response = {"continue": False, "stopReason": "❌ No session found"}
                print(json.dumps(response))
                return
            
            session_files = sorted(projects_dir.glob("*.jsonl"), 
                                 key=lambda p: p.stat().st_mtime, reverse=True)
            if not session_files:
                send_tmux_message("❌ No session found")
                response = {"continue": False, "stopReason": "❌ No session found"}
                print(json.dumps(response))
                return
            
            # Generate new fork session ID
            fork_session_id = str(uuid.uuid4())
            
            # Clone session to fork
            fork_file = projects_dir / f"{fork_session_id}.jsonl"
            shutil.copy2(session_files[0], fork_file)
            
            send_tmux_message("🔀 Opening fork in popup...")
            
            # Open fork in popup (blocking call)
            subprocess.run([
                "tmux", "-L", "kitty-claude",
                "display-popup", "-E", "-w", "90%", "-h", "90%",
                f"claude --resume {fork_session_id}"
            ])
            
            # Popup closed - get last assistant message from fork
            try:
                last_message = None
                with open(fork_file, 'r') as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                            if entry.get('type') == 'assistant':
                                message = entry.get('message', {})
                                content = message.get('content', [])
                                # Extract text from content blocks
                                text_parts = [
                                    block.get('text', '') 
                                    for block in content 
                                    if isinstance(block, dict) and block.get('type') == 'text'
                                ]
                                if text_parts:
                                    last_message = '\n'.join(text_parts)
                        except json.JSONDecodeError:
                            continue
                
                if last_message:
                    send_tmux_message("✓ Fork completed, injecting response")
                    
                    # Escape the message for shell safety
                    fork_message = f"Fork result:\n\n{last_message}"
                    escaped_message = shlex.quote(fork_message)
                    
                    # Background process: sleep then type the fork result
                    subprocess.Popen([
                        "sh", "-c",
                        f"sleep 0.5 && tmux -L kitty-claude send-keys -l {escaped_message} && tmux -L kitty-claude send-keys Enter"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    # Return immediately to unblock the hook
                    response = {"continue": False, "stopReason": ""}
                    print(json.dumps(response))
                else:
                    send_tmux_message("⚠ Fork had no assistant messages")
                    response = {"continue": False, "stopReason": "Fork had no responses"}
                    print(json.dumps(response))
                
            except Exception as e:
                send_tmux_message(f"❌ Error reading fork: {str(e)}")
                response = {"continue": False, "stopReason": f"Fork error: {str(e)}"}
                print(json.dumps(response))
            
            return
        
        # Check for :cd-tmux command
        if prompt == ':cd-tmux':
            # Get current directory from main tmux session "0"
            try:
                result = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", "0", "#{pane_current_path}"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                target_dir = result.stdout.strip()

                if not target_dir:
                    send_tmux_message("❌ Could not get directory from tmux session 0")
                    response = {"continue": False, "stopReason": "❌ Could not get directory from tmux session 0"}
                    print(json.dumps(response))
                    return

            except subprocess.CalledProcessError:
                send_tmux_message("❌ Could not access tmux session 0")
                response = {"continue": False, "stopReason": "❌ Could not access tmux session 0"}
                print(json.dumps(response))
                return

            current_dir = input_data.get('cwd', os.getcwd())

            # Encode paths
            encoded_current = current_dir.replace('/', '-')
            encoded_target = target_dir.replace('/', '-')

            # Find current session
            projects_dir = claude_data_dir / "projects" / encoded_current
            if not projects_dir.exists():
                send_tmux_message("❌ No session found in current directory")
                response = {"continue": False, "stopReason": "❌ No session found in current directory"}
                print(json.dumps(response))
                return

            session_files = sorted(projects_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not session_files:
                send_tmux_message("❌ No session found in current directory")
                response = {"continue": False, "stopReason": "❌ No session found in current directory"}
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
            response = {"continue": False, "stopReason": f"✓ Opened new window in {target_dir}"}
            print(json.dumps(response))
            return

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

def new_window():
    """Create a new Claude window with session tracking."""
    config_dir = Path.home() / ".config" / "kitty-claude"
    state_file = config_dir / "window-state.json"

    # Get current window index
    try:
        result = subprocess.run(
            ["tmux", "-L", "kitty-claude", "display-message", "-p", "#{window_index}"],
            capture_output=True,
            text=True,
            check=True
        )
        window_index = result.stdout.strip()
    except:
        window_index = "unknown"

    # If this is the first window (index 1, since base-index is 1), restore state
    if window_index == "1":
        try:
            if state_file.exists():
                state = json.loads(state_file.read_text())
                windows = state.get("windows", {})

                if windows:
                    # Get jail directory
                    uid = os.getuid()
                    jail_dir = Path(f"/var/run/{uid}/kitty-claude")
                    if not jail_dir.exists():
                        jail_dir = Path(f"/tmp/kitty-claude-{uid}")

                    # Sort by window index
                    sorted_windows = sorted(windows.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0)

                    # Restore windows 2 and onwards (skip window 1 since we're in it)
                    for win_index, window_data in sorted_windows[1:]:
                        path = window_data.get("path", str(jail_dir))
                        sess_id = window_data.get("session_id")

                        if sess_id:
                            subprocess.run(
                                ["tmux", "-L", "kitty-claude", "new-window", "-c", str(path), "claude", "--resume", sess_id],
                                stderr=subprocess.DEVNULL
                            )
        except Exception as e:
            print(f"Warning: Could not restore state: {e}", file=sys.stderr)

    # Generate new session ID
    session_id = str(uuid.uuid4())

    # Get current path
    current_path = os.getcwd()

    # Update state file
    try:
        if state_file.exists():
            state = json.loads(state_file.read_text())
        else:
            state = {"windows": {}}

        state["windows"][window_index] = {
            "session_id": session_id,
            "path": current_path
        }

        config_dir.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2))
    except Exception as e:
        print(f"Warning: Could not update state: {e}", file=sys.stderr)

    # Launch claude with the new session ID
    os.execvp("claude", ["claude", "--session-id", session_id])

def save_state():
    """State is maintained automatically by new_window()."""
    config_dir = Path.home() / ".config" / "kitty-claude"
    state_file = config_dir / "window-state.json"

    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            window_count = len(state.get("windows", {}))
            print(f"✓ State saved: {window_count} window(s)")
            return True
        except:
            pass
    return False

def restore_state(jail_dir):
    """Restore tmux windows from saved state."""
    config_dir = Path.home() / ".config" / "kitty-claude"
    state_file = config_dir / "window-state.json"

    if not state_file.exists():
        return

    try:
        state = json.loads(state_file.read_text())
        windows = state.get("windows", {})

        if not windows:
            return

        print(f"Restoring {len(windows)} window(s)...")

        # Sort by window index
        sorted_windows = sorted(windows.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0)

        # Skip first window (it will be created automatically)
        for window_index, window_data in sorted_windows[1:]:
            path = window_data.get("path", jail_dir)
            session_id = window_data.get("session_id")

            if session_id:
                subprocess.run(
                    ["tmux", "-L", "kitty-claude", "new-window", "-t", "kitty-claude", "-c", str(path), "claude", "--resume", session_id],
                    stderr=subprocess.DEVNULL
                )

        print("✓ State restored")

    except Exception as e:
        print(f"Warning: Could not restore state: {e}")

def restart():
    """Save state and restart kitty-claude."""
    config_dir = Path.home() / ".config" / "kitty-claude"

    # Save state
    print("Saving state...")
    save_state()

    # Kill tmux session
    print("Stopping tmux session...")
    try:
        subprocess.run(
            ["tmux", "-L", "kitty-claude", "kill-session", "-t", "kitty-claude"],
            stderr=subprocess.DEVNULL
        )
    except:
        pass

    # Relaunch (will restore state on startup)
    print("Relaunching...")
    os.execvp("kitty-claude", ["kitty-claude"])

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
    parser.add_argument("--new-window", action="store_true", help="Create new window with session tracking (internal use)")
    parser.add_argument("--restart", action="store_true", help="Restart kitty-claude with state preservation")
    parser.add_argument("--update-config", action="store_true", help="Regenerate tmux and kitty config files")
    parser.add_argument("--force-new", action="store_true", help="Launch new kitty window regardless of existing windows")
    args = parser.parse_args()

    config_dir = Path.home() / ".config" / "kitty-claude"
    claude_data_dir = config_dir / "claude-data"

    # Handle user prompt submit hook
    if args.user_prompt_submit:
        handle_user_prompt_submit()
        sys.exit(0)

    # Handle new window command
    if args.new_window:
        new_window()
        sys.exit(0)

    # Handle restart command
    if args.restart:
        restart()
        sys.exit(0)

    # Handle update-config command
    if args.update_config:
        print("Regenerating config files...")

        kitty_config_path = config_dir / "kitty.conf"
        tmux_config_path = config_dir / "tmux.conf"

        # Set up jail directory
        jail_dir = setup_jail_directory()

        # Ensure Claude config exists
        if not claude_data_dir.exists():
            setup_claude_config(config_dir)

        # Remove old configs
        if tmux_config_path.exists():
            tmux_config_path.unlink()
            print(f"Removed old {tmux_config_path}")

        if kitty_config_path.exists():
            kitty_config_path.unlink()
            print(f"Removed old {kitty_config_path}")

        # Regenerate tmux config
        tmux_config_path.write_text(f"""\
# kitty-claude tmux config (isolated server)
# Kill session when kitty window closes
set -g destroy-unattached on
# Set CLAUDE_CONFIG_DIR for isolated Claude data
set-environment -g CLAUDE_CONFIG_DIR "{claude_data_dir}"
# Default command is claude wrapper for session tracking
set -g default-command "kitty-claude --new-window"
# Bind C-n directly (no prefix) to open new window with claude in jail
bind -n C-n new-window -c "{jail_dir}" kitty-claude --new-window
# Also override default C-b c
bind c new-window -c "{jail_dir}" kitty-claude --new-window
# C-w closes current window, but not the last one
bind -n C-w if-shell "[ $(tmux list-windows | wc -l) -gt 1 ]" "kill-window" "display-message 'Cannot close last window'"
# C-v passthrough for paste
bind -n C-v send-keys C-v
# Alt-r to restart kitty-claude
bind -n M-r run-shell "kitty-claude --restart"
# Some sensible defaults
set -g mouse on
set -g history-limit 10000
set -g base-index 1
setw -g pane-base-index 1
# Easier window switching
bind -n C-j previous-window
bind -n C-k next-window
bind -n M-o last-window
# Style the status bar for better visibility
set -g status-style bg=colour235,fg=colour248
set -g window-status-style bg=colour235,fg=colour248
set -g window-status-current-style bg=colour39,fg=colour235,bold
set -g window-status-format " #I:#W "
set -g window-status-current-format " #I:#W "
""")
        print(f"✓ Created {tmux_config_path}")

        # Regenerate kitty config
        kitty_config_path.write_text(
            f"include {Path.home()}/.config/kitty/kitty.conf\n"
            f"shell tmux -L kitty-claude -f {tmux_config_path} new-session -As kitty-claude -c {jail_dir} kitty-claude --new-window\n"
        )
        print(f"✓ Created {kitty_config_path}")

        print("\nConfig files regenerated!")
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
    
    # Try to find and focus existing window (unless --force-new is set)
    if not args.force_new and find_and_focus_window():
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
# Default command is claude wrapper for session tracking
set -g default-command "kitty-claude --new-window"
# Bind C-n directly (no prefix) to open new window with claude in jail
bind -n C-n new-window -c "{jail_dir}" kitty-claude --new-window
# Also override default C-b c
bind c new-window -c "{jail_dir}" kitty-claude --new-window
# C-w closes current window, but not the last one
bind -n C-w if-shell "[ $(tmux list-windows | wc -l) -gt 1 ]" "kill-window" "display-message 'Cannot close last window'"
# C-v passthrough for paste
bind -n C-v send-keys C-v
# Alt-r to restart kitty-claude
bind -n M-r run-shell "kitty-claude --restart"
# Some sensible defaults
set -g mouse on
set -g history-limit 10000
set -g base-index 1
setw -g pane-base-index 1
# Easier window switching
bind -n C-j previous-window
bind -n C-k next-window
bind -n M-o last-window
# Style the status bar for better visibility
set -g status-style bg=colour235,fg=colour248
set -g window-status-style bg=colour235,fg=colour248
set -g window-status-current-style bg=colour39,fg=colour235,bold
set -g window-status-format " #I:#W "
set -g window-status-current-format " #I:#W "
""")
        print(f"Created tmux config at {tmux_config_path}")
    
    # Create kitty config if it doesn't exist
    if not kitty_config_path.exists():
        kitty_config_path.write_text(
            f"include {Path.home()}/.config/kitty/kitty.conf\n"
            f"shell tmux -L kitty-claude -f {tmux_config_path} new-session -As kitty-claude -c {jail_dir} kitty-claude --new-window\n"
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