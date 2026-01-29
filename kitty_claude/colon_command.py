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
from kitty_claude.session_utils import session_has_messages
from kitty_claude.window_utils import open_session_notes
from kitty_claude.tmux import get_runtime_tmux_state_file
from kitty_claude.rules import build_claude_md


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

        # Register this session as running (if not already registered)
        session_id = input_data.get('session_id')
        if session_id:
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            cwd = input_data.get('cwd', os.getcwd())
            # Find Claude's PID - try multiple approaches
            claude_pid = None
            try:
                # First try: multi-tab mode with --resume
                result = subprocess.run(
                    ["pgrep", "-f", f"claude --resume {session_id}"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    claude_pid = int(result.stdout.strip().split('\n')[0])

                # Second try: one-tab mode - get PID from tmux pane
                if not claude_pid:
                    socket = os.environ.get('KITTY_CLAUDE_TMUX_SOCKET')
                    if socket:
                        result = subprocess.run(
                            ["tmux", "-L", socket, "display-message", "-p", "#{pane_pid}"],
                            capture_output=True, text=True
                        )
                        if result.returncode == 0:
                            pane_pid = int(result.stdout.strip())
                            # The pane_pid is usually the shell, Claude is a child
                            result = subprocess.run(
                                ["pgrep", "-P", str(pane_pid), "claude"],
                                capture_output=True, text=True
                            )
                            if result.returncode == 0:
                                claude_pid = int(result.stdout.strip().split('\n')[0])

                if claude_pid:
                    from kitty_claude.claude import register_running_session
                    register_running_session(session_id, claude_pid, cwd, profile)
                    with open("/tmp/session-register-debug.log", "a") as f:
                        f.write(f"Registered: {session_id[:8]} PID={claude_pid} cwd={cwd}\n")
            except Exception as e:
                with open("/tmp/session-register-debug.log", "a") as f:
                    f.write(f"Failed to register {session_id[:8]}: {e}\n")

        # Check for :help command
        if prompt == ':help':
            help_text = """kitty-claude colon commands:

:help                Show this help message
:list                List available slash commands (skills)
:rules               List all rules
:note                Open session notes in vim
:skill <name>        Create/edit a global Claude skill
:rule <name>         Create/edit a global rule
::skills             List all kitty-claude skills
::skill <name>       Create/edit a kitty-claude skill
::<skill> [prompt]   Run kitty-claude skill (injects context)
:mcp-shell <cmd>     Expose shell command as MCP server
:current-sessions    List all currently running sessions
:sessions            List recent sessions (last 10)
:resume <num|id>     Resume a session in new window
:clear               Clear session and start fresh
:reload              Reload Claude (same session, pick up config changes)
:cd <path>           Change directory and move session
:cd-tmux             Change to directory of tmux session 0
:fork                Open a fork in a popup window
:time                Show duration of last response
:checkpoint          Save a checkpoint in the current session
:rollback            Rollback to the last checkpoint (clones session)

Examples:
  :list, :rules, ::skills
  :note
  :skill my-skill
  :rule my-rule
  ::skill context
  ::context what should I do?
  :mcp-shell cat
  :current-sessions
  :sessions
  :resume 1
  :resume 58093da9-7922-4bc0-a89f-eea6691b02eb
  :cd ~/projects/myapp
  :checkpoint
  :rollback
  :clear
  :reload
"""
            send_tmux_message("📖 See console for help", socket)
            response = {"continue": False, "stopReason": help_text}
            print(json.dumps(response))
            return

        # Check for :current-sessions command
        if prompt == ':current-sessions':
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            from kitty_claude.claude import get_running_sessions

            sessions = get_running_sessions(profile)

            if not sessions:
                msg = "No currently running sessions"
            else:
                lines = ["Currently running sessions:\n"]
                for i, sess in enumerate(sessions, 1):
                    cwd = sess.get('cwd', '?')
                    session_id = sess['session_id']
                    pid = sess['pid']
                    lines.append(f"{i}. {session_id[:8]}... (PID {pid}) - {cwd}")
                msg = "\n".join(lines)

            send_tmux_message(f"✓ {len(sessions)} running", socket)
            response = {"continue": False, "stopReason": msg}
            print(json.dumps(response))
            return

        # Check for :sessions command
        if prompt == ':sessions':
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            from kitty_claude.claude import get_recent_sessions
            from datetime import datetime

            sessions = get_recent_sessions(profile, limit=10)

            if not sessions:
                msg = "No recent sessions found"
            else:
                lines = ["Recent sessions (ordered by last activity):\n"]
                for i, sess in enumerate(sessions, 1):
                    session_id = sess['session_id']
                    cwd = sess.get('cwd', '?')
                    mtime = datetime.fromtimestamp(sess['last_modified']).strftime('%Y-%m-%d %H:%M')
                    lines.append(f"{i}. {session_id[:8]}... - {cwd} (last: {mtime})")
                lines.append(f"\nUse :resume <number> or :resume <session-id> to resume")
                msg = "\n".join(lines)

            send_tmux_message(f"✓ {len(sessions)} sessions", socket)
            response = {"continue": False, "stopReason": msg}
            print(json.dumps(response))
            return

        # Check for :resume command
        if prompt.startswith(':resume '):
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            arg = prompt[8:].strip()

            # Determine if arg is a number or session ID
            target_session_id = None
            if arg.isdigit():
                # It's a number - get from :sessions list
                from kitty_claude.claude import get_recent_sessions
                sessions = get_recent_sessions(profile, limit=10)
                index = int(arg) - 1
                if 0 <= index < len(sessions):
                    target_session_id = sessions[index]['session_id']
                else:
                    send_tmux_message(f"❌ Invalid session number", socket)
                    response = {"continue": False, "stopReason": f"❌ Session number {arg} not found"}
                    print(json.dumps(response))
                    return
            else:
                # It's a session ID
                target_session_id = arg

            # Open new window with this session
            from kitty_claude.claude import new_window
            new_window(profile=profile, resume_session_id=target_session_id, socket=socket)

            send_tmux_message(f"✓ Resuming session", socket)
            response = {"continue": False, "stopReason": f"✓ Opening session {target_session_id[:8]}... in new window"}
            print(json.dumps(response))
            return

        # Check for :rules command
        if prompt == ':rules':
            # Determine config directory based on profile
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            if profile:
                config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                config_dir = Path.home() / ".config" / "kitty-claude"

            rules_dir = config_dir / "rules"

            if not rules_dir.exists() or not any(rules_dir.iterdir()):
                message = "No rules found.\n\nCreate rules with :rule <name>"
                send_tmux_message("📋 No rules found", socket)
                response = {"continue": False, "stopReason": message}
                print(json.dumps(response))
                return

            # List all rule files
            rules = []
            for rule_file in sorted(rules_dir.iterdir()):
                if rule_file.is_file() and rule_file.suffix == '.md':
                    rule_name = rule_file.stem
                    rules.append(f"  {rule_name}")

            if rules:
                rules_text = "Available rules:\n\n" + "\n".join(rules)
                send_tmux_message(f"📋 Found {len(rules)} rules", socket)
            else:
                rules_text = "No rules found."
                send_tmux_message("📋 No rules found", socket)

            response = {"continue": False, "stopReason": rules_text}
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
                from kitty_claude.session_utils import get_last_assistant_message
                last_message = get_last_assistant_message(fork_file)

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

        # Check for :reload command
        if prompt == ':reload':
            current_dir = input_data.get('cwd', os.getcwd())
            session_id = input_data.get('session_id')

            if not session_id:
                send_tmux_message("❌ No session ID available", socket)
                response = {"continue": False, "stopReason": "❌ No session ID available"}
                print(json.dumps(response))
                return

            # Get kitty-claude executable path
            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

            # Rebuild CLAUDE.md from rules before reloading
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            build_claude_md(profile)

            # Save auth from current session before regenerating config
            from kitty_claude.claude import save_auth_from_session, setup_session_config
            save_auth_from_session(session_id, profile)

            # Merge session config (global + session.json overrides)
            session_config_dir = setup_session_config(session_id, profile)

            # Check if we're in one-tab mode
            if socket.startswith("kc1-"):
                # One-tab mode: respawn with same session
                claude_config = str(session_config_dir)

                uid = os.getuid()
                launcher = Path(f"/tmp/kc-reload-{uid}.sh")
                launcher.write_text(f'''#!/bin/sh
export CLAUDE_CONFIG_DIR="{claude_config}"
cd "{current_dir}"
exec claude --resume {session_id}
''')
                launcher.chmod(0o755)

                # Schedule respawn after delay
                subprocess.Popen([
                    "sh", "-c",
                    f"sleep 1 && tmux -L {socket} respawn-pane -k {launcher}"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                send_tmux_message("✓ Reloading...", socket)
                response = {
                    "continue": False,
                    "stopReason": "✓ Reloading..."
                }
                print(json.dumps(response))
                return

            # Multi-tab mode: kill window and open new one with same session
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

            # Open new window with same session
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            cmd_parts = [kitty_claude_path]
            if profile:
                cmd_parts.extend(["--profile", profile])
            cmd_parts.extend(["--new-window", "--resume-session", session_id])
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

            send_tmux_message("✓ Reloaded with same session", socket)
            response = {
                "continue": False,
                "stopReason": "✓ Reloaded with same session"
            }
            print(json.dumps(response))
            return

        # Check for :clear command (formerly :restart)
        if prompt == ':clear':
            current_dir = input_data.get('cwd', os.getcwd())

            # Get kitty-claude executable path
            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

            # Check if we're in one-tab mode
            if socket.startswith("kc1-"):
                # One-tab mode: respawn with fresh session
                claude_config = os.environ.get('CLAUDE_CONFIG_DIR', str(claude_data_dir))

                uid = os.getuid()
                launcher = Path(f"/tmp/kc-clear-{uid}.sh")
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

                send_tmux_message("✓ Clearing session...", socket)
                response = {
                    "continue": False,
                    "stopReason": "✓ Clearing session..."
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

            send_tmux_message("✓ Starting fresh session", socket)
            response = {
                "continue": False,
                "stopReason": "✓ Starting fresh session"
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

        # Check for ::skills command (list kitty-claude skills)
        if prompt == '::skills':
            # Determine config directory based on profile
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            if profile:
                config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                config_dir = Path.home() / ".config" / "kitty-claude"

            kc_skills_dir = config_dir / "kc-skills"

            if not kc_skills_dir.exists() or not any(kc_skills_dir.iterdir()):
                message = "No kitty-claude skills found.\n\nCreate skills with ::skill <name>"
                send_tmux_message("📋 No KC skills found", socket)
                response = {"continue": False, "stopReason": message}
                print(json.dumps(response))
                return

            # List all skill files
            skills = []
            for skill_file in sorted(kc_skills_dir.iterdir()):
                if skill_file.is_file() and skill_file.suffix == '.md':
                    skill_name = skill_file.stem
                    skills.append(f"  ::{skill_name}")

            if skills:
                skills_text = "Available kitty-claude skills:\n\n" + "\n".join(skills)
                send_tmux_message(f"📋 Found {len(skills)} KC skills", socket)
            else:
                skills_text = "No kitty-claude skills found."
                send_tmux_message("📋 No KC skills found", socket)

            response = {"continue": False, "stopReason": skills_text}
            print(json.dumps(response))
            return

        # Check for ::skill command (create/edit kitty-claude skill)
        if prompt.startswith('::skill '):
            skill_name = prompt[8:].strip()

            if not skill_name:
                send_tmux_message("❌ Usage: ::skill <name>", socket)
                response = {"continue": False, "stopReason": "❌ Usage: ::skill <name>"}
                print(json.dumps(response))
                return

            # Validate skill name (alphanumeric, dash, underscore only)
            if not all(c.isalnum() or c in '-_' for c in skill_name):
                send_tmux_message("❌ Skill name can only contain letters, numbers, dash, underscore", socket)
                response = {"continue": False, "stopReason": "❌ Invalid skill name"}
                print(json.dumps(response))
                return

            try:
                # Determine config directory based on profile
                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
                else:
                    config_dir = Path.home() / ".config" / "kitty-claude"

                # Create kc-skills directory
                kc_skills_dir = config_dir / "kc-skills"
                kc_skills_dir.mkdir(parents=True, exist_ok=True)

                skill_file = kc_skills_dir / f"{skill_name}.md"

                # Create template if file doesn't exist
                if not skill_file.exists():
                    template = f"""# {skill_name}

Add your kitty-claude skill content here.
This will be injected as context when you run ::{skill_name}
"""
                    skill_file.write_text(template)

                # Open vim in tmux popup
                result = subprocess.run([
                    "tmux", "-L", socket,
                    "display-popup", "-E", "-w", "80%", "-h", "80%",
                    f"vim {skill_file}"
                ])

                send_tmux_message(f"✓ KC skill '{skill_name}' saved", socket)
                response = {"continue": False, "stopReason": f"✓ KC skill '{skill_name}' saved"}
            except Exception as e:
                send_tmux_message(f"❌ Error editing KC skill: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

        # Check for :: command (run kitty-claude skill)
        if prompt.startswith('::') and not prompt.startswith('::skill '):
            # Extract skill name (everything after :: up to first space or end)
            rest = prompt[2:]
            parts = rest.split(None, 1)
            skill_name = parts[0] if parts else ""
            rest_of_prompt = parts[1] if len(parts) > 1 else ""

            if not skill_name:
                send_tmux_message("❌ Usage: ::skill-name [your prompt]", socket)
                response = {"continue": False, "stopReason": "❌ Usage: ::skill-name [your prompt]"}
                print(json.dumps(response))
                return

            try:
                # Determine config directory based on profile
                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
                else:
                    config_dir = Path.home() / ".config" / "kitty-claude"

                kc_skills_dir = config_dir / "kc-skills"
                skill_file = kc_skills_dir / f"{skill_name}.md"

                if not skill_file.exists():
                    send_tmux_message(f"❌ KC skill '{skill_name}' not found", socket)
                    response = {"continue": False, "stopReason": f"❌ KC skill '{skill_name}' not found. Create it with ::skill {skill_name}"}
                    print(json.dumps(response))
                    return

                # Load skill content
                skill_content = skill_file.read_text().strip()

                # Inject context and continue
                send_tmux_message(f"📖 Loading KC skill '{skill_name}'...", socket)

                if rest_of_prompt:
                    modified_prompt = f"{rest_of_prompt}\n\n[Kitty-Claude Skill: {skill_name}]\n{skill_content}"
                else:
                    modified_prompt = f"[Kitty-Claude Skill: {skill_name}]\n{skill_content}"

                # Print modified prompt (goes to Claude) and return WITHOUT json response
                print(modified_prompt)
                return

            except Exception as e:
                send_tmux_message(f"❌ Error loading KC skill: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}
                print(json.dumps(response))
                return

        # Check for :note command
        if prompt == ':note' or prompt.startswith(':note '):
            session_id = input_data.get('session_id')
            try:
                open_session_notes(get_runtime_tmux_state_file, session_id=session_id)
                response = {"continue": False, "stopReason": "📝 Opening session notes..."}
            except Exception as e:
                send_tmux_message(f"❌ Error opening notes: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

        # Check for :skill command
        if prompt.startswith(':skill '):
            skill_name = prompt[7:].strip()

            if not skill_name:
                send_tmux_message("❌ Usage: :skill <name>", socket)
                response = {"continue": False, "stopReason": "❌ Usage: :skill <name>"}
                print(json.dumps(response))
                return

            # Validate skill name (alphanumeric, dash, underscore only)
            if not all(c.isalnum() or c in '-_' for c in skill_name):
                send_tmux_message("❌ Skill name can only contain letters, numbers, dash, underscore", socket)
                response = {"continue": False, "stopReason": "❌ Invalid skill name"}
                print(json.dumps(response))
                return

            try:
                # Create skill directory
                skills_dir = claude_data_dir / "skills" / skill_name
                skills_dir.mkdir(parents=True, exist_ok=True)

                skill_file = skills_dir / "SKILL.md"

                # Create template if file doesn't exist
                if not skill_file.exists():
                    template = f"""---
name: {skill_name}
description: Execute {skill_name}
---

Add your skill content here.
"""
                    skill_file.write_text(template)

                # Open vim in tmux popup
                result = subprocess.run([
                    "tmux", "-L", socket,
                    "display-popup", "-E", "-w", "80%", "-h", "80%",
                    f"vim {skill_file}"
                ])

                send_tmux_message(f"✓ Skill '{skill_name}' saved - use :reload to apply", socket)
                response = {"continue": False, "stopReason": f"✓ Skill '{skill_name}' saved\n\nUse :reload to make the skill available."}
            except Exception as e:
                send_tmux_message(f"❌ Error editing skill: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

        # Check for :rule command
        if prompt.startswith(':rule '):
            rule_name = prompt[6:].strip()

            if not rule_name:
                send_tmux_message("❌ Usage: :rule <name>", socket)
                response = {"continue": False, "stopReason": "❌ Usage: :rule <name>"}
                print(json.dumps(response))
                return

            # Validate rule name (alphanumeric, dash, underscore only)
            if not all(c.isalnum() or c in '-_' for c in rule_name):
                send_tmux_message("❌ Rule name can only contain letters, numbers, dash, underscore", socket)
                response = {"continue": False, "stopReason": "❌ Invalid rule name"}
                print(json.dumps(response))
                return

            try:
                # Determine config directory based on profile
                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
                else:
                    config_dir = Path.home() / ".config" / "kitty-claude"

                # Create rules directory
                rules_dir = config_dir / "rules"
                rules_dir.mkdir(parents=True, exist_ok=True)

                rule_file = rules_dir / f"{rule_name}.md"

                # Create template if file doesn't exist
                if not rule_file.exists():
                    template = f"""# {rule_name}

Add your rule content here. This will be included in CLAUDE.md.
"""
                    rule_file.write_text(template)

                # Open vim in tmux popup
                result = subprocess.run([
                    "tmux", "-L", socket,
                    "display-popup", "-E", "-w", "80%", "-h", "80%",
                    f"vim {rule_file}"
                ])

                send_tmux_message(f"✓ Rule '{rule_name}' saved - use :reload to apply", socket)
                response = {"continue": False, "stopReason": f"✓ Rule '{rule_name}' saved\n\nUse :reload to rebuild CLAUDE.md and pick up the new rule."}
            except Exception as e:
                send_tmux_message(f"❌ Error editing rule: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

        # Check for :mcp-shell command
        if prompt.startswith(':mcp-shell '):
            command_name = prompt[11:].strip()

            if not command_name:
                send_tmux_message("❌ Usage: :mcp-shell <command>", socket)
                response = {"continue": False, "stopReason": "❌ Usage: :mcp-shell <command>"}
                print(json.dumps(response))
                return

            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("❌ No session ID", socket)
                response = {"continue": False, "stopReason": "❌ No session ID found"}
                print(json.dumps(response))
                return

            try:
                # Debug logging to /tmp
                with open("/tmp/mcp-shell-debug.log", "a") as f:
                    f.write(f"command_name={command_name}\n")
                    f.write(f"PATH={os.environ.get('PATH', 'NO PATH')[:500]}\n")

                # Find command in PATH
                command_path = shutil.which(command_name)

                with open("/tmp/mcp-shell-debug.log", "a") as f:
                    f.write(f"which result={command_path}\n")
                    f.write(f"type={type(command_path)}\n")
                    f.write(f"repr={repr(command_path)}\n")
                    f.write(f"bool={bool(command_path)}\n")
                    f.write(f"checking: not command_path = {not command_path}\n\n")

                if not command_path:
                    send_tmux_message(f"❌ Command '{command_name}' not found in PATH", socket)
                    response = {"continue": False, "stopReason": f"❌ Command '{command_name}' not found in PATH"}
                    print(json.dumps(response))
                    return

                # Get command help to extract description
                with open("/tmp/mcp-shell-debug.log", "a") as f:
                    f.write(f"About to run: {[command_path, '--help']}\n")

                help_result = subprocess.run(
                    [command_path, "--help"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )

                with open("/tmp/mcp-shell-debug.log", "a") as f:
                    f.write(f"subprocess completed successfully\n")
                # Use first line of help as description, or fallback
                help_lines = (help_result.stdout or help_result.stderr or "").strip().split('\n')
                description = help_lines[0] if help_lines and help_lines[0] else f"Execute {command_name}"
                # Truncate if too long
                if len(description) > 100:
                    description = description[:97] + "..."

                # Determine config directory for current session
                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
                else:
                    config_dir = Path.home() / ".config" / "kitty-claude"

                session_config_dir = config_dir / "session-configs" / session_id
                session_config_dir.mkdir(parents=True, exist_ok=True)

                # Get current working directory
                cwd = input_data.get('cwd', os.getcwd())

                # Load session-specific .claude.json
                claude_json_file = session_config_dir / ".claude.json"
                if claude_json_file.exists():
                    claude_config = json.loads(claude_json_file.read_text())
                else:
                    claude_config = {}

                # Ensure projects structure exists
                if "projects" not in claude_config:
                    claude_config["projects"] = {}
                if cwd not in claude_config["projects"]:
                    claude_config["projects"][cwd] = {}
                if "mcpServers" not in claude_config["projects"][cwd]:
                    claude_config["projects"][cwd]["mcpServers"] = {}

                # Add the mcp-exec server using kitty-claude --mcp-exec
                server_name = f"shell-{command_name}"
                kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

                claude_config["projects"][cwd]["mcpServers"][server_name] = {
                    "type": "stdio",
                    "command": kitty_claude_path,
                    "args": [
                        "--mcp-exec",
                        command_name,  # Use command name, not full path, for valid MCP tool name
                        description,
                        "--pos-arg", "input Input data"
                    ]
                }

                # Save session .claude.json
                claude_json_file.write_text(json.dumps(claude_config, indent=2))

                send_tmux_message(f"✓ MCP server '{server_name}' added - use :reload", socket)
                response = {
                    "continue": False,
                    "stopReason": f"✓ MCP server '{server_name}' added\n\nUse :reload to start Claude with the new MCP server."
                }
            except subprocess.TimeoutExpired:
                send_tmux_message(f"❌ Command '{command_name}' timed out", socket)
                response = {"continue": False, "stopReason": f"❌ Command '{command_name}' help timed out"}
            except Exception as e:
                with open("/tmp/mcp-shell-debug.log", "a") as f:
                    f.write(f"Exception: {type(e).__name__}: {e}\n")
                send_tmux_message(f"❌ Error: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

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