#!/usr/bin/env python3
"""Colon command handlers for kitty-claude (:cd, :fork, :time, etc)."""

import os
import sys
import json
import shutil
import subprocess
import uuid
import shlex
from pathlib import Path

from kitty_claude.logging import log, run
from kitty_claude.colon_commands.time import (
    save_request_start_time,
    save_response_duration,
    get_last_response_duration
)
from kitty_claude.session import (
    get_session_name,
    save_session_metadata,
    remove_open_session
)


def get_tmux_socket():
    """Get the tmux socket name from environment or default."""
    # First try our explicit variable
    socket = os.environ.get('KITTY_CLAUDE_TMUX_SOCKET')
    if socket:
        return socket
    
    # Fallback: parse TMUX variable (format: /tmp/tmux-1000/socketname,pid,window)
    tmux_var = os.environ.get('TMUX', '')
    if tmux_var:
        # Extract socket name from path
        socket_path = tmux_var.split(',')[0]
        socket_name = os.path.basename(socket_path)
        if socket_name:
            return socket_name
    
    return 'kitty-claude'  # default


def send_tmux_message(message, socket=None):
    """Send a message via tmux display-message"""
    if socket is None:
        socket = get_tmux_socket()
    try:
        run([
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


def save_session_metadata(session_id, name, path):
    """Save session metadata to state directory."""
    state_dir = get_state_dir()
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    
    metadata_file = sessions_dir / f"{session_id}.json"
    metadata = {
        "name": name,
        "path": path,
        "created": run(["date", "-Iseconds"], capture_output=True, text=True).stdout.strip()
    }
    metadata_file.write_text(json.dumps(metadata, indent=2))


def session_has_messages(session_file):
    """Check if a session file has any actual user/assistant messages.
    
    Args:
        session_file: Path to the JSONL session file
        
    Returns:
        True if the session has at least one user or assistant message
    """
    try:
        with open(session_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get('type') in ('user', 'assistant'):
                        return True
                except json.JSONDecodeError:
                    continue
        return False
    except Exception:
        return False


def handle_user_prompt_submit(claude_data_dir=None):
    """Handle UserPromptSubmit hook - process custom commands like :cd and :fork"""
    socket = get_tmux_socket()
    
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
                send_tmux_message("❌ No session found", socket)
                response = {"continue": False, "stopReason": "❌ No session found"}
                print(json.dumps(response))
                return
            
            session_files = sorted(projects_dir.glob("*.jsonl"), 
                                 key=lambda p: p.stat().st_mtime, reverse=True)
            if not session_files:
                send_tmux_message("❌ No session found", socket)
                response = {"continue": False, "stopReason": "❌ No session found"}
                print(json.dumps(response))
                return
            
            # Generate new fork session ID
            fork_session_id = str(uuid.uuid4())
            
            # Clone session to fork
            fork_file = projects_dir / f"{fork_session_id}.jsonl"
            shutil.copy2(session_files[0], fork_file)
            
            send_tmux_message("🔀 Opening fork in popup...", socket)
            
            # Open fork in popup (blocking call)
            run([
                "tmux", "-L", socket,
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
                    send_tmux_message("✓ Fork completed, injecting response", socket)
                    
                    # Escape the message for shell safety
                    fork_message = f"Fork result:\n\n{last_message}"
                    escaped_message = shlex.quote(fork_message)
                    
                    # Background process: sleep then type the fork result
                    subprocess.Popen([
                        "sh", "-c",
                        f"sleep 0.5 && tmux -L {socket} send-keys -l {escaped_message} && tmux -L {socket} send-keys Enter"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    # Return immediately to unblock the hook
                    response = {"continue": False, "stopReason": ""}
                    print(json.dumps(response))
                else:
                    send_tmux_message("⚠ Fork had no assistant messages", socket)
                    response = {"continue": False, "stopReason": "Fork had no responses"}
                    print(json.dumps(response))
                
            except Exception as e:
                send_tmux_message(f"❌ Error reading fork: {str(e)}", socket)
                response = {"continue": False, "stopReason": f"Fork error: {str(e)}"}
                print(json.dumps(response))
            
            return
        
        # Check for :cd-tmux command
        if prompt == ':cd-tmux':
            # Get current directory from main tmux session "0"
            try:
                result = run(
                    ["tmux", "display-message", "-p", "-t", "0", "#{pane_current_path}"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                target_dir = result.stdout.strip()
                if not target_dir:
                    send_tmux_message("❌ Could not get directory from tmux session 0", socket)
                    response = {"continue": False, "stopReason": "❌ Could not get directory from tmux session 0"}
                    print(json.dumps(response))
                    return
            except subprocess.CalledProcessError:
                send_tmux_message("❌ Could not access tmux session 0", socket)
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
                send_tmux_message("❌ Claude cannot resume without a message. Send one first.", socket)
                response = {"continue": False, "stopReason": "❌ Claude cannot resume without a message. Send one first."}
                print(json.dumps(response))
                return
            
            session_files = sorted(projects_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not session_files:
                send_tmux_message("❌ Claude cannot resume without a message. Send one first.", socket)
                response = {"continue": False, "stopReason": "❌ Claude cannot resume without a message. Send one first."}
                print(json.dumps(response))
                return
            
            session_id = session_files[0].stem
            
            # Clone session
            target_projects_dir = claude_data_dir / "projects" / encoded_target
            target_projects_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(session_files[0], target_projects_dir / f"{session_id}.jsonl")
            
            # Open new tmux window
            run([
                "tmux", "-L", socket,
                "new-window", "-c", target_dir,
                f"claude --resume {session_id}"
            ])
            
            send_tmux_message(f"✓ Opened new window in {target_dir}", socket)
            response = {"continue": False, "stopReason": f"✓ Opened new window in {target_dir}"}
            print(json.dumps(response))
            return
        
        # Check for :time command
        if prompt == ':time':
            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("⏱ No session ID available", socket)
                response = {"continue": False, "stopReason": "⏱ No session ID available"}
                print(json.dumps(response))
                return
            
            duration = get_last_response_duration(session_id)
            if duration is None:
                send_tmux_message("⏱ No timing data available yet", socket)
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
            send_tmux_message(message, socket)
            response = {"continue": False, "stopReason": message}
            print(json.dumps(response))
            return
        
        # Check for :cd command
        if prompt.startswith(':cd '):
            target_dir = prompt[4:].strip()
            
            # Convert to absolute path
            target_dir = str(Path(target_dir).expanduser().resolve())
            
            current_dir = input_data.get('cwd', os.getcwd())
            
            # Encode paths
            encoded_current = current_dir.replace('/', '-')
            encoded_target = target_dir.replace('/', '-')
            
            # Find current session
            projects_dir = claude_data_dir / "projects" / encoded_current
            if not projects_dir.exists():
                send_tmux_message("❌ Claude cannot resume without a message. Send one first.", socket)
                response = {
                    "continue": False,
                    "stopReason": "❌ Claude cannot resume without a message. Send one first."
                }
                print(json.dumps(response))
                return
            
            session_files = sorted(projects_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not session_files:
                send_tmux_message("❌ Claude cannot resume without a message. Send one first.", socket)
                response = {
                    "continue": False,
                    "stopReason": "❌ Claude cannot resume without a message. Send one first."
                }
                print(json.dumps(response))
                return
            
            # Check if session has any messages (not just metadata)
            source_file = session_files[0]
            if not session_has_messages(source_file):
                send_tmux_message("❌ Claude cannot resume without a message. Send one first.", socket)
                response = {
                    "continue": False,
                    "stopReason": "❌ Claude cannot resume without a message. Send one first."
                }
                print(json.dumps(response))
                return
            
            old_session_id = source_file.stem
            
            # Generate NEW session ID for the target directory
            new_session_id = str(uuid.uuid4())
            
            # Get current window ID before creating new window
            try:
                result = run(
                    ["tmux", "-L", socket, "display-message", "-p", "#{window_id}"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                current_window_id = result.stdout.strip()
            except:
                current_window_id = None
            
            # Clone session to target directory with NEW session ID
            target_projects_dir = claude_data_dir / "projects" / encoded_target
            target_projects_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(session_files[0], target_projects_dir / f"{new_session_id}.jsonl")
            
            # Update session metadata with NEW session ID and path
            save_session_metadata(new_session_id, get_session_name(old_session_id), target_dir)
            
            # Get kitty-claude executable path
            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
            
            # Check if we're in one-tab mode (socket starts with kc1-)
            if socket.startswith("kc1-"):
                # One-tab mode: use a temp launcher script to avoid shell quoting issues
                # The script sets CLAUDE_CONFIG_DIR and runs claude
                
                claude_config = os.environ.get('CLAUDE_CONFIG_DIR', str(claude_data_dir))
                
                # Log for debugging
                log(f"one-tab :cd - config={claude_config}, session={new_session_id}, target={target_dir}")
                session_file = target_projects_dir / f"{new_session_id}.jsonl"
                log(f"one-tab :cd - session file exists: {session_file.exists()}")
                
                # Write a launcher script (avoids all tmux shell quoting issues)
                uid = os.getuid()
                launcher = Path(f"/tmp/kc-cd-{uid}-{new_session_id[:8]}.sh")
                launcher.write_text(f'''#!/bin/sh
export CLAUDE_CONFIG_DIR="{claude_config}"
cd "{target_dir}"
exec claude --resume {new_session_id}
''')
                launcher.chmod(0o755)
                log(f"one-tab :cd - launcher script: {launcher}")
                
                # Schedule respawn after delay (gives hook time to return)
                subprocess.Popen([
                    "sh", "-c",
                    f"sleep 1 && tmux -L {socket} respawn-pane -k {launcher}"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                send_tmux_message(f"✓ Changing to {target_dir}...", socket)
                response = {
                    "continue": False,
                    "stopReason": f"✓ Changing to {target_dir}"
                }
                print(json.dumps(response))
                return
            
            # Regular multi-tab mode: Open new tmux window using kitty-claude indirection with NEW session ID
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            cmd_parts = [kitty_claude_path]
            if profile:
                cmd_parts.extend(["--profile", profile])
            cmd_parts.extend(["--new-window", "--resume-session", new_session_id])
            cmd_str = " ".join(cmd_parts)
            
            run([
                "tmux", "-L", socket,
                "new-window", "-c", target_dir,
                cmd_str
            ])
            
            # Schedule closing the current window after verifying new window exists
            if current_window_id:
                # Script that waits, checks if new window exists with our session ID, then closes old window
                close_script = f"""
sleep 2
# Check if a window exists with the session ID we just created
if tmux -L {socket} list-windows -F '#{{@session_id}}' 2>/dev/null | grep -q '^{new_session_id}$'; then
    # New window exists, safe to close old window
    tmux -L {socket} kill-window -t {current_window_id} 2>/dev/null || true
fi
"""
                subprocess.Popen([
                    "sh", "-c",
                    close_script
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            send_tmux_message(f"✓ Moving to {target_dir}", socket)
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
        send_tmux_message(f"❌ {error_msg}", socket)
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
            # Remove from open sessions list
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            remove_open_session(session_id, profile)
    except Exception as e:
        # Log error silently
        with open("/tmp/kitty-claude-stop-hook-error.log", "a") as f:
            f.write(f"Stop hook error: {str(e)}\n")