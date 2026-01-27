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


def add_checkpoint_to_session(session_file):
    """Add a checkpoint marker to a session file.

    Args:
        session_file: Path to the JSONL session file
    """
    import time
    checkpoint_entry = {
        "type": "checkpoint",
        "timestamp": time.time(),
        "iso_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")
    }
    with open(session_file, 'a') as f:
        f.write(json.dumps(checkpoint_entry) + '\n')


def rollback_session_to_checkpoint(session_file, target_session_file):
    """Copy session file up to the last checkpoint into a new file.

    Args:
        session_file: Source session file
        target_session_file: Target session file to write

    Returns:
        True if checkpoint was found and rollback succeeded, False otherwise
    """
    # Read all lines and find the last checkpoint
    lines = []
    last_checkpoint_index = -1

    with open(session_file, 'r') as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get('type') == 'checkpoint':
                    last_checkpoint_index = i
                lines.append(line)
            except json.JSONDecodeError:
                lines.append(line)

    if last_checkpoint_index == -1:
        return False

    # Write everything up to and including the checkpoint
    with open(target_session_file, 'w') as f:
        for i, line in enumerate(lines):
            if i <= last_checkpoint_index:
                f.write(line + '\n')

    return True


def clone_session_and_change_directory(target_dir, current_dir, input_data, claude_data_dir, socket):
    """Clone current session to target directory and open new window/pane there.

    Args:
        target_dir: Target directory path (must exist)
        current_dir: Current working directory
        input_data: Hook input data containing session info
        claude_data_dir: Path to Claude data directory
        socket: Tmux socket name

    Returns:
        dict: Response to send back to Claude (continue=False, stopReason=message)
    """
    # Encode paths
    encoded_current = current_dir.replace('/', '-')
    encoded_target = target_dir.replace('/', '-')

    # Find current session
    projects_dir = claude_data_dir / "projects" / encoded_current
    if not projects_dir.exists():
        send_tmux_message("❌ Claude cannot resume without a message. Send one first.", socket)
        return {"continue": False, "stopReason": "❌ Claude cannot resume without a message. Send one first."}

    session_files = sorted(projects_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not session_files:
        send_tmux_message("❌ Claude cannot resume without a message. Send one first.", socket)
        return {"continue": False, "stopReason": "❌ Claude cannot resume without a message. Send one first."}

    # Check if session has any messages (not just metadata)
    source_file = session_files[0]
    if not session_has_messages(source_file):
        send_tmux_message("❌ Claude cannot resume without a message. Send one first.", socket)
        return {"continue": False, "stopReason": "❌ Claude cannot resume without a message. Send one first."}

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
        return {"continue": False, "stopReason": f"✓ Changing to {target_dir}"}

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
    return {"continue": False, "stopReason": f"✓ Moving to {target_dir}"}


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
        
        # Check for :help command
        if prompt == ':help':
            help_text = """kitty-claude colon commands:

:help           Show this help message
:list           List available slash commands (skills)
:restart        Restart Claude with fresh session
:cd <path>      Change directory and move session
:cd-tmux        Change to directory of tmux session 0
:fork           Open a fork in a popup window
:time           Show duration of last response
:checkpoint     Save a checkpoint in the current session
:rollback       Rollback to the last checkpoint (clones session)

Examples:
  :cd ~/projects/myapp
  :checkpoint
  :rollback
  :restart
  :list
"""
            send_tmux_message("📖 See console for help", socket)
            response = {"continue": False, "stopReason": help_text}
            print(json.dumps(response))
            return

        # Check for :list command
        if prompt == ':list':
            skills_dir = claude_data_dir / "skills"

            if not skills_dir.exists() or not any(skills_dir.iterdir()):
                message = "No skills installed.\n\nSkills can be added to .claude/skills/ in your project."
                send_tmux_message("📋 No skills found", socket)
                response = {"continue": False, "stopReason": message}
                print(json.dumps(response))
                return

            # List all skill directories
            skills = []
            for skill_dir in sorted(skills_dir.iterdir()):
                if skill_dir.is_dir():
                    skill_name = skill_dir.name
                    # Check if it's a symlink (project skill)
                    if skill_dir.is_symlink():
                        skills.append(f"  /{skill_name} (project)")
                    else:
                        skills.append(f"  /{skill_name}")

            if skills:
                skills_text = "Available slash commands:\n\n" + "\n".join(skills)
                send_tmux_message(f"📋 Found {len(skills)} skills", socket)
            else:
                skills_text = "No skills found."
                send_tmux_message("📋 No skills found", socket)

            response = {"continue": False, "stopReason": skills_text}
            print(json.dumps(response))
            return

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

        # Check for :checkpoint command
        if prompt == ':checkpoint':
            current_dir = input_data.get('cwd', os.getcwd())
            session_id = input_data.get('session_id')

            if not session_id:
                send_tmux_message("❌ No session ID available", socket)
                response = {"continue": False, "stopReason": "❌ No session ID available"}
                print(json.dumps(response))
                return

            # Encode path
            encoded_current = current_dir.replace('/', '-')

            # Find current session file
            projects_dir = claude_data_dir / "projects" / encoded_current
            if not projects_dir.exists():
                send_tmux_message("❌ No session found", socket)
                response = {"continue": False, "stopReason": "❌ No session found"}
                print(json.dumps(response))
                return

            session_file = projects_dir / f"{session_id}.jsonl"
            if not session_file.exists():
                send_tmux_message("❌ Session file not found", socket)
                response = {"continue": False, "stopReason": "❌ Session file not found"}
                print(json.dumps(response))
                return

            # Add checkpoint to session
            add_checkpoint_to_session(session_file)

            send_tmux_message("✓ Checkpoint saved", socket)
            response = {"continue": False, "stopReason": "✓ Checkpoint saved"}
            print(json.dumps(response))
            return

        # Check for :rollback command
        if prompt == ':rollback':
            current_dir = input_data.get('cwd', os.getcwd())
            session_id = input_data.get('session_id')

            if not session_id:
                send_tmux_message("❌ No session ID available", socket)
                response = {"continue": False, "stopReason": "❌ No session ID available"}
                print(json.dumps(response))
                return

            # Encode path
            encoded_current = current_dir.replace('/', '-')

            # Find current session file
            projects_dir = claude_data_dir / "projects" / encoded_current
            if not projects_dir.exists():
                send_tmux_message("❌ No session found", socket)
                response = {"continue": False, "stopReason": "❌ No session found"}
                print(json.dumps(response))
                return

            source_session_file = projects_dir / f"{session_id}.jsonl"
            if not source_session_file.exists():
                send_tmux_message("❌ Session file not found", socket)
                response = {"continue": False, "stopReason": "❌ Session file not found"}
                print(json.dumps(response))
                return

            # Generate new session ID for rollback
            new_session_id = str(uuid.uuid4())
            target_session_file = projects_dir / f"{new_session_id}.jsonl"

            # Rollback to checkpoint
            if not rollback_session_to_checkpoint(source_session_file, target_session_file):
                send_tmux_message("❌ No checkpoint found in session", socket)
                response = {"continue": False, "stopReason": "❌ No checkpoint found in session"}
                print(json.dumps(response))
                return

            # Update session metadata
            save_session_metadata(new_session_id, get_session_name(session_id), current_dir)

            # Get kitty-claude executable path
            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

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

            # Check if we're in one-tab mode
            if socket.startswith("kc1-"):
                # One-tab mode: use launcher script
                claude_config = os.environ.get('CLAUDE_CONFIG_DIR', str(claude_data_dir))

                uid = os.getuid()
                launcher = Path(f"/tmp/kc-rollback-{uid}-{new_session_id[:8]}.sh")
                launcher.write_text(f'''#!/bin/sh
export CLAUDE_CONFIG_DIR="{claude_config}"
cd "{current_dir}"
exec claude --resume {new_session_id}
''')
                launcher.chmod(0o755)

                # Schedule respawn after delay
                subprocess.Popen([
                    "sh", "-c",
                    f"sleep 1 && tmux -L {socket} respawn-pane -k {launcher}"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                send_tmux_message("✓ Rolling back to checkpoint...", socket)
                response = {"continue": False, "stopReason": "✓ Rolling back to checkpoint"}
                print(json.dumps(response))
                return

            # Regular multi-tab mode
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            cmd_parts = [kitty_claude_path]
            if profile:
                cmd_parts.extend(["--profile", profile])
            cmd_parts.extend(["--new-window", "--resume-session", new_session_id])
            cmd_str = " ".join(cmd_parts)

            run([
                "tmux", "-L", socket,
                "new-window", "-c", current_dir,
                cmd_str
            ])

            # Schedule closing the current window
            if current_window_id:
                close_script = f"""
sleep 2
if tmux -L {socket} list-windows -F '#{{@session_id}}' 2>/dev/null | grep -q '^{new_session_id}$'; then
    tmux -L {socket} kill-window -t {current_window_id} 2>/dev/null || true
fi
"""
                subprocess.Popen([
                    "sh", "-c",
                    close_script
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            send_tmux_message("✓ Rolled back to checkpoint", socket)
            response = {"continue": False, "stopReason": "✓ Rolled back to checkpoint"}
            print(json.dumps(response))
            return

        # Check for :cd-tmux command
        if prompt == ':cd-tmux':
            # Get current directory from session "0" on the default tmux server
            try:
                result = run(
                    ["tmux", "-L", "default", "display-message", "-p", "-t", "0", "#{pane_current_path}"],
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
                send_tmux_message("❌ Could not access tmux session 0 on default server", socket)
                response = {"continue": False, "stopReason": "❌ Could not access tmux session 0 on default server"}
                print(json.dumps(response))
                return

            # Check if directory exists
            if not os.path.isdir(target_dir):
                send_tmux_message(f"❌ Directory does not exist: {target_dir}", socket)
                response = {"continue": False, "stopReason": f"❌ Directory does not exist: {target_dir}"}
                print(json.dumps(response))
                return

            current_dir = input_data.get('cwd', os.getcwd())

            # Use the shared session cloning logic
            response = clone_session_and_change_directory(
                target_dir, current_dir, input_data, claude_data_dir, socket
            )
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
        
        # Check for :restart command
        if prompt == ':restart':
            current_dir = input_data.get('cwd', os.getcwd())

            # Get kitty-claude executable path
            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

            # Check if we're in one-tab mode
            if socket.startswith("kc1-"):
                # One-tab mode: respawn with fresh session
                claude_config = os.environ.get('CLAUDE_CONFIG_DIR', str(claude_data_dir))

                uid = os.getuid()
                launcher = Path(f"/tmp/kc-restart-{uid}.sh")
                launcher.write_text(f'''#!/bin/sh
export CLAUDE_CONFIG_DIR="{claude_config}"
cd "{current_dir}"
exec claude
''')
                launcher.chmod(0o755)

                # Schedule respawn after delay
                subprocess.Popen([
                    "sh", "-c",
                    f"sleep 1 && tmux -L {socket} respawn-pane -k {launcher}"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                send_tmux_message("✓ Restarting...", socket)
                response = {
                    "continue": False,
                    "stopReason": "✓ Restarting..."
                }
                print(json.dumps(response))
                return

            # Multi-tab mode: kill window and open new one
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

            # Get current window name
            try:
                result = run(
                    ["tmux", "-L", socket, "display-message", "-p", "#{window_name}"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                window_name = result.stdout.strip()
            except:
                window_name = None

            # Open new window with fresh session
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            cmd_parts = [kitty_claude_path]
            if profile:
                cmd_parts.extend(["--profile", profile])
            cmd_parts.append("--new-window")
            cmd_str = " ".join(cmd_parts)

            new_window_cmd = ["tmux", "-L", socket, "new-window"]
            if window_name:
                new_window_cmd.extend(["-n", window_name])
            new_window_cmd.extend(["-c", current_dir, cmd_str])

            run(new_window_cmd)

            # Close old window after delay
            if current_window_id:
                subprocess.Popen([
                    "sh", "-c",
                    f"sleep 2 && tmux -L {socket} kill-window -t {current_window_id} 2>/dev/null || true"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            send_tmux_message("✓ Restarting with fresh session", socket)
            response = {
                "continue": False,
                "stopReason": "✓ Restarting with fresh session"
            }
            print(json.dumps(response))
            return

        # Check for :cd command
        if prompt.startswith(':cd '):
            target_dir = prompt[4:].strip()

            # Convert to absolute path
            target_dir = str(Path(target_dir).expanduser().resolve())

            # Check if directory exists
            if not os.path.isdir(target_dir):
                send_tmux_message(f"❌ Directory does not exist: {target_dir}", socket)
                response = {
                    "continue": False,
                    "stopReason": f"❌ Directory does not exist: {target_dir}"
                }
                print(json.dumps(response))
                return

            current_dir = input_data.get('cwd', os.getcwd())

            # Use the shared session cloning logic
            response = clone_session_and_change_directory(
                target_dir, current_dir, input_data, claude_data_dir, socket
            )
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