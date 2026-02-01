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


def push_dir_stack(session_id, directory):
    """Push a directory onto the session's directory stack."""
    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"
    metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    stack = metadata.get("dir_stack", [])
    stack.append(directory)
    metadata["dir_stack"] = stack
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    metadata_file.write_text(json.dumps(metadata, indent=2))


def pop_dir_stack(session_id):
    """Pop a directory from the session's directory stack. Returns None if empty."""
    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"
    if not metadata_file.exists():
        return None
    metadata = json.loads(metadata_file.read_text())
    stack = metadata.get("dir_stack", [])
    if not stack:
        return None
    directory = stack.pop()
    metadata["dir_stack"] = stack
    metadata_file.write_text(json.dumps(metadata, indent=2))
    return directory


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

    # Carry over session-level state from old session
    state_dir = get_state_dir()
    old_meta_file = state_dir / "sessions" / f"{old_session_id}.json"
    new_meta_file = state_dir / "sessions" / f"{new_session_id}.json"
    if old_meta_file.exists() and new_meta_file.exists():
        try:
            old_meta = json.loads(old_meta_file.read_text())
            new_meta = json.loads(new_meta_file.read_text())
            for key in ("dir_stack", "mcpServers", "linked_tmux_window", "linked_tmux_windows"):
                if key in old_meta:
                    new_meta[key] = old_meta[key]
            new_meta_file.write_text(json.dumps(new_meta, indent=2))
        except:
            pass

    # Get kitty-claude executable path
    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

    # Check if we're in one-tab mode (socket starts with kc1-)
    if socket.startswith("kc1-"):
        # One-tab mode: use a temp launcher script to avoid shell quoting issues
        claude_config = os.environ.get('CLAUDE_CONFIG_DIR', str(claude_data_dir))

        # Get claude binary path from config, or resolve from PATH
        profile = os.environ.get('KITTY_CLAUDE_PROFILE')
        if profile:
            config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
        else:
            config_dir = Path.home() / ".config" / "kitty-claude"
        config_file = config_dir / "config.json"
        claude_bin = None
        if config_file.exists():
            try:
                config = json.loads(config_file.read_text())
                if config.get("claude_binary"):
                    claude_bin = config["claude_binary"]
            except:
                pass

        # If not configured, try to find it in current PATH and use full path
        if not claude_bin:
            claude_bin = shutil.which("claude") or "claude"

        # Log for debugging
        log(f"one-tab :cd - config={claude_config}, session={new_session_id}, target={target_dir}")
        log(f"one-tab :cd - claude binary: {claude_bin}")
        session_file = target_projects_dir / f"{new_session_id}.jsonl"
        log(f"one-tab :cd - session file exists: {session_file.exists()}")

        # Write a launcher script (avoids all tmux shell quoting issues)
        uid = os.getuid()
        launcher = Path(f"/tmp/kc-cd-{uid}-{new_session_id[:8]}.sh")
        launcher.write_text(f'''#!/bin/sh
export CLAUDE_CONFIG_DIR="{claude_config}"
cd "{target_dir}"
exec "{claude_bin}" --resume {new_session_id}
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
:kitty-commands      Enable kitty-claude command MCP server and reload
:plan / :god         Enable planning MCP server (session overview) and reload
::skills             List all kitty-claude skills
::skill <name>       Create/edit a kitty-claude skill
::<skill> [prompt]   Run kitty-claude skill (injects context)
:mcp <cmd> [args]    Add a native MCP server to this session
:mcp-shell <cmd>     Expose shell command as MCP server
:roles               List available roles
:role <name>         Load a role's MCP servers into session
:save-role <name>    Save current session's MCP servers as a role
:send <message>      Send a message to another kitty-claude window (fzf)
:current-sessions    List all currently running sessions
:sessions            List recent sessions (last 10)
:resume <num|id>     Resume a session in new window
:clear               Clear session and start fresh
:reload              Reload Claude (same session, pick up config changes)
:cd <path>           Change directory and move session
:cdpop               Return to previous directory
:cd-tmux             Change to directory of tmux session 0
:tmux                Link/switch to a tmux window on default server
:tmux-unlink         Unlink the associated tmux window
:tmuxpath            Show path of linked tmux window
:tmuxs-link          Add current tmux window to linked windows list
:tmuxs               Pick a linked tmux window (fzf)
:call                Open popup with context, returns result
:ask                 Open popup without context, returns result
:fork                Clone conversation to new window (independent)
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
            # Find plugin commands on PATH
            plugins = set()
            for path_dir in os.environ.get("PATH", "").split(os.pathsep):
                try:
                    for entry in Path(path_dir).iterdir():
                        if entry.name.startswith("kitty-claude-") and os.access(entry, os.X_OK):
                            cmd_name = entry.name[len("kitty-claude-"):]
                            plugins.add(cmd_name)
                except (OSError, PermissionError):
                    pass

            if plugins:
                help_text += "\nPlugins (from PATH):\n"
                for name in sorted(plugins):
                    help_text += f"  :{name:<20s} (kitty-claude-{name})\n"

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

        # Check for :call command
        if prompt.startswith(':call'):
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

            # Generate new call session ID
            call_session_id = str(uuid.uuid4())

            # Clone session for call
            call_file = projects_dir / f"{call_session_id}.jsonl"
            shutil.copy2(session_files[0], call_file)

            send_tmux_message("📞 Opening call in popup...", socket)

            # Open call in popup (blocking call)
            run([
                "tmux", "-L", socket,
                "display-popup", "-E", "-w", "90%", "-h", "90%",
                f"claude --resume {call_session_id}"
            ])

            # Popup closed - get last assistant message from call
            try:
                from kitty_claude.session_utils import get_last_assistant_message
                last_message = get_last_assistant_message(call_file)

                if last_message:
                    send_tmux_message("✓ Call completed, injecting response", socket)

                    # Escape the message for shell safety
                    call_message = f"Call result:\n\n{last_message}"
                    escaped_message = shlex.quote(call_message)

                    # Background process: sleep then type the call result
                    subprocess.Popen([
                        "sh", "-c",
                        f"sleep 0.5 && tmux -L {socket} send-keys -l {escaped_message} && tmux -L {socket} send-keys Enter"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    # Return immediately to unblock the hook
                    response = {"continue": False, "stopReason": ""}
                    print(json.dumps(response))
                else:
                    send_tmux_message("⚠ Call had no assistant messages", socket)
                    response = {"continue": False, "stopReason": "Call had no responses"}
                    print(json.dumps(response))

            except Exception as e:
                send_tmux_message(f"❌ Error reading call: {str(e)}", socket)
                response = {"continue": False, "stopReason": f"Call error: {str(e)}"}
                print(json.dumps(response))

            return

        # Check for :ask command
        if prompt.startswith(':ask'):
            current_dir = input_data.get('cwd', os.getcwd())

            # Generate new ask session ID
            ask_session_id = str(uuid.uuid4())

            # Encode path for projects directory
            encoded_current = current_dir.replace('/', '-')
            projects_dir = claude_data_dir / "projects" / encoded_current

            # Create fresh session file (no context copying)
            projects_dir.mkdir(parents=True, exist_ok=True)
            ask_file = projects_dir / f"{ask_session_id}.jsonl"
            # Create empty session file
            ask_file.touch()

            send_tmux_message("❓ Opening ask in popup...", socket)

            # Open ask in popup (blocking call)
            run([
                "tmux", "-L", socket,
                "display-popup", "-E", "-w", "90%", "-h", "90%",
                f"claude --resume {ask_session_id}"
            ])

            # Popup closed - get last assistant message from ask
            try:
                from kitty_claude.session_utils import get_last_assistant_message
                last_message = get_last_assistant_message(ask_file)

                if last_message:
                    send_tmux_message("✓ Ask completed, injecting response", socket)

                    # Escape the message for shell safety
                    ask_message = f"Ask result:\n\n{last_message}"
                    escaped_message = shlex.quote(ask_message)

                    # Background process: sleep then type the ask result
                    subprocess.Popen([
                        "sh", "-c",
                        f"sleep 0.5 && tmux -L {socket} send-keys -l {escaped_message} && tmux -L {socket} send-keys Enter"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    # Return immediately to unblock the hook
                    response = {"continue": False, "stopReason": ""}
                    print(json.dumps(response))
                else:
                    send_tmux_message("⚠ Ask had no assistant messages", socket)
                    response = {"continue": False, "stopReason": "Ask had no responses"}
                    print(json.dumps(response))

            except Exception as e:
                send_tmux_message(f"❌ Error reading ask: {str(e)}", socket)
                response = {"continue": False, "stopReason": f"Ask error: {str(e)}"}
                print(json.dumps(response))

            return

        # Check for :fork command
        if prompt.startswith(':fork'):
            current_dir = input_data.get('cwd', os.getcwd())
            session_id = input_data.get('session_id')
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')

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

            send_tmux_message("🔀 Forking to new window...", socket)

            # Open fork in new window (independent)
            from kitty_claude.claude import new_window
            new_window(profile=profile, resume_session_id=fork_session_id, socket=socket)

            send_tmux_message(f"✓ Forked to session {fork_session_id[:8]}...", socket)
            response = {"continue": False, "stopReason": f"✓ Forked conversation to new window"}
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

            # Push current directory onto stack
            session_id = input_data.get('session_id')
            if session_id:
                push_dir_stack(session_id, current_dir)

            # Use the shared session cloning logic
            response = clone_session_and_change_directory(
                target_dir, current_dir, input_data, claude_data_dir, socket
            )
            print(json.dumps(response))
            return

        # Check for :tmux command - link/switch to a tmux window on the "default" server
        if prompt == ':tmux':
            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("❌ No session ID", socket)
                response = {"continue": False, "stopReason": "❌ No session ID"}
                print(json.dumps(response))
                return

            state_dir = get_state_dir()
            metadata_file = state_dir / "sessions" / f"{session_id}.json"

            try:
                if metadata_file.exists():
                    metadata = json.loads(metadata_file.read_text())
                else:
                    metadata = {}

                linked_window = metadata.get("linked_tmux_window")

                if linked_window:
                    # Already linked - switch to that window
                    try:
                        run(
                            ["tmux", "-L", "default", "select-window", "-t", linked_window],
                            capture_output=True, text=True, check=True
                        )
                        send_tmux_message(f"✓ Switched to tmux window {linked_window}", socket)
                        response = {"continue": False, "stopReason": f"✓ Switched to tmux window {linked_window}"}
                    except subprocess.CalledProcessError:
                        send_tmux_message(f"❌ Linked window {linked_window} not found - use :tmux-unlink to reset", socket)
                        response = {"continue": False, "stopReason": f"❌ Linked window {linked_window} not found"}
                else:
                    # Not linked - find current active window on default tmux and link it
                    try:
                        result = run(
                            ["tmux", "-L", "default", "display-message", "-p", "#{window_id}:#{window_name}"],
                            capture_output=True, text=True, check=True
                        )
                        parts = result.stdout.strip().split(":", 1)
                        window_id = parts[0]
                        window_name = parts[1] if len(parts) > 1 else window_id

                        metadata["linked_tmux_window"] = window_id
                        metadata_file.parent.mkdir(parents=True, exist_ok=True)
                        metadata_file.write_text(json.dumps(metadata, indent=2))

                        send_tmux_message(f"✓ Linked to tmux window '{window_name}' ({window_id})", socket)
                        response = {"continue": False, "stopReason": f"✓ Linked to tmux window '{window_name}' ({window_id})"}
                    except subprocess.CalledProcessError:
                        send_tmux_message("❌ Could not access default tmux server", socket)
                        response = {"continue": False, "stopReason": "❌ Could not access default tmux server"}
            except Exception as e:
                send_tmux_message(f"❌ Error: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

        # Check for :tmux-unlink command
        if prompt == ':tmux-unlink':
            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("❌ No session ID", socket)
                response = {"continue": False, "stopReason": "❌ No session ID"}
                print(json.dumps(response))
                return

            state_dir = get_state_dir()
            metadata_file = state_dir / "sessions" / f"{session_id}.json"

            try:
                if metadata_file.exists():
                    metadata = json.loads(metadata_file.read_text())
                    if "linked_tmux_window" in metadata:
                        del metadata["linked_tmux_window"]
                        metadata_file.write_text(json.dumps(metadata, indent=2))
                        send_tmux_message("✓ Unlinked tmux window", socket)
                        response = {"continue": False, "stopReason": "✓ Unlinked tmux window"}
                    else:
                        send_tmux_message("No tmux window linked", socket)
                        response = {"continue": False, "stopReason": "No tmux window linked"}
                else:
                    send_tmux_message("No tmux window linked", socket)
                    response = {"continue": False, "stopReason": "No tmux window linked"}
            except Exception as e:
                send_tmux_message(f"❌ Error: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

        # :tmuxpath - show the current path of the linked tmux window
        if prompt == ':tmuxpath':
            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("❌ No session ID", socket)
                response = {"continue": False, "stopReason": "❌ No session ID"}
                print(json.dumps(response))
                return

            state_dir = get_state_dir()
            metadata_file = state_dir / "sessions" / f"{session_id}.json"

            try:
                if metadata_file.exists():
                    metadata = json.loads(metadata_file.read_text())
                else:
                    metadata = {}

                linked_window = metadata.get("linked_tmux_window")

                if not linked_window:
                    send_tmux_message("No tmux window linked. Use :tmux first.", socket)
                    response = {"continue": False, "stopReason": "No tmux window linked. Use :tmux to link a window first."}
                    print(json.dumps(response))
                    return

                result = run(
                    ["tmux", "-L", "default", "display-message", "-p", "-t", linked_window, "#{pane_current_path}"],
                    capture_output=True, text=True, check=True
                )
                path = result.stdout.strip()
                if path:
                    send_tmux_message(f"Linked tmux window path: {path}", socket)
                    response = {"continue": False, "stopReason": f"The linked tmux window ({linked_window}) is at: {path}"}
                else:
                    send_tmux_message("❌ Could not get path from linked window", socket)
                    response = {"continue": False, "stopReason": "❌ Could not get path from linked tmux window"}
            except subprocess.CalledProcessError:
                send_tmux_message(f"❌ Linked window {linked_window} not found", socket)
                response = {"continue": False, "stopReason": f"❌ Linked window {linked_window} not found - use :tmux-unlink to reset"}
            except Exception as e:
                send_tmux_message(f"❌ Error: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

        # :tmuxs-link - add current default tmux window to list of linked windows
        if prompt == ':tmuxs-link':
            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("❌ No session ID", socket)
                response = {"continue": False, "stopReason": "❌ No session ID"}
                print(json.dumps(response))
                return

            try:
                result = run(
                    ["tmux", "-L", "default", "display-message", "-p", "#{window_id}:#{window_name}"],
                    capture_output=True, text=True, check=True
                )
                parts = result.stdout.strip().split(":", 1)
                window_id = parts[0]
                window_name = parts[1] if len(parts) > 1 else window_id

                state_dir = get_state_dir()
                metadata_file = state_dir / "sessions" / f"{session_id}.json"
                metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
                linked = metadata.get("linked_tmux_windows", [])

                # Don't add duplicates
                if not any(w["id"] == window_id for w in linked):
                    linked.append({"id": window_id, "name": window_name})
                    metadata["linked_tmux_windows"] = linked
                    metadata_file.parent.mkdir(parents=True, exist_ok=True)
                    metadata_file.write_text(json.dumps(metadata, indent=2))
                    send_tmux_message(f"✓ Added tmux window '{window_name}' ({window_id})", socket)
                    response = {"continue": False, "stopReason": f"✓ Added tmux window '{window_name}' ({window_id})"}
                else:
                    send_tmux_message(f"Already linked: '{window_name}' ({window_id})", socket)
                    response = {"continue": False, "stopReason": f"Already linked: '{window_name}' ({window_id})"}
            except subprocess.CalledProcessError:
                send_tmux_message("❌ Could not access default tmux server", socket)
                response = {"continue": False, "stopReason": "❌ Could not access default tmux server"}
            except Exception as e:
                send_tmux_message(f"❌ Error: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

        # :tmuxs - pick a linked tmux window via fzf popup
        if prompt == ':tmuxs':
            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("❌ No session ID", socket)
                response = {"continue": False, "stopReason": "❌ No session ID"}
                print(json.dumps(response))
                return

            try:
                state_dir = get_state_dir()
                metadata_file = state_dir / "sessions" / f"{session_id}.json"
                metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
                linked = metadata.get("linked_tmux_windows", [])

                if not linked:
                    send_tmux_message("No linked windows. Use :tmuxs-link first.", socket)
                    response = {"continue": False, "stopReason": "No linked windows. Use :tmuxs-link to add windows."}
                    print(json.dumps(response))
                    return

                # Build fzf input: "window_id\twindow_name"
                fzf_input = "\n".join(f"{w['id']}\t{w['name']}" for w in linked)

                # Write to temp file for the popup script
                uid = os.getuid()
                tmp_input = Path(f"/tmp/kc-tmuxs-{uid}.txt")
                tmp_output = Path(f"/tmp/kc-tmuxs-{uid}-out.txt")
                tmp_input.write_text(fzf_input)
                tmp_output.unlink(missing_ok=True)

                # Run fzf in tmux popup
                subprocess.run([
                    "tmux", "-L", socket,
                    "display-popup", "-E", "-w", "60%", "-h", "40%",
                    f"cat {tmp_input} | fzf --delimiter='\\t' --with-nth=2 --header='Select window' > {tmp_output}"
                ])

                if tmp_output.exists():
                    selected = tmp_output.read_text().strip()
                    if selected:
                        window_id = selected.split("\t")[0]
                        try:
                            run(
                                ["tmux", "-L", "default", "select-window", "-t", window_id],
                                capture_output=True, text=True, check=True
                            )
                            send_tmux_message(f"✓ Switched to {window_id}", socket)
                            response = {"continue": False, "stopReason": f"✓ Switched to {window_id}"}
                        except subprocess.CalledProcessError:
                            send_tmux_message(f"❌ Window {window_id} not found", socket)
                            response = {"continue": False, "stopReason": f"❌ Window {window_id} not found"}
                    else:
                        response = {"continue": False, "stopReason": "Cancelled"}
                else:
                    response = {"continue": False, "stopReason": "Cancelled"}

                tmp_input.unlink(missing_ok=True)
                tmp_output.unlink(missing_ok=True)
            except Exception as e:
                send_tmux_message(f"❌ Error: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

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

                # Get claude binary path from config (don't rely on PATH)
                if profile:
                    config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
                else:
                    config_dir = Path.home() / ".config" / "kitty-claude"
                config_file = config_dir / "config.json"
                claude_bin = None
                if config_file.exists():
                    try:
                        config = json.loads(config_file.read_text())
                        if config.get("claude_binary"):
                            claude_bin = config["claude_binary"]
                    except:
                        pass
                if not claude_bin:
                    claude_bin = shutil.which("claude") or "claude"

                uid = os.getuid()
                launcher = Path(f"/tmp/kc-reload-{uid}.sh")
                launcher.write_text(f'''#!/bin/sh
export CLAUDE_CONFIG_DIR="{claude_config}"
cd "{current_dir}"
exec "{claude_bin}" --resume {session_id}
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

            # Push current directory onto stack
            session_id = input_data.get('session_id')
            if session_id:
                push_dir_stack(session_id, current_dir)

            # Use the shared session cloning logic
            response = clone_session_and_change_directory(
                target_dir, current_dir, input_data, claude_data_dir, socket
            )
            print(json.dumps(response))
            return

        # Check for :cdpop command - pop directory stack
        if prompt == ':cdpop':
            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("❌ No session ID", socket)
                response = {"continue": False, "stopReason": "❌ No session ID"}
                print(json.dumps(response))
                return

            target_dir = pop_dir_stack(session_id)
            if not target_dir:
                send_tmux_message("❌ Directory stack is empty", socket)
                response = {"continue": False, "stopReason": "❌ Directory stack is empty"}
                print(json.dumps(response))
                return

            if not os.path.isdir(target_dir):
                send_tmux_message(f"❌ Directory does not exist: {target_dir}", socket)
                response = {"continue": False, "stopReason": f"❌ Directory does not exist: {target_dir}"}
                print(json.dumps(response))
                return

            current_dir = input_data.get('cwd', os.getcwd())

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

        # Check for :kitty-commands command - enable command MCP server and reload
        if prompt == ':kitty-commands':
            current_dir = input_data.get('cwd', os.getcwd())
            session_id = input_data.get('session_id')

            if not session_id:
                send_tmux_message("❌ No session ID available", socket)
                response = {"continue": False, "stopReason": "❌ No session ID available"}
                print(json.dumps(response))
                return

            # The command MCP server is auto-registered by setup_session_config,
            # so we just need to reload to pick it up.
            send_tmux_message("✓ Enabling kitty-claude commands MCP. Reloading...", socket)

            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            build_claude_md(profile)

            from kitty_claude.claude import save_auth_from_session, setup_session_config
            save_auth_from_session(session_id, profile)
            session_config_dir = setup_session_config(session_id, profile)

            # Reload (same logic as :reload)
            if socket.startswith("kc1-"):
                claude_config = str(session_config_dir)
                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
                else:
                    config_dir = Path.home() / ".config" / "kitty-claude"
                config_file = config_dir / "config.json"
                claude_bin = None
                if config_file.exists():
                    try:
                        config = json.loads(config_file.read_text())
                        if config.get("claude_binary"):
                            claude_bin = config["claude_binary"]
                    except:
                        pass
                if not claude_bin:
                    claude_bin = shutil.which("claude") or "claude"

                uid = os.getuid()
                launcher = Path(f"/tmp/kc-cmds-{uid}.sh")
                launcher.write_text(f'''#!/bin/sh
export CLAUDE_CONFIG_DIR="{claude_config}"
cd "{current_dir}"
exec "{claude_bin}" --resume {session_id}
''')
                launcher.chmod(0o755)

                subprocess.Popen([
                    "sh", "-c",
                    f"sleep 1 && tmux -L {socket} respawn-pane -k {launcher}"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                response = {"continue": False, "stopReason": "✓ kitty-claude commands enabled. Reloading..."}
                print(json.dumps(response))
                return

            # Multi-tab mode
            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
            try:
                result = run(
                    ["tmux", "-L", socket, "display-message", "-p", "#{window_id}"],
                    capture_output=True, text=True, check=True
                )
                current_window_id = result.stdout.strip()
            except:
                current_window_id = None

            cmd_parts = [kitty_claude_path]
            if profile:
                cmd_parts.extend(["--profile", profile])
            cmd_parts.extend(["--new-window", "--resume-session", session_id])
            cmd_str = " ".join(cmd_parts)

            run([
                "tmux", "-L", socket,
                "new-window", "-c", current_dir, cmd_str
            ])

            if current_window_id:
                subprocess.Popen([
                    "sh", "-c",
                    f"sleep 0.5 && tmux -L {socket} kill-window -t {current_window_id}"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            response = {"continue": False, "stopReason": "✓ kitty-claude commands enabled. Reloading..."}
            print(json.dumps(response))
            return

        # Check for :god, :planner, or :plan command
        if prompt in [':god', ':planner', ':plan']:
            current_dir = input_data.get('cwd', os.getcwd())
            session_id = input_data.get('session_id')

            if not session_id:
                send_tmux_message("❌ No session ID available", socket)
                response = {"continue": False, "stopReason": "❌ No session ID available"}
                print(json.dumps(response))
                return

            # Create/update .mcp.json in current directory
            mcp_config_file = Path(current_dir) / ".mcp.json"

            # Get kitty-claude path
            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

            # Load existing config or create new
            if mcp_config_file.exists():
                try:
                    mcp_config = json.loads(mcp_config_file.read_text())
                except:
                    mcp_config = {"mcpServers": {}}
            else:
                mcp_config = {"mcpServers": {}}

            # Add planning MCP server
            mcp_config.setdefault("mcpServers", {})
            mcp_config["mcpServers"]["kitty-claude-planning"] = {
                "command": kitty_claude_path,
                "args": ["--plan-mcp"]
            }

            # Write config
            try:
                mcp_config_file.write_text(json.dumps(mcp_config, indent=2) + "\n")
                send_tmux_message("✓ Planning MCP enabled. Reloading...", socket)
            except Exception as e:
                send_tmux_message(f"❌ Error writing .mcp.json: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}
                print(json.dumps(response))
                return

            # Now trigger reload logic (same as :reload command)
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            build_claude_md(profile)

            from kitty_claude.claude import save_auth_from_session, setup_session_config
            save_auth_from_session(session_id, profile)
            session_config_dir = setup_session_config(session_id, profile)

            # Check if we're in one-tab mode
            if socket.startswith("kc1-"):
                # One-tab mode: respawn with same session
                claude_config = str(session_config_dir)

                # Get claude binary path
                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
                else:
                    config_dir = Path.home() / ".config" / "kitty-claude"
                config_file = config_dir / "config.json"
                claude_bin = None
                if config_file.exists():
                    try:
                        config = json.loads(config_file.read_text())
                        if config.get("claude_binary"):
                            claude_bin = config["claude_binary"]
                    except:
                        pass
                if not claude_bin:
                    claude_bin = shutil.which("claude") or "claude"

                uid = os.getuid()
                launcher = Path(f"/tmp/kc-god-{uid}.sh")
                launcher.write_text(f'''#!/bin/sh
export CLAUDE_CONFIG_DIR="{claude_config}"
cd "{current_dir}"
exec "{claude_bin}" --resume {session_id}
''')
                launcher.chmod(0o755)

                # Schedule respawn after delay
                subprocess.Popen([
                    "sh", "-c",
                    f"sleep 1 && tmux -L {socket} respawn-pane -k {launcher}"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                response = {"continue": False, "stopReason": "✓ God mode enabled. Reloading..."}
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

            # Open new window with same session
            cmd_parts = [kitty_claude_path]
            if profile:
                cmd_parts.extend(["--profile", profile])
            cmd_parts.extend(["--new-window", "--resume-session", session_id])
            cmd_str = " ".join(cmd_parts)

            run([
                "tmux", "-L", socket,
                "new-window", "-c", current_dir, cmd_str
            ])

            # Kill old window after brief delay
            if current_window_id:
                subprocess.Popen([
                    "sh", "-c",
                    f"sleep 0.5 && tmux -L {socket} kill-window -t {current_window_id}"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            response = {"continue": False, "stopReason": "✓ God mode enabled. Reloading..."}
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
        # :mcp <command> [args...] - add a native stdio MCP server to this session
        if prompt.startswith(':mcp '):
            parts = prompt[5:].strip().split()

            if not parts:
                send_tmux_message("❌ Usage: :mcp <command> [args...]", socket)
                response = {"continue": False, "stopReason": "❌ Usage: :mcp <command> [args...]"}
                print(json.dumps(response))
                return

            command_name = parts[0]
            extra_args = parts[1:]

            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("❌ No session ID", socket)
                response = {"continue": False, "stopReason": "❌ No session ID found"}
                print(json.dumps(response))
                return

            try:
                command_path = shutil.which(command_name)
                if not command_path:
                    send_tmux_message(f"❌ Command '{command_name}' not found in PATH", socket)
                    response = {"continue": False, "stopReason": f"❌ Command '{command_name}' not found in PATH"}
                    print(json.dumps(response))
                    return

                server_name = command_name.rsplit("/", 1)[-1]
                server_entry = {
                    "type": "stdio",
                    "command": command_path,
                }
                if extra_args:
                    server_entry["args"] = extra_args

                # Store in session metadata
                state_dir = get_state_dir()
                metadata_file = state_dir / "sessions" / f"{session_id}.json"
                metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
                if "mcpServers" not in metadata:
                    metadata["mcpServers"] = {}
                metadata["mcpServers"][server_name] = server_entry
                metadata_file.parent.mkdir(parents=True, exist_ok=True)
                metadata_file.write_text(json.dumps(metadata, indent=2))

                send_tmux_message(f"✓ MCP server '{server_name}' added - use :reload", socket)
                response = {
                    "continue": False,
                    "stopReason": f"✓ MCP server '{server_name}' added\n\nUse :reload to start Claude with the new MCP server."
                }
            except Exception as e:
                send_tmux_message(f"❌ Error: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

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

                # Add the mcp-exec server using kitty-claude --mcp-exec
                server_name = f"shell-{command_name}"
                kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

                server_entry = {
                    "type": "stdio",
                    "command": kitty_claude_path,
                    "args": [
                        "--mcp-exec",
                        command_name,
                        description,
                        "--pos-arg", "input Input data"
                    ]
                }

                # Store in session metadata
                state_dir = get_state_dir()
                metadata_file = state_dir / "sessions" / f"{session_id}.json"
                metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
                if "mcpServers" not in metadata:
                    metadata["mcpServers"] = {}
                metadata["mcpServers"][server_name] = server_entry
                metadata_file.parent.mkdir(parents=True, exist_ok=True)
                metadata_file.write_text(json.dumps(metadata, indent=2))

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

        # :roles - list available roles
        if prompt == ':roles':
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            if profile:
                config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                config_dir = Path.home() / ".config" / "kitty-claude"

            roles_dir = config_dir / "mcp-roles"
            if not roles_dir.exists() or not any(roles_dir.glob("*.json")):
                send_tmux_message("No roles found", socket)
                response = {"continue": False, "stopReason": "No roles found. Use :save-role <name> to create one."}
                print(json.dumps(response))
                return

            lines = []
            for role_file in sorted(roles_dir.glob("*.json")):
                try:
                    role = json.loads(role_file.read_text())
                    servers = list(role.get("mcpServers", {}).keys())
                    lines.append(f"  {role_file.stem}: {', '.join(servers)}")
                except:
                    lines.append(f"  {role_file.stem}: (error reading)")

            message = "Roles:\n" + "\n".join(lines)
            send_tmux_message(f"📋 {len(lines)} roles", socket)
            response = {"continue": False, "stopReason": message}
            print(json.dumps(response))
            return

        # :save-role <name> - save current session MCP servers as a named role
        if prompt.startswith(':save-role '):
            role_name = prompt[11:].strip()

            if not role_name:
                send_tmux_message("❌ Usage: :save-role <name>", socket)
                response = {"continue": False, "stopReason": "❌ Usage: :save-role <name>"}
                print(json.dumps(response))
                return

            if not all(c.isalnum() or c in '-_' for c in role_name):
                send_tmux_message("❌ Role name can only contain letters, numbers, dash, underscore", socket)
                response = {"continue": False, "stopReason": "❌ Invalid role name"}
                print(json.dumps(response))
                return

            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("❌ No session ID", socket)
                response = {"continue": False, "stopReason": "❌ No session ID"}
                print(json.dumps(response))
                return

            try:
                state_dir = get_state_dir()
                metadata_file = state_dir / "sessions" / f"{session_id}.json"
                metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
                mcp_servers = metadata.get("mcpServers", {})

                if not mcp_servers:
                    send_tmux_message("❌ No MCP servers in current session", socket)
                    response = {"continue": False, "stopReason": "❌ No MCP servers in current session to save"}
                    print(json.dumps(response))
                    return

                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
                else:
                    config_dir = Path.home() / ".config" / "kitty-claude"

                roles_dir = config_dir / "mcp-roles"
                roles_dir.mkdir(parents=True, exist_ok=True)

                role_file = roles_dir / f"{role_name}.json"
                role_file.write_text(json.dumps({"mcpServers": mcp_servers}, indent=2))

                server_names = ", ".join(mcp_servers.keys())
                send_tmux_message(f"✓ Role '{role_name}' saved ({server_names})", socket)
                response = {"continue": False, "stopReason": f"✓ Role '{role_name}' saved with: {server_names}"}
            except Exception as e:
                send_tmux_message(f"❌ Error: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

        # :role <name> - load a role's MCP servers into the current session
        if prompt.startswith(':role '):
            role_name = prompt[6:].strip()

            if not role_name:
                send_tmux_message("❌ Usage: :role <name>", socket)
                response = {"continue": False, "stopReason": "❌ Usage: :role <name>"}
                print(json.dumps(response))
                return

            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("❌ No session ID", socket)
                response = {"continue": False, "stopReason": "❌ No session ID"}
                print(json.dumps(response))
                return

            try:
                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
                else:
                    config_dir = Path.home() / ".config" / "kitty-claude"

                role_file = config_dir / "mcp-roles" / f"{role_name}.json"
                if not role_file.exists():
                    send_tmux_message(f"❌ Role '{role_name}' not found", socket)
                    response = {"continue": False, "stopReason": f"❌ Role '{role_name}' not found. Use :roles to list."}
                    print(json.dumps(response))
                    return

                role = json.loads(role_file.read_text())
                role_servers = role.get("mcpServers", {})

                # Merge into session metadata
                state_dir = get_state_dir()
                metadata_file = state_dir / "sessions" / f"{session_id}.json"
                metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
                if "mcpServers" not in metadata:
                    metadata["mcpServers"] = {}
                metadata["mcpServers"].update(role_servers)
                metadata_file.write_text(json.dumps(metadata, indent=2))

                server_names = ", ".join(role_servers.keys())
                send_tmux_message(f"✓ Role '{role_name}' loaded - use :reload", socket)
                response = {
                    "continue": False,
                    "stopReason": f"✓ Role '{role_name}' loaded ({server_names})\n\nUse :reload to start Claude with the new MCP servers."
                }
            except Exception as e:
                send_tmux_message(f"❌ Error: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

        # :send <message> - send a message to another kitty-claude window (picked via fzf)
        if prompt.startswith(':send '):
            message = prompt[6:].strip()
            if not message:
                send_tmux_message("❌ Usage: :send <message>", socket)
                response = {"continue": False, "stopReason": "❌ Usage: :send <message>"}
                print(json.dumps(response))
                return

            try:
                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                from kitty_claude.claude import get_running_sessions
                sessions = get_running_sessions(profile)

                # Exclude our own session
                my_session_id = input_data.get('session_id')
                sessions = [s for s in sessions if s['session_id'] != my_session_id]

                if not sessions:
                    send_tmux_message("No other running sessions", socket)
                    response = {"continue": False, "stopReason": "No other running sessions to send to"}
                    print(json.dumps(response))
                    return

                # Build fzf input with session info
                state_dir = get_state_dir()
                fzf_lines = []
                for s in sessions:
                    sid = s['session_id']
                    cwd = s.get('cwd', '?')
                    # Load session name from metadata
                    meta_file = state_dir / "sessions" / f"{sid}.json"
                    name = sid[:8]
                    if meta_file.exists():
                        try:
                            meta = json.loads(meta_file.read_text())
                            name = meta.get("name", sid[:8])
                        except:
                            pass
                    fzf_lines.append(f"{sid}\t{name}\t{cwd}")

                uid = os.getuid()
                tmp_input = Path(f"/tmp/kc-send-{uid}.txt")
                tmp_output = Path(f"/tmp/kc-send-{uid}-out.txt")
                tmp_input.write_text("\n".join(fzf_lines))
                tmp_output.unlink(missing_ok=True)

                subprocess.run([
                    "tmux", "-L", socket,
                    "display-popup", "-E", "-w", "70%", "-h", "40%",
                    f"cat {tmp_input} | fzf --delimiter='\\t' --with-nth=2,3 --header='Send to:' > {tmp_output}"
                ])

                if tmp_output.exists():
                    selected = tmp_output.read_text().strip()
                    if selected:
                        target_session_id = selected.split("\t")[0]

                        # Find the tmux window with this session ID
                        result = run(
                            ["tmux", "-L", socket, "list-windows", "-F", "#{window_id} #{@session_id}"],
                            capture_output=True, text=True, check=True
                        )
                        target_window = None
                        for line in result.stdout.strip().split("\n"):
                            parts = line.split()
                            if len(parts) >= 2 and parts[1] == target_session_id:
                                target_window = parts[0]
                                break

                        if target_window:
                            escaped = shlex.quote(message)
                            run([
                                "tmux", "-L", socket,
                                "send-keys", "-t", target_window, "-l", message
                            ])
                            run([
                                "tmux", "-L", socket,
                                "send-keys", "-t", target_window, "Enter"
                            ])
                            send_tmux_message(f"✓ Sent to {target_session_id[:8]}", socket)
                            response = {"continue": False, "stopReason": f"✓ Message sent"}
                        else:
                            send_tmux_message(f"❌ Could not find window for session", socket)
                            response = {"continue": False, "stopReason": "❌ Could not find target window"}
                    else:
                        response = {"continue": False, "stopReason": "Cancelled"}
                else:
                    response = {"continue": False, "stopReason": "Cancelled"}

                tmp_input.unlink(missing_ok=True)
                tmp_output.unlink(missing_ok=True)
            except Exception as e:
                send_tmux_message(f"❌ Error: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

        # Check for plugin commands: :foo -> kitty-claude-foo on PATH
        if prompt.startswith(':'):
            parts = prompt[1:].split(None, 1)
            cmd_name = parts[0] if parts else ""
            cmd_args = parts[1] if len(parts) > 1 else ""

            plugin_bin = shutil.which(f"kitty-claude-{cmd_name}")
            if plugin_bin:
                session_id = input_data.get('session_id')
                env = os.environ.copy()
                if session_id:
                    env["KITTY_CLAUDE_SESSION_ID"] = session_id
                env["KITTY_CLAUDE_SOCKET"] = socket
                env["KITTY_CLAUDE_CWD"] = input_data.get('cwd', os.getcwd())

                try:
                    result = subprocess.run(
                        [plugin_bin] + (cmd_args.split() if cmd_args else []),
                        capture_output=True, text=True, timeout=30, env=env
                    )
                    output = result.stdout.strip()

                    if output.startswith(':'):
                        # Re-dispatch as a colon command — recursive call
                        # Replace the prompt and fall through from the top
                        # We do this by writing to stdout so the hook re-runs
                        print(output)
                    elif output:
                        response = {"continue": False, "stopReason": output}
                        print(json.dumps(response))
                    else:
                        response = {"continue": False, "stopReason": f"✓ {cmd_name} completed"}
                        print(json.dumps(response))
                except subprocess.TimeoutExpired:
                    send_tmux_message(f"❌ Plugin '{cmd_name}' timed out", socket)
                    response = {"continue": False, "stopReason": f"❌ Plugin '{cmd_name}' timed out"}
                    print(json.dumps(response))
                except Exception as e:
                    send_tmux_message(f"❌ Plugin error: {e}", socket)
                    response = {"continue": False, "stopReason": f"❌ Plugin error: {str(e)}"}
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


def handle_run_command(command):
    """Handle --run-command - run a colon command directly, sharing the hook handler logic.

    Constructs input_data from environment, feeds it via stdin to handle_user_prompt_submit,
    and captures the output.
    """
    import io

    config_dir = os.environ.get('CLAUDE_CONFIG_DIR', '')
    session_id = Path(config_dir).name if config_dir else None

    input_data = {
        "session_id": session_id,
        "cwd": os.getcwd(),
        "prompt": command,
    }

    # Swap stdin so the handler reads our input_data
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(input_data))

    # Swap stdout so we capture the handler's output
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()

    try:
        handle_user_prompt_submit()
    except SystemExit:
        pass

    output = sys.stdout.getvalue()
    sys.stdin = old_stdin
    sys.stdout = old_stdout

    # Parse and return the result
    for line in output.strip().split('\n'):
        if not line:
            continue
        try:
            result = json.loads(line)
            print(json.dumps(result))
            return
        except (json.JSONDecodeError, ValueError):
            pass

    # If no JSON found, return the raw output
    print(output)


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