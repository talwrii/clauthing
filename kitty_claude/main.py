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

def send_tmux_message(message, socket="kitty-claude"):
    """Send a message via tmux display-message"""
    try:
        subprocess.run([
            "tmux", "-L", socket,
            "display-message", message
        ], stderr=subprocess.DEVNULL)
    except:
        pass

def get_state_dir():
    """Get the XDG state directory for kitty-claude."""
    xdg_state = os.environ.get('XDG_STATE_HOME')
    if xdg_state:
        state_dir = Path(xdg_state) / "kitty-claude"
    else:
        state_dir = Path.home() / ".local" / "state" / "kitty-claude"

    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir

def save_request_start_time(session_id):
    """Save timestamp when request starts for a specific session."""
    import time
    state_dir = get_state_dir()
    timing_dir = state_dir / "timing"
    timing_dir.mkdir(exist_ok=True)
    timing_file = timing_dir / f"{session_id}.json"

    timing_data = {
        "start_time": time.time()
    }
    timing_file.write_text(json.dumps(timing_data))

def save_response_duration(session_id):
    """Calculate and save the duration of the last response for a specific session."""
    import time
    state_dir = get_state_dir()
    timing_dir = state_dir / "timing"
    timing_file = timing_dir / f"{session_id}.json"

    if not timing_file.exists():
        return

    try:
        timing_data = json.loads(timing_file.read_text())
        start_time = timing_data.get("start_time")

        if start_time:
            duration = time.time() - start_time
            timing_data["duration"] = duration
            timing_data["timestamp"] = time.time()
            timing_file.write_text(json.dumps(timing_data))
    except:
        pass

def get_last_response_duration(session_id):
    """Get the duration of the last response in seconds for a specific session."""
    state_dir = get_state_dir()
    timing_dir = state_dir / "timing"
    timing_file = timing_dir / f"{session_id}.json"

    if not timing_file.exists():
        return None

    try:
        timing_data = json.loads(timing_file.read_text())
        return timing_data.get("duration")
    except:
        return None

def save_session_metadata(session_id, name, path):
    """Save session metadata to state directory."""
    state_dir = get_state_dir()
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(exist_ok=True)

    metadata_file = sessions_dir / f"{session_id}.json"
    metadata = {
        "name": name,
        "path": path,
        "created": subprocess.run(["date", "-Iseconds"], capture_output=True, text=True).stdout.strip()
    }

    metadata_file.write_text(json.dumps(metadata, indent=2))

def get_session_name(session_id):
    """Get session name from metadata, or return session_id if not found."""
    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"

    if metadata_file.exists():
        try:
            metadata = json.loads(metadata_file.read_text())
            return metadata.get("name", session_id)
        except:
            pass

    return session_id

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

        # Check for :time command
        if prompt == ':time':
            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("⏱ No session ID available")
                response = {"continue": False, "stopReason": "⏱ No session ID available"}
                print(json.dumps(response))
                return

            duration = get_last_response_duration(session_id)

            if duration is None:
                send_tmux_message("⏱ No timing data available yet")
                response = {"continue": False, "stopReason": "⏱ No timing data available yet"}
                print(json.dumps(response))
                return

            # Format duration nicely
            if duration < 1:
                duration_str = f"{duration * 1000:.0f}ms"
            elif duration < 60:
                duration_str = f"{duration:.1f}s"
            else:
                minutes = int(duration // 60)
                seconds = duration % 60
                duration_str = f"{minutes}m {seconds:.1f}s"

            message = f"⏱ Last response took: {duration_str}"
            send_tmux_message(message)
            response = {"continue": False, "stopReason": message}
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

            # Get current window ID before creating new window
            try:
                result = subprocess.run(
                    ["tmux", "-L", "kitty-claude", "display-message", "-p", "#{window_id}"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                current_window_id = result.stdout.strip()
            except:
                current_window_id = None

            # Clone session to target directory
            target_projects_dir = claude_data_dir / "projects" / encoded_target
            target_projects_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(session_files[0], target_projects_dir / f"{session_id}.jsonl")

            # Update session metadata with new path
            save_session_metadata(session_id, get_session_name(session_id), target_dir)

            # Get kitty-claude executable path
            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

            # Open new tmux window using kitty-claude indirection
            subprocess.run([
                "tmux", "-L", "kitty-claude",
                "new-window", "-c", target_dir,
                f"{kitty_claude_path} --new-window --resume-session {session_id}"
            ])

            # Schedule closing the current window after verifying new window exists
            if current_window_id:
                # Script that waits, checks if new window exists with our session ID, then closes old window
                close_script = f"""
sleep 2
# Check if a window exists with the session ID we just created
if tmux -L kitty-claude list-windows -F '#{{@session_id}}' 2>/dev/null | grep -q '^{session_id}$'; then
    # New window exists, safe to close old window
    tmux -L kitty-claude kill-window -t {current_window_id} 2>/dev/null || true
fi
"""
                subprocess.Popen([
                    "sh", "-c",
                    close_script
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            send_tmux_message(f"✓ Moving to {target_dir}")
            response = {
                "continue": False,
                "stopReason": f"✓ Moving to {target_dir}"
            }
            print(json.dumps(response))
            return
        
        # Not a custom command, save start time and pass through
        session_id = input_data.get('session_id')
        if session_id:
            save_request_start_time(session_id)
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

def handle_stop():
    """Handle Stop hook - calculate and save response duration."""
    try:
        # Read JSON from stdin
        input_data = json.loads(sys.stdin.read())
        session_id = input_data.get('session_id')

        if session_id:
            save_response_duration(session_id)
    except Exception as e:
        # Log error silently
        with open("/tmp/kitty-claude-stop-hook-error.log", "a") as f:
            f.write(f"Stop hook error: {str(e)}\n")

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
    
    # Create settings.json with UserPromptSubmit and Stop hooks
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
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{kitty_claude_path} --stop"
                            }
                        ]
                    }
                ]
            }
        }, indent=2))
        print(f"Created settings with UserPromptSubmit and Stop hooks at {settings_file}")
    
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

def get_runtime_state_file(profile=None):
    """Get the runtime window state file path."""
    uid = os.getuid()

    # Try /var/run first
    try:
        runtime_dir = Path(f"/var/run/{uid}/kitty-claude")
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # Fallback to /tmp
        runtime_dir = Path(f"/tmp/kitty-claude-{uid}")
        runtime_dir.mkdir(parents=True, exist_ok=True)

    # Use profile-specific state file if profile is set
    if profile:
        return runtime_dir / f"window-state-{profile}.json"
    else:
        return runtime_dir / "window-state.json"

def new_window(resume_session_id=None):
    """Create a new Claude window with session tracking.

    Args:
        resume_session_id: Optional session ID to resume instead of creating new
    """
    profile = os.environ.get('KITTY_CLAUDE_PROFILE')
    state_file = get_runtime_state_file(profile)

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
                        win_name = window_data.get("name")

                        if sess_id:
                            # Create window
                            subprocess.run(
                                ["tmux", "-L", "kitty-claude", "new-window", "-c", str(path), "-n", win_name or get_session_name(sess_id), "claude", "--resume", sess_id],
                                stderr=subprocess.DEVNULL
                            )
                            # Set session ID in window option
                            subprocess.run(
                                ["tmux", "-L", "kitty-claude", "set-option", "-w", "-t", f":{win_index}", "@session_id", sess_id],
                                stderr=subprocess.DEVNULL
                            )
        except Exception as e:
            print(f"Warning: Could not restore state: {e}", file=sys.stderr)

    # Use provided session ID or generate new one
    if resume_session_id:
        session_id = resume_session_id
    else:
        session_id = str(uuid.uuid4())

    # Get current path
    current_path = os.getcwd()

    # Get session name from metadata if resuming, otherwise generate from path
    if resume_session_id:
        default_name = get_session_name(session_id)
    else:
        # Generate default session name from path
        default_name = Path(current_path).name or "claude"
        # Save session metadata with default name
        save_session_metadata(session_id, default_name, current_path)

    # Set window name to default and store session ID in window option
    try:
        subprocess.run(
            ["tmux", "-L", "kitty-claude", "rename-window", default_name],
            stderr=subprocess.DEVNULL
        )
        subprocess.run(
            ["tmux", "-L", "kitty-claude", "set-option", "-w", f"@session_id", session_id],
            stderr=subprocess.DEVNULL
        )
    except:
        pass

    # Update state file
    try:
        if state_file.exists():
            state = json.loads(state_file.read_text())
        else:
            state = {"windows": {}}

        state["windows"][window_index] = {
            "session_id": session_id,
            "path": current_path,
            "name": default_name
        }

        # Ensure parent directory exists
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2))
    except Exception as e:
        print(f"Warning: Could not update state: {e}", file=sys.stderr)

    # Launch claude with the session ID (resume if provided, otherwise use --session-id)
    if resume_session_id:
        os.execvp("claude", ["claude", "--resume", session_id])
    else:
        os.execvp("claude", ["claude", "--session-id", session_id])

def save_state():
    """State is maintained automatically by new_window()."""
    profile = os.environ.get('KITTY_CLAUDE_PROFILE')
    state_file = get_runtime_state_file(profile)

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
    profile = os.environ.get('KITTY_CLAUDE_PROFILE')
    state_file = get_runtime_state_file(profile)

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

def open_session_notes():
    """Open session notes in vim via tmux popup."""
    profile = os.environ.get('KITTY_CLAUDE_PROFILE')
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    state_file = get_runtime_state_file(profile)

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
        subprocess.run(
            ["tmux", "-L", "kitty-claude", "display-message", "Could not get window index"],
            stderr=subprocess.DEVNULL
        )
        return

    # Load state to get session ID
    if not state_file.exists():
        subprocess.run(
            ["tmux", "-L", "kitty-claude", "display-message", "No session found"],
            stderr=subprocess.DEVNULL
        )
        return

    try:
        state = json.loads(state_file.read_text())
        windows = state.get("windows", {})
        window_data = windows.get(window_index)

        if not window_data:
            subprocess.run(
                ["tmux", "-L", "kitty-claude", "display-message", "No session data for this window"],
                stderr=subprocess.DEVNULL
            )
            return

        session_id = window_data.get("session_id")
        if not session_id:
            subprocess.run(
                ["tmux", "-L", "kitty-claude", "display-message", "No session ID found"],
                stderr=subprocess.DEVNULL
            )
            return

        # Create notes file path
        notes_dir = config_dir / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        notes_file = notes_dir / f"{session_id}.md"

        # Open vim in tmux popup
        subprocess.run([
            "tmux", "-L", "kitty-claude",
            "display-popup", "-E", "-w", "80%", "-h", "80%",
            f"vim {notes_file}"
        ])

    except Exception as e:
        subprocess.run(
            ["tmux", "-L", "kitty-claude", "display-message", f"Error opening notes: {str(e)}"],
            stderr=subprocess.DEVNULL
        )

def get_log_file(profile=None):
    """Get the log file path for the given profile."""
    if profile:
        return Path(f"/tmp/kitty-claude-{profile}.log")
    else:
        return Path("/tmp/kitty-claude.log")

def log(message, profile=None):
    """Log a message to the profile-specific log file."""
    log_file = get_log_file(profile)
    try:
        with open(log_file, "a") as f:
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{timestamp}] {message}\n")
    except:
        pass

def main():
    parser = argparse.ArgumentParser(description="Launch Claude Code in isolated kitty+tmux environment")
    parser.add_argument("--reinstall", action="store_true", help="Remove all config except credentials and exit")
    parser.add_argument("--user-prompt-submit", action="store_true", help="Handle UserPromptSubmit hook (internal use)")
    parser.add_argument("--stop", action="store_true", help="Handle Stop hook (internal use)")
    parser.add_argument("--new-window", action="store_true", help="Create new window with session tracking (internal use)")
    parser.add_argument("--resume-session", type=str, metavar="SESSION_ID", help="Resume specific session in new window (internal use)")
    parser.add_argument("--restart", action="store_true", help="Restart kitty-claude with state preservation")
    parser.add_argument("--update-config", action="store_true", help="Regenerate tmux and kitty config files")
    parser.add_argument("--force-new", action="store_true", help="Launch new kitty window regardless of existing windows")
    parser.add_argument("--rename-session", nargs=2, metavar=("SESSION_ID", "NAME"), help="Rename session (internal use)")
    parser.add_argument("--rename", type=str, metavar="NAME", help="Rename current window's session (looks up session ID automatically)")
    parser.add_argument("--no-kitty", action="store_true", help="Run tmux directly without kitty (for testing)")
    parser.add_argument("--notes", action="store_true", help="Open session notes in vim popup")
    parser.add_argument("--profile", type=str, help="Use specific profile (required for non-internal commands)")
    parser.add_argument("--copy-profile", nargs=2, metavar=("SOURCE", "DEST"), help="Copy profile SOURCE to DEST")
    parser.add_argument("--follow-logs", action="store_true", help="Follow log file for current profile")
    args = parser.parse_args()

    # Determine profile name
    profile = args.profile or os.environ.get('KITTY_CLAUDE_PROFILE')

    # Log this invocation
    import sys as sys_module
    log(f"=== COMMAND: {' '.join(sys_module.argv)} ===", profile)

    # Set up directories based on profile
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
        tmux_socket = f"kitty-claude-{profile}"
        kitty_claude_cmd = f"kitty-claude --profile {profile} --new-window"
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
        tmux_socket = "kitty-claude"
        kitty_claude_cmd = "kitty-claude --new-window"

    claude_data_dir = config_dir / "claude-data"

    # Handle follow-logs command
    if args.follow_logs:
        log_file = get_log_file(profile)
        if not log_file.exists():
            print(f"Log file does not exist: {log_file}")
            print("Run some kitty-claude commands first to generate logs")
            sys.exit(1)

        # Print last 80 lines
        try:
            with open(log_file, "r") as f:
                lines = f.readlines()
                start = max(0, len(lines) - 80)
                for line in lines[start:]:
                    print(line, end='')
        except Exception as e:
            print(f"Error reading log file: {e}")

        # Follow the log file
        print(f"\n--- Following {log_file} ---")
        os.execvp("tail", ["tail", "-f", str(log_file)])

    # Handle copy-profile command
    if args.copy_profile:
        source_profile, dest_profile = args.copy_profile

        # Source: if "default", use base config dir, otherwise other-profiles
        if source_profile == "default":
            source_dir = Path.home() / ".config" / "kitty-claude"
        else:
            source_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / source_profile

        # Dest: always in other-profiles
        dest_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / dest_profile

        if not source_dir.exists():
            print(f"Error: Source profile '{source_profile}' does not exist at {source_dir}")
            sys.exit(1)

        if dest_dir.exists():
            print(f"Error: Destination profile '{dest_profile}' already exists at {dest_dir}")
            sys.exit(1)

        print(f"Copying profile '{source_profile}' to '{dest_profile}'...")

        # Exclude config files (they'll be regenerated) and other directories
        def ignore_configs(directory, contents):
            ignored = []
            if directory == str(source_dir):
                # Exclude these from root of source
                ignored.extend(['other-profiles', 'worktrees', 'kitty.conf', 'tmux.conf'])
            return ignored

        shutil.copytree(source_dir, dest_dir, ignore=ignore_configs)
        print(f"✓ Profile '{dest_profile}' created at {dest_dir}")
        sys.exit(0)

    # Handle notes command
    if args.notes:
        open_session_notes()
        sys.exit(0)

    # Handle user prompt submit hook
    if args.user_prompt_submit:
        handle_user_prompt_submit()
        sys.exit(0)

    # Handle stop hook
    if args.stop:
        handle_stop()
        sys.exit(0)

    # Handle new window command
    if args.new_window:
        new_window(resume_session_id=args.resume_session)
        sys.exit(0)

    # Handle restart command
    if args.restart:
        restart()
        sys.exit(0)

    # Handle rename command (looks up session ID automatically)
    if args.rename:
        new_name = args.rename
        log(f"Rename request: new_name={new_name}, profile={profile}, tmux_socket={tmux_socket}", profile)

        # Get session ID from tmux window option
        try:
            cmd = ["tmux", "-L", tmux_socket, "display-message", "-p", "#{@session_id}"]
            log(f"Running: {' '.join(cmd)}", profile)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True
            )
            session_id = result.stdout.strip()
            log(f"Got session_id='{session_id}', stdout='{result.stdout}', stderr='{result.stderr}'", profile)
        except Exception as e:
            log(f"Error getting session ID: {e}", profile)
            print("Error: Could not get session ID from tmux", file=sys.stderr)
            sys.exit(1)

        if not session_id:
            log("ERROR: Session ID is empty", profile)
            print("Error: No session ID set for this window", file=sys.stderr)
            sys.exit(1)

        # Now call the rename logic with the looked-up session ID
        args.rename_session = (session_id, new_name)
        # Fall through to rename-session handler below

    # Handle rename-session command
    if args.rename_session:
        session_id, new_name = args.rename_session
        log(f"Rename session handler: session_id={session_id}, new_name={new_name}", profile)

        # Update session metadata
        state_dir = get_state_dir()
        metadata_file = state_dir / "sessions" / f"{session_id}.json"
        log(f"Metadata file: {metadata_file}, exists={metadata_file.exists()}", profile)

        if metadata_file.exists():
            try:
                metadata = json.loads(metadata_file.read_text())
                metadata["name"] = new_name
                metadata_file.write_text(json.dumps(metadata, indent=2))
                log("Updated metadata file", profile)
            except Exception as e:
                log(f"Error updating metadata: {e}", profile)

        # Update window state
        state_file = get_runtime_state_file(profile)
        log(f"State file: {state_file}, exists={state_file.exists()}", profile)

        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                for window_index, window_data in state.get("windows", {}).items():
                    if window_data.get("session_id") == session_id:
                        window_data["name"] = new_name
                        log(f"Updated window {window_index} name", profile)
                        break
                state_file.write_text(json.dumps(state, indent=2))
            except Exception as e:
                log(f"Error updating state: {e}", profile)

        # Rename current tmux window
        try:
            cmd = ["tmux", "-L", tmux_socket, "rename-window", new_name]
            log(f"Running: {' '.join(cmd)}", profile)

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            log(f"Rename successful, stdout='{result.stdout}', stderr='{result.stderr}'", profile)
        except Exception as e:
            log(f"Error renaming window: {e}", profile)

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
set -g default-command "{kitty_claude_cmd}"
# Bind C-n directly (no prefix) to open new window with claude in jail
bind -n C-n new-window -c "{jail_dir}" {kitty_claude_cmd}
# Also override default C-b c
bind c new-window -c "{jail_dir}" {kitty_claude_cmd}
# C-w closes current window, but not the last one
bind -n C-w if-shell "[ $(tmux list-windows | wc -l) -gt 1 ]" "kill-window" "display-message 'Cannot close last window'"
# C-v passthrough for paste
bind -n C-v send-keys C-v
# Alt-r to restart kitty-claude
bind -n M-r run-shell "kitty-claude --restart"
# Alt-e to open session notes
bind -n M-e run-shell "kitty-claude --notes"
# Some sensible defaults
set -g mouse on
set -g history-limit 10000
set -g base-index 1
setw -g pane-base-index 1
# Easier window switching
bind -n C-j previous-window
bind -n C-k next-window
bind -n M-o last-window
# Disable automatic window renaming (we manage names manually)
set -g automatic-rename off
set -g allow-rename off
# Bind M-n to prompt for window name and update session metadata
bind -n M-n command-prompt -I "#W" -p "Session name:" "run-shell 'kitty-claude --rename \\"%%\\"'"
# Multiline status bar (3 lines) for more window visibility
set -g status 3
set -g status-style bg=colour235,fg=colour248
# Top line: kitty-claude label
set -g status-format[0] '#[bg=colour235,fg=colour248] [kitty-claude]'
# Middle line: window list (this is where all windows show)
set -g status-format[1] '#[bg=colour235,fg=colour248,align=left]#{{W:#{{E:window-status-format}},#{{E:window-status-current-format}}}}'
# Bottom line: current path
set -g status-format[2] '#[bg=colour235,fg=colour248,align=right] #{{pane_current_path}} '
# Window status styling
set -g window-status-style bg=colour235,fg=colour248
set -g window-status-current-style bg=colour39,fg=colour235,bold
set -g window-status-format " #I:#W "
set -g window-status-current-format " #I:#W "
""")
        print(f"✓ Created {tmux_config_path}")

        # Regenerate kitty config
        kitty_config_path.write_text(
            f"include {Path.home()}/.config/kitty/kitty.conf\n"
            f"shell tmux -L {tmux_socket} -f {tmux_config_path} new-session -As {tmux_socket} -c {jail_dir} {kitty_claude_cmd}\n"
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
    
    # Handle --no-kitty mode (run tmux directly for testing)
    if args.no_kitty:
        # Set up isolated Claude config
        claude_data_dir = setup_claude_config(config_dir)

        # Set up jail directory
        jail_dir = setup_jail_directory()

        # Create tmux config
        tmux_config_path = config_dir / "tmux.conf"
        if not tmux_config_path.exists():
            config_dir.mkdir(parents=True, exist_ok=True)
            tmux_config_path.write_text(f"""\
# kitty-claude tmux config (isolated server)
# Kill session when kitty window closes
set -g destroy-unattached on
# Set CLAUDE_CONFIG_DIR for isolated Claude data
set-environment -g CLAUDE_CONFIG_DIR "{claude_data_dir}"
# Default command is claude wrapper for session tracking
set -g default-command "kitty-claude --new-window"
# Some sensible defaults
set -g mouse on
set -g history-limit 10000
set -g base-index 1
setw -g pane-base-index 1
""")

        # Launch tmux directly
        os.execvp("tmux", [
            "tmux", "-L", "kitty-claude", "-f", str(tmux_config_path),
            "new-session", "-As", "kitty-claude-test", "-c", str(jail_dir),
            "kitty-claude", "--new-window"
        ])

    # Try to find and focus existing window (unless --force-new or --profile is set)
    if not args.force_new and not profile and find_and_focus_window():
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

    # Remove old config files if they exist (they're read-only)
    if tmux_config_path.exists():
        tmux_config_path.unlink()
    if kitty_config_path.exists():
        kitty_config_path.unlink()

    # Always regenerate tmux config (it's ephemeral, not user-editable)
    tmux_config_path.write_text(f"""\
# ============================================================================
# DO NOT MODIFY THIS FILE - IT IS AUTO-GENERATED ON EVERY LAUNCH
# This file is regenerated each time kitty-claude starts
# To customize: Use hooks or environment variables (future feature)
# ============================================================================
#
# kitty-claude tmux config (isolated server)
# Kill session when kitty window closes
set -g destroy-unattached on
# Set CLAUDE_CONFIG_DIR for isolated Claude data
set-environment -g CLAUDE_CONFIG_DIR "{claude_data_dir}"
# Default command is claude wrapper for session tracking
set -g default-command "{kitty_claude_cmd}"
# Bind C-n directly (no prefix) to open new window with claude in jail
bind -n C-n new-window -c "{jail_dir}" {kitty_claude_cmd}
# Also override default C-b c
bind c new-window -c "{jail_dir}" {kitty_claude_cmd}
# C-w closes current window, but not the last one
bind -n C-w if-shell "[ $(tmux list-windows | wc -l) -gt 1 ]" "kill-window" "display-message 'Cannot close last window'"
# C-v passthrough for paste
bind -n C-v send-keys C-v
# Alt-r to restart kitty-claude
bind -n M-r run-shell "kitty-claude --restart"
# Alt-e to open session notes
bind -n M-e run-shell "kitty-claude --notes"
# Some sensible defaults
set -g mouse on
set -g history-limit 10000
set -g base-index 1
setw -g pane-base-index 1
# Easier window switching
bind -n C-j previous-window
bind -n C-k next-window
bind -n M-o last-window
# Disable automatic window renaming (we manage names manually)
set -g automatic-rename off
set -g allow-rename off
# Bind M-n to prompt for window name and update session metadata
bind -n M-n command-prompt -I "#W" -p "Session name:" "run-shell 'kitty-claude --rename \\"%%\\"'"
# Multiline status bar (3 lines) for more window visibility
set -g status 3
set -g status-style bg=colour235,fg=colour248
# Top line: kitty-claude label
set -g status-format[0] '#[bg=colour235,fg=colour248] [kitty-claude]'
# Middle line: window list (this is where all windows show)
set -g status-format[1] '#[bg=colour235,fg=colour248,align=left]#{{W:#{{E:window-status-format}},#{{E:window-status-current-format}}}}'
# Bottom line: current path
set -g status-format[2] '#[bg=colour235,fg=colour248,align=right] #{{pane_current_path}} '
# Window status styling
set -g window-status-style bg=colour235,fg=colour248
set -g window-status-current-style bg=colour39,fg=colour235,bold
set -g window-status-format " #I:#W "
set -g window-status-current-format " #I:#W "
""")
    tmux_config_path.chmod(0o444)  # Read-only
    print(f"Created tmux config at {tmux_config_path}")

    # Always regenerate kitty config (it's ephemeral, not user-editable)
    kitty_config_path.write_text(f"""\
# ============================================================================
# DO NOT MODIFY THIS FILE - IT IS AUTO-GENERATED ON EVERY LAUNCH
# ============================================================================
include {Path.home()}/.config/kitty/kitty.conf
shell tmux -L {tmux_socket} -f {tmux_config_path} new-session -As {tmux_socket} -c {jail_dir} {kitty_claude_cmd}
""")
    kitty_config_path.chmod(0o444)  # Read-only
    print(f"Created kitty config at {kitty_config_path}")

    # Clear runtime window state (fresh start for each launch)
    state_file = get_runtime_state_file(profile)
    if state_file.exists():
        state_file.unlink()
        print(f"Cleared runtime state: {state_file}")

    # Launch kitty
    os.execvp("kitty", [
        "kitty",
        "--class=kitty-claude",
        f"--config={kitty_config_path}"
    ])

if __name__ == "__main__":
    main()