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
import time


def get_title_history_file(profile=None):
    """Get the path to the title history file."""
    if profile is None:
        profile = os.environ.get('KITTY_CLAUDE_PROFILE')
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    return config_dir / "title-history.json"


def record_title(title, profile=None):
    """Record a title in the history file."""
    if not title or not title.strip():
        return
    title = title.strip()

    history_file = get_title_history_file(profile)
    history = []

    if history_file.exists():
        try:
            history = json.loads(history_file.read_text())
        except:
            history = []

    # Find existing entry or create new one
    found = False
    for entry in history:
        if entry.get("title") == title:
            entry["last_used"] = time.time()
            entry["count"] = entry.get("count", 0) + 1
            found = True
            break

    if not found:
        history.append({
            "title": title,
            "last_used": time.time(),
            "count": 1
        })

    # Sort by last_used descending
    history.sort(key=lambda x: x.get("last_used", 0), reverse=True)

    history_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.write_text(json.dumps(history, indent=2))


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


def queue_startup_message(session_id: str, message: str, profile: str = None):
    """Queue a message to be shown on next session start."""
    if profile:
        base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "kitty-claude"

    session_dir = base_config / "session-configs" / session_id
    run_file = session_dir / ".run-counter"
    messages_file = session_dir / ".startup-messages"

    # Get current run number
    current_run = 0
    if run_file.exists():
        try:
            current_run = int(run_file.read_text().strip())
        except (ValueError, OSError):
            pass

    # Load existing messages or start fresh
    messages = []
    if messages_file.exists():
        try:
            messages = json.loads(messages_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Add new message with current run number
    messages.append({"run": current_run, "text": message})

    # Write back
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        messages_file.write_text(json.dumps(messages))
    except OSError:
        pass


def get_timed_permissions_file():
    """Get path to timed permissions file."""
    config_dir = Path.home() / ".config" / "kitty-claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "timed-permissions.json"


def load_timed_permissions():
    """Load timed permissions from config file."""
    perm_file = get_timed_permissions_file()
    if perm_file.exists():
        try:
            return json.loads(perm_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_timed_permissions(permissions):
    """Save timed permissions to config file."""
    perm_file = get_timed_permissions_file()
    perm_file.write_text(json.dumps(permissions, indent=2))


def parse_duration(duration_str):
    """Parse duration string like '1h', '30m', '2h30m' into seconds.

    Returns None if invalid.
    """
    import re
    total_seconds = 0
    pattern = re.compile(r'(\d+)([hms])')
    matches = pattern.findall(duration_str.lower())
    if not matches:
        return None
    for value, unit in matches:
        value = int(value)
        if unit == 'h':
            total_seconds += value * 3600
        elif unit == 'm':
            total_seconds += value * 60
        elif unit == 's':
            total_seconds += value
    return total_seconds if total_seconds > 0 else None


def format_remaining_time(seconds):
    """Format remaining seconds as human-readable string."""
    if seconds <= 0:
        return "expired"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h{minutes}m"
    elif minutes > 0:
        return f"{minutes}m{secs}s"
    else:
        return f"{secs}s"


def cleanup_expired_timed_permissions(claude_data_dir=None):
    """Remove expired timed permissions from both kitty-claude and Claude's settings.

    Called on session startup to clean up any permissions that expired
    while the session was not running.
    """
    import time
    now = time.time()

    timed_perms = load_timed_permissions()
    expired_patterns = []
    active_perms = []

    for perm in timed_perms:
        if now > perm.get('expires', 0):
            expired_patterns.append(perm.get('pattern'))
        else:
            active_perms.append(perm)

    if not expired_patterns:
        return  # Nothing to clean up

    # Update timed permissions file
    save_timed_permissions(active_perms)

    # Remove expired patterns from Claude's settings.json
    if claude_data_dir:
        settings_file = Path(claude_data_dir) / "settings.json"
        if settings_file.exists():
            try:
                settings = json.loads(settings_file.read_text())
                allow_list = settings.get("permissions", {}).get("allow", [])
                original_len = len(allow_list)
                allow_list = [p for p in allow_list if p not in expired_patterns]
                if len(allow_list) != original_len:
                    settings["permissions"]["allow"] = allow_list
                    settings_file.write_text(json.dumps(settings, indent=2))
            except (json.JSONDecodeError, OSError):
                pass


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
:skills              List available slash commands (skills)
:rules               List all rules
:note                Open session notes in vim
:skill [name]        Create/edit a global Claude skill (fzf if no name)
:rule [name]         Create/edit a global rule (fzf if no name)
:todo [desc]         List todos or add one for current directory
:done <num>          Mark a todo as done by number
:plan / :god         Enable planning MCP server (session overview) and reload
:skills-mcp          Enable skills MCP server (lets Claude create kc-skills)
::skills             List all kitty-claude skills
::skill <name>       Create/edit a kitty-claude skill
::<skill> [prompt]   Run kitty-claude skill (injects context)
:mcp <cmd> [args]    Add a native MCP server to this session
:mcp-shell <cmd>     Expose shell command as MCP server
:mcp-approve <cmd>   Add MCP server wrapped with tmux approval proxy
:mcps                List MCP servers in this session
:mcp-remove <name>   Remove an MCP server from session
:roles               List available roles
:role [name]         Activate a role (fzf picker if no name given)
:role-add <role> <n> Add permission #n (from :permissions) to a role
:role-add-all <role> Add all current permissions to a role
:role-add-mcp <role> <server>  Add MCP server from session to a role
:roles-current       Show active roles in this session
:title-role [t] [r]  Map tmux window title to a role (no args: show)
:login               Refresh credentials from freshest session
:login-all           Send :login to all kc1-* instances
:reload-all          Send :reload to all kc1-* instances
:send <message>      Send a message to another kitty-claude window (fzf)
:current-sessions    List all currently running sessions
:sessions [N]        List recent sessions (default 10)
:resume <num|id>     Resume a session in new window
:resume-new [num|id] Resume a session in a new kitty-claude window
:spawn [title]       Spawn new window (no arg: pick from history)
:clear               Clear session and start fresh
:reload              Reload Claude (same session, pick up config changes)
:cd <path>           Change directory and move session
:cdpop               Return to previous directory
:cd-tmux             Change to directory of tmux session 0
:tmux                Link/switch to a tmux window on default server
:tmux-unlink         Unlink the associated tmux window
:tmuxpath            Show path of linked tmux window
:tmuxscreen          Capture and show content of linked tmux window
:tmuxs-link          Add current tmux window to linked windows list
:tmuxs               Pick a linked tmux window (fzf)
:call                Open popup with context, returns result
:ask                 Open popup without context, returns result
:fork                Clone conversation to new window (independent)
:permissions          Show allowed commands in this session
:permissions-gui     Open permissions editor GUI
:disallow <num> ...  Remove allowed command(s) by number
:allow-for <dur> <p|n> Allow tool for duration (pattern or # from :permissions)
:allow-last          Allow the last tool that was used
:allow-recent        Select from recent tools to allow (fzf)
:time                Show duration of last response
:checkpoint          Save a checkpoint in the current session
:rollback            Rollback to the last checkpoint (clones session)

Examples:
  :skills, :rules, ::skills
  :allow-for 1h 5  (make permission #5 expire in 1 hour)
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
  :resume-new 1
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

        # Check for :permissions command
        if prompt == ':permissions':
            allow_rules = []  # list of (rule, source_label, source_file)

            # Session-level permissions (MCP auto-approvals)
            settings_file = claude_data_dir / "settings.json"
            if settings_file.exists():
                try:
                    settings = json.loads(settings_file.read_text())
                    for rule in settings.get("permissions", {}).get("allow", []):
                        allow_rules.append((rule, "session", str(settings_file)))
                except (json.JSONDecodeError, OSError):
                    pass

            # Project-level permissions (user-approved tools)
            cwd = input_data.get('cwd', os.getcwd())
            project_settings = Path(cwd) / ".claude" / "settings.local.json"
            if project_settings.exists():
                try:
                    proj = json.loads(project_settings.read_text())
                    for rule in proj.get("permissions", {}).get("allow", []):
                        allow_rules.append((rule, "project", str(project_settings)))
                except (json.JSONDecodeError, OSError):
                    pass

            # Build a lookup: which active roles contain each rule
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            if profile:
                roles_base = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile / "mcp-roles"
            else:
                roles_base = Path.home() / ".config" / "kitty-claude" / "mcp-roles"

            rule_in_roles = {}  # rule -> list of role names
            session_id = input_data.get('session_id')
            if session_id:
                state_dir = get_state_dir()
                metadata_file = state_dir / "sessions" / f"{session_id}.json"
                metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
                active_roles = metadata.get("activeRoles", [])

                for role_name in active_roles:
                    role_file = roles_base / f"{role_name}.json"
                    if role_file.exists():
                        try:
                            role = json.loads(role_file.read_text())
                            for rule in role.get("permissions", {}).get("allow", []):
                                rule_in_roles.setdefault(rule, []).append(role_name)
                        except (json.JSONDecodeError, OSError):
                            pass

            # Deduplicate while preserving order
            seen = set()
            unique_rules = []
            for rule, label, source_file in allow_rules:
                if rule not in seen:
                    seen.add(rule)
                    unique_rules.append((rule, label, source_file))

            # Load timed permissions to show remaining time
            import time
            now = time.time()
            timed_perms = load_timed_permissions()
            timed_lookup = {}  # pattern -> remaining time string
            for perm in timed_perms:
                pattern = perm.get('pattern', '')
                expires = perm.get('expires', 0)
                remaining = expires - now
                if remaining > 0:
                    timed_lookup[pattern] = format_remaining_time(remaining)
                else:
                    timed_lookup[pattern] = "expired"

            if unique_rules:
                lines = "Allowed commands in this session:\n\n"
                current_label = None
                for i, (rule, label, _source_file) in enumerate(unique_rules, 1):
                    if label != current_label:
                        current_label = label
                        lines += f"  [{label}]\n"
                    tags = []
                    if rule in rule_in_roles:
                        tags.append(", ".join(rule_in_roles[rule]))
                    if rule in timed_lookup:
                        tags.append(f"⏱ {timed_lookup[rule]}")
                    tags_str = f"  [{', '.join(tags)}]" if tags else ""
                    lines += f"  {i:3d}. {rule}{tags_str}\n"
                lines += "\nUse :disallow <num> [num2 ...] to remove permission(s)."
            else:
                lines = "No allowed commands configured."

            response = {"continue": False, "stopReason": lines}
            print(json.dumps(response))
            return

        # Check for :permissions-gui command
        if prompt == ':permissions-gui':
            session_id = input_data.get('session_id')
            if not session_id:
                response = {"continue": False, "stopReason": "No session ID."}
                print(json.dumps(response))
                return

            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
            subprocess.Popen([kitty_claude_path, "--permissions-gui", session_id])
            send_tmux_message("Opening permissions editor...", socket)
            response = {"continue": False, "stopReason": ""}
            print(json.dumps(response))
            return

        # Check for :disallow command
        if prompt.startswith(':disallow'):
            arg = prompt[len(':disallow'):].strip()
            if not arg:
                response = {"continue": False, "stopReason": "Usage: :disallow <num> [num2 num3 ...]\nRun :permissions to see numbered list."}
                print(json.dumps(response))
                return

            # Parse multiple numbers
            parts = arg.split()
            target_nums = []
            for p in parts:
                if not p.isdigit():
                    response = {"continue": False, "stopReason": f"Invalid number: {p}\nUsage: :disallow <num> [num2 num3 ...]"}
                    print(json.dumps(response))
                    return
                target_nums.append(int(p))

            # Rebuild the same numbered list as :permissions
            allow_rules = []  # list of (rule, source_file)

            settings_file = claude_data_dir / "settings.json"
            if settings_file.exists():
                try:
                    settings = json.loads(settings_file.read_text())
                    for rule in settings.get("permissions", {}).get("allow", []):
                        allow_rules.append((rule, str(settings_file)))
                except (json.JSONDecodeError, OSError):
                    pass

            cwd = input_data.get('cwd', os.getcwd())
            project_settings = Path(cwd) / ".claude" / "settings.local.json"
            if project_settings.exists():
                try:
                    proj = json.loads(project_settings.read_text())
                    for rule in proj.get("permissions", {}).get("allow", []):
                        allow_rules.append((rule, str(project_settings)))
                except (json.JSONDecodeError, OSError):
                    pass

            # Active role permissions
            session_id = input_data.get('session_id')
            if session_id:
                state_dir = get_state_dir()
                metadata_file = state_dir / "sessions" / f"{session_id}.json"
                metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
                active_roles = metadata.get("activeRoles", [])

                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    roles_base = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile / "mcp-roles"
                else:
                    roles_base = Path.home() / ".config" / "kitty-claude" / "mcp-roles"

                for role_name in active_roles:
                    role_file = roles_base / f"{role_name}.json"
                    if role_file.exists():
                        try:
                            role = json.loads(role_file.read_text())
                            for rule in role.get("permissions", {}).get("allow", []):
                                allow_rules.append((rule, str(role_file)))
                        except (json.JSONDecodeError, OSError):
                            pass

            # Deduplicate
            seen = set()
            unique_rules = []
            for rule, source in allow_rules:
                if rule not in seen:
                    seen.add(rule)
                    unique_rules.append((rule, source))

            # Validate all numbers first
            for num in target_nums:
                if num < 1 or num > len(unique_rules):
                    response = {"continue": False, "stopReason": f"Invalid number {num}. Run :permissions to see valid range (1-{len(unique_rules)})."}
                    print(json.dumps(response))
                    return

            # Sort descending so we remove from highest index first (avoids index shifting)
            target_nums_sorted = sorted(set(target_nums), reverse=True)
            removed = []
            errors = []

            for target_num in target_nums_sorted:
                rule_to_remove, source_file = unique_rules[target_num - 1]

                # Remove from the source file
                source_path = Path(source_file)
                try:
                    data = json.loads(source_path.read_text())
                    allow_list = data.get("permissions", {}).get("allow", [])
                    if rule_to_remove in allow_list:
                        allow_list.remove(rule_to_remove)
                        source_path.write_text(json.dumps(data, indent=2))
                        removed.append(rule_to_remove)
                    else:
                        errors.append(f"Rule not found in {source_file}")
                except Exception as e:
                    errors.append(f"Error removing {rule_to_remove[:30]}: {e}")

            # Build response
            msgs = []
            if removed:
                if len(removed) == 1:
                    msgs.append(f"Removed: {removed[0]}")
                else:
                    msgs.append(f"Removed {len(removed)} permissions:")
                    for r in removed:
                        msgs.append(f"  - {r[:60]}")
                send_tmux_message(f"Removed {len(removed)} permission(s)", socket)
            if errors:
                msgs.append("Errors:")
                msgs.extend(f"  - {e}" for e in errors)

            response = {"continue": False, "stopReason": "\n".join(msgs) if msgs else "Nothing removed."}
            print(json.dumps(response))
            return

        # Check for :allow-for command
        if prompt.startswith(':allow-for'):
            import time
            arg = prompt[len(':allow-for'):].strip()
            parts = arg.split(None, 1)  # Split into duration and pattern/number
            if len(parts) < 2:
                response = {"continue": False, "stopReason": "Usage: :allow-for <duration> <pattern|num>\nExamples:\n  :allow-for 1h Bash(npm:*)\n  :allow-for 1h 5  (use number from :permissions)"}
                print(json.dumps(response))
                return

            duration_str, pattern_or_num = parts
            duration_secs = parse_duration(duration_str)
            if duration_secs is None:
                response = {"continue": False, "stopReason": f"Invalid duration: {duration_str}\nUse format like: 1h, 30m, 2h30m, 90s"}
                print(json.dumps(response))
                return

            # Check if pattern_or_num is a number (retroactive timed permission)
            if pattern_or_num.isdigit():
                target_num = int(pattern_or_num)
                # Rebuild the permissions list (same as :permissions)
                allow_rules = []
                settings_file = claude_data_dir / "settings.json"
                if settings_file.exists():
                    try:
                        settings = json.loads(settings_file.read_text())
                        for rule in settings.get("permissions", {}).get("allow", []):
                            allow_rules.append((rule, str(settings_file)))
                    except (json.JSONDecodeError, OSError):
                        pass

                cwd = input_data.get('cwd', os.getcwd())
                project_settings = Path(cwd) / ".claude" / "settings.local.json"
                if project_settings.exists():
                    try:
                        proj = json.loads(project_settings.read_text())
                        for rule in proj.get("permissions", {}).get("allow", []):
                            allow_rules.append((rule, str(project_settings)))
                    except (json.JSONDecodeError, OSError):
                        pass

                # Deduplicate
                seen = set()
                unique_rules = []
                for rule, source in allow_rules:
                    if rule not in seen:
                        seen.add(rule)
                        unique_rules.append((rule, source))

                if target_num < 1 or target_num > len(unique_rules):
                    response = {"continue": False, "stopReason": f"Invalid number {target_num}. Run :permissions to see valid range (1-{len(unique_rules)})."}
                    print(json.dumps(response))
                    return

                pattern = unique_rules[target_num - 1][0]
            else:
                pattern = pattern_or_num

            expires_at = time.time() + duration_secs

            # Add to timed permissions file
            timed_perms = load_timed_permissions()
            # Remove any existing entry for the same pattern
            timed_perms = [p for p in timed_perms if p.get('pattern') != pattern]
            timed_perms.append({
                'pattern': pattern,
                'expires': expires_at,
                'created': time.time()
            })
            save_timed_permissions(timed_perms)

            # Also add to Claude's session permissions.allow (if not already there)
            settings_file = claude_data_dir / "settings.json"
            try:
                if settings_file.exists():
                    settings = json.loads(settings_file.read_text())
                else:
                    settings = {}
                if 'permissions' not in settings:
                    settings['permissions'] = {}
                if 'allow' not in settings['permissions']:
                    settings['permissions']['allow'] = []
                if pattern not in settings['permissions']['allow']:
                    settings['permissions']['allow'].append(pattern)
                settings_file.write_text(json.dumps(settings, indent=2))
            except Exception as e:
                response = {"continue": False, "stopReason": f"Error updating settings: {e}"}
                print(json.dumps(response))
                return

            readable_duration = format_remaining_time(duration_secs)
            send_tmux_message(f"Timed {pattern[:30]}... for {readable_duration}", socket)
            response = {"continue": False, "stopReason": f"Allowed for {readable_duration}: {pattern}\n\nThis permission will be denied after {readable_duration}."}
            print(json.dumps(response))
            return

        # Check for :allow-last command
        if prompt == ':allow-last':
            # Find the last tool from session logs
            session_id = input_data.get('session_id')
            cwd = input_data.get('cwd', os.getcwd())

            if not session_id:
                response = {"continue": False, "stopReason": "No session ID available"}
                print(json.dumps(response))
                return

            # Find the session JSONL file
            # Path format: claude-data/projects/<path-hash>/<session-id>.jsonl
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            if profile:
                base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                base_config = Path.home() / ".config" / "kitty-claude"

            projects_dir = base_config / "claude-data" / "projects"
            # Path hash format: replace / with -
            path_hash = cwd.replace('/', '-')
            session_file = projects_dir / path_hash / f"{session_id}.jsonl"

            if not session_file.exists():
                response = {"continue": False, "stopReason": f"Session log not found: {session_file}"}
                print(json.dumps(response))
                return

            # Read the file and find the last tool_use
            last_tool = None
            try:
                with open(session_file, 'r') as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            content = entry.get('message', {}).get('content', [])
                            if isinstance(content, list):
                                for item in content:
                                    if item.get('type') == 'tool_use':
                                        last_tool = item
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                response = {"continue": False, "stopReason": f"Error reading session log: {e}"}
                print(json.dumps(response))
                return

            if not last_tool:
                response = {"continue": False, "stopReason": "No tool use found in session"}
                print(json.dumps(response))
                return

            # Build the permission pattern
            tool_name = last_tool.get('name', '')
            tool_input = last_tool.get('input', {})

            if tool_name == 'Bash':
                command = tool_input.get('command', '')
                # Extract the base command for the pattern
                base_cmd = command.split()[0] if command else ''
                pattern = f"Bash({base_cmd}:*)"
            elif tool_name.startswith('mcp__'):
                pattern = tool_name
            else:
                pattern = tool_name

            # Add to Claude's session permissions.allow
            settings_file = claude_data_dir / "settings.json"
            try:
                if settings_file.exists():
                    settings = json.loads(settings_file.read_text())
                else:
                    settings = {}
                if 'permissions' not in settings:
                    settings['permissions'] = {}
                if 'allow' not in settings['permissions']:
                    settings['permissions']['allow'] = []
                if pattern not in settings['permissions']['allow']:
                    settings['permissions']['allow'].append(pattern)
                    settings_file.write_text(json.dumps(settings, indent=2))
                    send_tmux_message(f"✓ Allowed: {pattern[:50]}", socket)
                    response = {"continue": False, "stopReason": f"✓ Allowed: {pattern}"}
                else:
                    send_tmux_message(f"Already allowed: {pattern[:50]}", socket)
                    response = {"continue": False, "stopReason": f"Already allowed: {pattern}"}
            except Exception as e:
                response = {"continue": False, "stopReason": f"Error updating settings: {e}"}

            print(json.dumps(response))
            return

        # Check for :allow-recent command
        if prompt == ':allow-recent':
            # Find recent tools from session logs, let user select with fzf
            session_id = input_data.get('session_id')
            cwd = input_data.get('cwd', os.getcwd())

            if not session_id:
                response = {"continue": False, "stopReason": "No session ID available"}
                print(json.dumps(response))
                return

            # Find the session JSONL file
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            if profile:
                base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                base_config = Path.home() / ".config" / "kitty-claude"

            projects_dir = base_config / "claude-data" / "projects"
            path_hash = cwd.replace('/', '-')
            session_file = projects_dir / path_hash / f"{session_id}.jsonl"

            if not session_file.exists():
                response = {"continue": False, "stopReason": f"Session log not found: {session_file}"}
                print(json.dumps(response))
                return

            # Read the file and collect recent tool_use entries (keep last N unique patterns)
            tools_seen = []  # list of (pattern, display_text) - most recent last
            try:
                with open(session_file, 'r') as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            content = entry.get('message', {}).get('content', [])
                            if isinstance(content, list):
                                for item in content:
                                    if item.get('type') == 'tool_use':
                                        tool_name = item.get('name', '')
                                        tool_input = item.get('input', {})

                                        # Build the permission pattern (same logic as allow-last)
                                        if tool_name == 'Bash':
                                            command = tool_input.get('command', '')
                                            base_cmd = command.split()[0] if command else ''
                                            pattern = f"Bash({base_cmd}:*)"
                                            display = f"{pattern}  # {command[:60]}"
                                        elif tool_name.startswith('mcp__'):
                                            pattern = tool_name
                                            display = pattern
                                        else:
                                            pattern = tool_name
                                            display = pattern

                                        # Remove if already seen, then add to end
                                        tools_seen = [(p, d) for p, d in tools_seen if p != pattern]
                                        tools_seen.append((pattern, display))
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                response = {"continue": False, "stopReason": f"Error reading session log: {e}"}
                print(json.dumps(response))
                return

            if not tools_seen:
                response = {"continue": False, "stopReason": "No tool use found in session"}
                print(json.dumps(response))
                return

            # Take last 20, reverse so most recent is first
            recent_tools = list(reversed(tools_seen[-20:]))

            # Use fzf in tmux popup to select
            import subprocess
            import tempfile

            # Build fzf input: index\tpattern\tdisplay
            fzf_lines = []
            for i, (p, d) in enumerate(recent_tools):
                fzf_lines.append(f"{i}\t{p}\t{d}")

            tmp_input = Path(tempfile.mktemp())
            tmp_output = Path(tempfile.mktemp())
            tmp_input.write_text("\n".join(fzf_lines))
            tmp_output.unlink(missing_ok=True)

            try:
                subprocess.run([
                    "tmux", "-L", socket,
                    "display-popup", "-E", "-w", "80%", "-h", "50%",
                    f"cat {tmp_input} | fzf --delimiter='\\t' --with-nth=3 --header='Select tool to allow' > {tmp_output}"
                ])

                if not tmp_output.exists() or not tmp_output.read_text().strip():
                    response = {"continue": False, "stopReason": "Selection cancelled"}
                    print(json.dumps(response))
                    tmp_input.unlink(missing_ok=True)
                    tmp_output.unlink(missing_ok=True)
                    return

                selected_line = tmp_output.read_text().strip()
                tmp_input.unlink(missing_ok=True)
                tmp_output.unlink(missing_ok=True)

                # Parse: index\tpattern\tdisplay
                parts = selected_line.split('\t')
                if len(parts) >= 2:
                    pattern = parts[1]
                else:
                    response = {"continue": False, "stopReason": "Could not parse selection"}
                    print(json.dumps(response))
                    return

            except Exception as e:
                tmp_input.unlink(missing_ok=True)
                tmp_output.unlink(missing_ok=True)
                response = {"continue": False, "stopReason": f"Error running fzf: {e}"}
                print(json.dumps(response))
                return

            # Add to Claude's session permissions.allow
            settings_file = claude_data_dir / "settings.json"
            try:
                if settings_file.exists():
                    settings = json.loads(settings_file.read_text())
                else:
                    settings = {}
                if 'permissions' not in settings:
                    settings['permissions'] = {}
                if 'allow' not in settings['permissions']:
                    settings['permissions']['allow'] = []
                if pattern not in settings['permissions']['allow']:
                    settings['permissions']['allow'].append(pattern)
                    settings_file.write_text(json.dumps(settings, indent=2))
                    send_tmux_message(f"✓ Allowed: {pattern[:50]}", socket)
                    response = {"continue": False, "stopReason": f"✓ Allowed: {pattern}"}
                else:
                    send_tmux_message(f"Already allowed: {pattern[:50]}", socket)
                    response = {"continue": False, "stopReason": f"Already allowed: {pattern}"}
            except Exception as e:
                response = {"continue": False, "stopReason": f"Error updating settings: {e}"}

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
        if prompt == ':sessions' or prompt.startswith(':sessions '):
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            from kitty_claude.claude import get_recent_sessions
            from datetime import datetime

            # Parse optional limit argument
            limit = 10
            if prompt.startswith(':sessions '):
                arg = prompt[10:].strip()
                if arg.isdigit():
                    limit = int(arg)

            sessions = get_recent_sessions(profile, limit=limit)

            if not sessions:
                msg = "No recent sessions found"
            else:
                lines = ["Recent sessions (ordered by last activity):\n"]
                for i, sess in enumerate(sessions, 1):
                    session_id = sess['session_id']
                    title = sess.get('title')
                    cwd = sess.get('cwd') or '?'
                    mtime = datetime.fromtimestamp(sess['last_modified']).strftime('%Y-%m-%d %H:%M')
                    last_msg = sess.get('last_message') or ''

                    # Build the main line
                    if title:
                        main_line = f"{i}. [{title}] {cwd} ({mtime})"
                    else:
                        main_line = f"{i}. {session_id[:8]}... - {cwd} ({mtime})"

                    lines.append(main_line)
                    if last_msg:
                        # Truncate and clean up the message
                        last_msg = last_msg.replace('\n', ' ').strip()
                        if len(last_msg) > 40:
                            last_msg = last_msg[:40] + "..."
                        lines.append(f"   └─ {last_msg}")
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

        # Check for :resume-new command (spawn new kitty-claude window to resume)
        if prompt.startswith(':resume-new') and (prompt == ':resume-new' or prompt[11:12] == ' '):
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            arg = prompt[12:].strip() if len(prompt) > 11 else ''

            # If no arg, show sessions and ask user to pick
            if not arg:
                from kitty_claude.claude import get_recent_sessions
                sessions = get_recent_sessions(profile, limit=10)
                if not sessions:
                    send_tmux_message("❌ No sessions found", socket)
                    response = {"continue": False, "stopReason": "No recent sessions found"}
                    print(json.dumps(response))
                    return

                from datetime import datetime
                lines = ["Recent sessions:\n"]
                for i, sess in enumerate(sessions, 1):
                    sid = sess['session_id']
                    cwd = sess.get('cwd', '?')
                    mtime = datetime.fromtimestamp(sess['last_modified']).strftime('%Y-%m-%d %H:%M')
                    lines.append(f"{i}. {sid[:8]}... - {cwd} (last: {mtime})")
                lines.append(f"\nUse :resume-new <number> or :resume-new <session-id>")
                msg = "\n".join(lines)
                send_tmux_message(f"✓ {len(sessions)} sessions", socket)
                response = {"continue": False, "stopReason": msg}
                print(json.dumps(response))
                return

            # Resolve session ID from number or direct ID
            target_session_id = None
            target_cwd = None
            if arg.isdigit():
                from kitty_claude.claude import get_recent_sessions
                sessions = get_recent_sessions(profile, limit=10)
                index = int(arg) - 1
                if 0 <= index < len(sessions):
                    target_session_id = sessions[index]['session_id']
                    target_cwd = sessions[index].get('cwd')
                else:
                    send_tmux_message(f"❌ Invalid session number", socket)
                    response = {"continue": False, "stopReason": f"❌ Session number {arg} not found"}
                    print(json.dumps(response))
                    return
            else:
                target_session_id = arg
                # Look up cwd from project directory (more reliable than .claude.json)
                if profile:
                    base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
                else:
                    base_config = Path.home() / ".config" / "kitty-claude"
                # Search projects directories for this session's JSONL file
                projects_dir = base_config / "claude-data" / "projects"
                if projects_dir.exists():
                    for proj_dir in projects_dir.iterdir():
                        if proj_dir.is_dir():
                            session_file = proj_dir / f"{target_session_id}.jsonl"
                            if session_file.exists():
                                # Reverse the path hash: -home-user-project -> /home/user/project
                                # But paths may contain hyphens (e.g., note-frame), so we
                                # progressively try converting hyphens to slashes from left
                                # and check if the resulting path exists
                                path_hash = proj_dir.name
                                if path_hash.startswith('-'):
                                    path_hash = path_hash[1:]  # Remove leading hyphen
                                parts = path_hash.split('-')
                                # Try progressively fewer slashes (more hyphens kept)
                                for num_slashes in range(len(parts), 0, -1):
                                    # Join first num_slashes parts with /, rest with -
                                    candidate = '/' + '/'.join(parts[:num_slashes])
                                    if num_slashes < len(parts):
                                        candidate += '-' + '-'.join(parts[num_slashes:])
                                    if Path(candidate).exists():
                                        target_cwd = candidate
                                        break
                                # Fallback: simple conversion
                                if not target_cwd:
                                    target_cwd = '/' + '/'.join(parts)
                                break

            # Detect one-tab mode from socket name
            is_one_tab = socket.startswith('kc1-')

            # Build kitty-claude command
            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
            cmd = [kitty_claude_path]
            if profile:
                cmd.extend(["--profile", profile])
            if is_one_tab:
                cmd.append("--one-tab")
            else:
                cmd.append("--one-tab")  # Always spawn as one-tab for resume-new
            cmd.extend(["--resume-session", target_session_id])
            # Pass working directory for resumed session
            if target_cwd and Path(target_cwd).exists():
                cmd.extend(["--cwd", target_cwd])

            # Spawn the new window
            import subprocess as sp
            try:
                proc = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE)
                # Give it a moment to fail if it's going to
                import time
                time.sleep(0.2)
                if proc.poll() is not None:
                    # Process already exited - probably an error
                    _, stderr = proc.communicate()
                    if stderr:
                        send_tmux_message(f"❌ {stderr.decode()[:50]}", socket)
                        response = {"continue": False, "stopReason": f"❌ Error: {stderr.decode()}"}
                        print(json.dumps(response))
                        return
            except Exception as e:
                send_tmux_message(f"❌ {str(e)[:50]}", socket)
                response = {"continue": False, "stopReason": f"❌ Failed to spawn: {e}"}
                print(json.dumps(response))
                return

            cwd_msg = f" in {target_cwd}" if target_cwd else ""
            send_tmux_message(f"✓ Spawning new window{cwd_msg[:30]}", socket)
            response = {"continue": False, "stopReason": f"✓ Resuming {target_session_id[:8]}...{cwd_msg} in new kitty-claude window"}
            print(json.dumps(response))
            return

        # Check for :spawn command
        if prompt.startswith(':spawn'):
            arg = prompt[len(':spawn'):].strip()

            # If no arg, show fzf picker of title history
            if not arg:
                history_file = get_title_history_file()
                history = []
                if history_file.exists():
                    try:
                        history = json.loads(history_file.read_text())
                    except:
                        pass

                if not history:
                    response = {"continue": False, "stopReason": "No title history yet. Use :spawn <title> to create one."}
                    print(json.dumps(response))
                    return

                # Build fzf input: title (count uses)
                fzf_lines = []
                for entry in history:
                    title = entry.get("title", "")
                    count = entry.get("count", 1)
                    fzf_lines.append(f"{title}\t({count} uses)")

                fzf_input = "\n".join(fzf_lines)

                # Run fzf in tmux popup
                fzf_cmd = f"echo '{fzf_input}' | fzf --prompt='Spawn with title: ' --with-nth=1"
                result = subprocess.run(
                    ["tmux", "-L", socket, "display-popup", "-E", "-w", "60%", "-h", "50%", fzf_cmd],
                    capture_output=True, text=True
                )

                if result.returncode == 0 and result.stdout.strip():
                    # Extract just the title (before the tab)
                    arg = result.stdout.strip().split("\t")[0]
                else:
                    response = {"continue": False, "stopReason": "Cancelled"}
                    print(json.dumps(response))
                    return

            window_title = arg

            # Record title in history
            record_title(window_title)

            # Build kitty-claude command - always use --one-tab for independent windows
            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
            cmd = [kitty_claude_path]
            if profile:
                cmd.extend(["--profile", profile])
            cmd.append("--one-tab")
            cmd.extend(["--window-name", window_title])

            # Spawn the new window
            import subprocess as sp
            sp.Popen(cmd)

            send_tmux_message(f"✓ Spawning: {window_title}", socket)
            response = {"continue": False, "stopReason": f"✓ Spawning new kitty-claude window: {window_title}"}
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

        # Check for :skills command
        if prompt == ':skills':
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

            # Carry over session-level state from old session
            state_dir = get_state_dir()
            old_meta_file = state_dir / "sessions" / f"{session_id}.json"
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

            # Check if we're in one-tab mode
            if socket.startswith("kc1-"):
                # One-tab mode: use launcher script
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
                if not claude_bin:
                    claude_bin = shutil.which("claude") or "claude"

                uid = os.getuid()
                launcher = Path(f"/tmp/kc-rollback-{uid}-{new_session_id[:8]}.sh")
                launcher.write_text(f'''#!/bin/sh
export CLAUDE_CONFIG_DIR="{claude_config}"
cd "{current_dir}"
exec "{claude_bin}" --resume {new_session_id}
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

        # :tmuxscreen - capture content of linked tmux window
        if prompt == ':tmuxscreen':
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
                    ["tmux", "-L", "default", "capture-pane", "-p", "-t", linked_window],
                    capture_output=True, text=True, check=True
                )
                content = result.stdout.rstrip()
                if content:
                    # Strip leading blank lines
                    lines = content.split('\n')
                    while lines and not lines[0].strip():
                        lines.pop(0)
                    content = '\n'.join(lines)
                    send_tmux_message(f"✓ Captured {len(lines)} lines from window {linked_window}", socket)
                    response = {"continue": False, "stopReason": f"Content of linked tmux window ({linked_window}):\n\n```\n{content}\n```"}
                else:
                    send_tmux_message("Linked window is empty", socket)
                    response = {"continue": False, "stopReason": f"Linked tmux window ({linked_window}) is empty."}
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

        # Check for :login command - refresh credentials from freshest session
        if prompt == ':login':
            session_id = input_data.get('session_id')
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')

            if not session_id:
                send_tmux_message("❌ No session ID available", socket)
                response = {"continue": False, "stopReason": "❌ No session ID available"}
                print(json.dumps(response))
                return

            from kitty_claude.claude import propagate_credentials

            if profile:
                base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                base_config = Path.home() / ".config" / "kitty-claude"

            session_configs_dir = base_config / "session-configs"
            current_session_creds = session_configs_dir / session_id / ".credentials.json"

            # Find the freshest credentials across all sessions
            best_expiry = 0
            best_creds_content = None
            best_source = None

            for session_dir in session_configs_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                creds_file = session_dir / ".credentials.json"
                if not creds_file.exists():
                    continue
                try:
                    content = creds_file.read_text()
                    data = json.loads(content)
                    expiry = data.get("claudeAiOauth", {}).get("expiresAt", 0)
                    if expiry > best_expiry:
                        best_expiry = expiry
                        best_creds_content = content
                        best_source = session_dir.name[:8]
                except Exception:
                    continue

            if not best_creds_content:
                send_tmux_message("❌ No valid credentials found in any session", socket)
                response = {"continue": False, "stopReason": "❌ No valid credentials found"}
                print(json.dumps(response))
                return

            import time
            now_ms = int(time.time() * 1000)
            if best_expiry < now_ms:
                send_tmux_message("❌ All credentials expired - need manual login", socket)
                response = {"continue": False, "stopReason": "❌ All credentials expired"}
                print(json.dumps(response))
                return

            # Copy freshest credentials to current session and shared location
            try:
                current_session_creds.parent.mkdir(parents=True, exist_ok=True)
                current_session_creds.write_text(best_creds_content)
                shared_creds = base_config / "claude-data" / ".credentials.json"
                if shared_creds.exists() or shared_creds.is_symlink():
                    shared_creds.unlink()
                shared_creds.write_text(best_creds_content)
                remaining = (best_expiry - now_ms) // 60000
                send_tmux_message(f"✓ Credentials from {best_source} - reloading...", socket)
                # Queue message for next session start
                queue_startup_message(
                    session_id,
                    f"✓ Logged in with credentials from session {best_source} ({remaining} min remaining)",
                    profile
                )
            except Exception as e:
                send_tmux_message(f"❌ Failed to copy credentials: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ {e}"}
                print(json.dumps(response))
                return

            # Auto-reload after successful credentials refresh
            current_dir = input_data.get('cwd', os.getcwd())
            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
            build_claude_md(profile)
            from kitty_claude.claude import save_auth_from_session, setup_session_config
            save_auth_from_session(session_id, profile)
            session_config_dir = setup_session_config(session_id, profile)

            if socket.startswith("kc1-"):
                claude_config = str(session_config_dir)
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
                subprocess.Popen([
                    "sh", "-c",
                    f"sleep 1 && tmux -L {socket} respawn-pane -k {launcher}"
                ])
            else:
                subprocess.Popen([
                    kitty_claude_path, "--resume-session", session_id
                ])
                subprocess.Popen([
                    "sh", "-c",
                    f"sleep 1.5 && tmux -L {socket} kill-pane"
                ])

            response = {"continue": False, "stopReason": ""}
            print(json.dumps(response))
            return

        # Check for :login-all command - send :login to all kc1-* instances
        if prompt == ':login-all':
            try:
                import time

                log("=== :login-all started ===")

                # Find all kc1-* tmux sockets
                uid = os.getuid()
                tmux_dir = Path(f"/tmp/tmux-{uid}")
                log(f"tmux_dir: {tmux_dir}, exists: {tmux_dir.exists()}")
                if not tmux_dir.exists():
                    send_tmux_message("No tmux socket dir found", socket)
                    response = {"continue": False, "stopReason": "No tmux socket dir"}
                    print(json.dumps(response))
                    return

                kc1_sockets = [f.name for f in tmux_dir.iterdir() if f.name.startswith("kc1-")]
                log(f"Found {len(kc1_sockets)} kc1 sockets")
                if not kc1_sockets:
                    send_tmux_message("No kc1-* instances found", socket)
                    response = {"continue": False, "stopReason": "No kc1-* instances"}
                    print(json.dumps(response))
                    return

                count = 0
                log(f"Current socket: {socket}")
                for kc_socket in kc1_sockets:
                    # Skip current session
                    if kc_socket == socket:
                        log(f"{kc_socket}: SKIPPING (current session)")
                        continue

                    # Check if tmux server is alive and get all windows
                    result = subprocess.run(
                        ["tmux", "-L", kc_socket, "list-windows", "-F", "#{window_index}"],
                        capture_output=True, text=True
                    )
                    if result.returncode != 0:
                        log(f"{kc_socket}: DEAD (rc={result.returncode})")
                        continue  # Dead socket

                    windows = result.stdout.strip().split('\n')
                    log(f"{kc_socket}: ALIVE, {len(windows)} window(s)")

                    # Send :login, Enter to all windows
                    for win_idx in windows:
                        target = win_idx

                        # Send :login
                        cmd1 = ["tmux", "-L", kc_socket, "send-keys", "-t", target, "-l", ":login"]
                        print(f"DEBUG: {' '.join(cmd1)}", file=sys.stderr)
                        subprocess.run(cmd1)
                        log(f"  {kc_socket} win{win_idx}: :login")
                        time.sleep(3.0)

                        # Send Enter
                        cmd2 = ["tmux", "-L", kc_socket, "send-keys", "-t", target, "Enter"]
                        print(f"DEBUG: {' '.join(cmd2)}", file=sys.stderr)
                        subprocess.run(cmd2)
                        log(f"  {kc_socket} win{win_idx}: Enter")

                        count += 1
                        time.sleep(0.2)

                log(f"=== Done, sent to {count} instances ===")
                send_tmux_message(f"✓ Sent :login to {count} instances", socket)
                response = {"continue": False, "stopReason": f"✓ Sent :login to {count} instances"}
            except Exception as e:
                log(f"EXCEPTION: {e}")
                send_tmux_message(f"❌ Error: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}

            print(json.dumps(response))
            return

        # Check for :reload-all command - send :reload to all kc1-* instances
        if prompt == ':reload-all':
            try:
                import time

                log("=== :reload-all started ===")

                uid = os.getuid()
                tmux_dir = Path(f"/tmp/tmux-{uid}")
                if not tmux_dir.exists():
                    send_tmux_message("No tmux socket dir found", socket)
                    response = {"continue": False, "stopReason": "No tmux socket dir"}
                    print(json.dumps(response))
                    return

                kc1_sockets = [f.name for f in tmux_dir.iterdir() if f.name.startswith("kc1-")]
                if not kc1_sockets:
                    send_tmux_message("No kc1-* instances found", socket)
                    response = {"continue": False, "stopReason": "No kc1-* instances"}
                    print(json.dumps(response))
                    return

                count = 0
                for kc_socket in kc1_sockets:
                    if kc_socket == socket:
                        continue

                    result = subprocess.run(
                        ["tmux", "-L", kc_socket, "list-windows", "-F", "#{window_index}"],
                        capture_output=True, text=True
                    )
                    if result.returncode != 0:
                        continue

                    windows = result.stdout.strip().split('\n')

                    for win_idx in windows:
                        subprocess.run(["tmux", "-L", kc_socket, "send-keys", "-t", win_idx, "-l", ":reload"])
                        time.sleep(0.5)
                        subprocess.run(["tmux", "-L", kc_socket, "send-keys", "-t", win_idx, "Enter"])
                        count += 1
                        time.sleep(0.2)

                send_tmux_message(f"✓ Sent :reload to {count} instances", socket)
                response = {"continue": False, "stopReason": f"✓ Sent :reload to {count} instances"}
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

            # Regenerate tmux config (picks up hook changes etc)
            from kitty_claude.main import regenerate_tmux_config
            if profile:
                base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                base_config = Path.home() / ".config" / "kitty-claude"
            session_config_dir = base_config / "session-configs" / session_id
            Path("/tmp/kc-reload-debug.txt").write_text(f"session_config_dir={session_config_dir}\nprofile={profile}\nsocket={socket}\n")
            try:
                regenerate_tmux_config(session_config_dir, profile, socket)
                with open("/tmp/kc-reload-debug.txt", "a") as f:
                    f.write(f"tmux.conf exists: {(session_config_dir / 'tmux.conf').exists()}\n")
            except Exception as e:
                with open("/tmp/kc-reload-debug.txt", "a") as f:
                    f.write(f"ERROR: {e}\n")

            # Save auth from current session before regenerating config
            from kitty_claude.claude import save_auth_from_session, setup_session_config
            from kitty_claude.events import update_window
            save_auth_from_session(session_id, profile)

            # Merge session config (global + session.json overrides)
            session_config_dir = setup_session_config(session_id, profile)

            # Get window name and register in windows.json
            try:
                result = run(
                    ["tmux", "-L", socket, "display-message", "-p", "#{window_name}"],
                    capture_output=True, text=True, check=True
                )
                window_name = result.stdout.strip()
                update_window(session_id, window_name, socket, current_dir, profile)
                # Record window title in history
                record_title(window_name, profile)
            except Exception as e:
                log(f"Error updating window: {e}", profile)

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

        # Check for :todo command - per-directory todo list
        if prompt == ':todo' or prompt.startswith(':todo '):
            current_dir = input_data.get('cwd', os.getcwd())
            state_dir = get_state_dir()
            todos_dir = state_dir / "todos"
            todos_dir.mkdir(parents=True, exist_ok=True)

            # Key by directory path
            encoded_dir = current_dir.replace('/', '-').strip('-')
            todos_file = todos_dir / f"{encoded_dir}.json"

            # Load existing todos
            if todos_file.exists():
                try:
                    todos = json.loads(todos_file.read_text())
                except:
                    todos = []
            else:
                todos = []

            description = prompt[5:].strip() if len(prompt) > 5 else ""

            if description:
                # Add a todo
                todos.append({"text": description, "done": False})
                todos_file.write_text(json.dumps(todos, indent=2))
                send_tmux_message(f"✓ Todo added ({len(todos)} total)", socket)
                response = {"continue": False, "stopReason": f"✓ Todo added: {description}"}
            else:
                # List todos
                if not todos:
                    send_tmux_message("No todos for this directory.", socket)
                    response = {"continue": False, "stopReason": f"No todos for {current_dir}"}
                else:
                    lines = [f"Todos for {current_dir}:"]
                    for i, todo in enumerate(todos, 1):
                        marker = "x" if todo.get("done") else " "
                        lines.append(f"  [{marker}] {i}. {todo['text']}")
                    todo_text = "\n".join(lines)
                    send_tmux_message(f"{len(todos)} todo(s) for this dir", socket)
                    response = {"continue": False, "stopReason": todo_text}

            print(json.dumps(response))
            return

        # Check for :done command - mark a todo as done
        if prompt.startswith(':done '):
            current_dir = input_data.get('cwd', os.getcwd())
            state_dir = get_state_dir()
            todos_dir = state_dir / "todos"

            encoded_dir = current_dir.replace('/', '-').strip('-')
            todos_file = todos_dir / f"{encoded_dir}.json"

            if not todos_file.exists():
                send_tmux_message("No todos for this directory.", socket)
                response = {"continue": False, "stopReason": "No todos for this directory."}
                print(json.dumps(response))
                return

            try:
                todos = json.loads(todos_file.read_text())
            except:
                todos = []

            num_str = prompt[6:].strip()
            try:
                num = int(num_str)
                if 1 <= num <= len(todos):
                    todos[num - 1]["done"] = True
                    todos_file.write_text(json.dumps(todos, indent=2))
                    send_tmux_message(f"✓ Marked #{num} as done", socket)
                    response = {"continue": False, "stopReason": f"✓ Done: {todos[num - 1]['text']}"}
                else:
                    response = {"continue": False, "stopReason": f"❌ Invalid number. Have {len(todos)} todos."}
            except ValueError:
                response = {"continue": False, "stopReason": "❌ Usage: :done <number>"}

            print(json.dumps(response))
            return

        # Check for :mcp-approve command - add MCP server wrapped with approval proxy
        if prompt.startswith(':mcp-approve '):
            parts = prompt[13:].strip().split()
            session_id = input_data.get('session_id')

            if not parts:
                send_tmux_message("❌ Usage: :mcp-approve <cmd> [args...]", socket)
                response = {"continue": False, "stopReason": "Usage: :mcp-approve <command> [args...]"}
                print(json.dumps(response))
                return

            if not session_id:
                send_tmux_message("❌ No session ID available", socket)
                response = {"continue": False, "stopReason": "❌ No session ID available"}
                print(json.dumps(response))
                return

            command_name = parts[0]
            extra_args = parts[1:]

            # Resolve command path
            command_path = shutil.which(command_name)
            if not command_path:
                send_tmux_message(f"❌ Command '{command_name}' not found in PATH", socket)
                response = {"continue": False, "stopReason": f"❌ Command '{command_name}' not found in PATH"}
                print(json.dumps(response))
                return

            # Build the real server definition
            server_name = command_name.rsplit("/", 1)[-1]
            original_def = {"command": command_path}
            if extra_args:
                original_def["args"] = extra_args

            # Wrap with proxy
            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
            proxy_def = {
                "command": kitty_claude_path,
                "args": ["--proxy-mcp", json.dumps(original_def)],
            }

            # Save to session metadata
            state_dir = get_state_dir()
            metadata_file = state_dir / "sessions" / f"{session_id}.json"
            metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
            if "mcpServers" not in metadata:
                metadata["mcpServers"] = {}
            metadata["mcpServers"][server_name] = proxy_def
            metadata_file.parent.mkdir(parents=True, exist_ok=True)
            metadata_file.write_text(json.dumps(metadata, indent=2))

            send_tmux_message(f"✓ Added '{server_name}' with approval proxy - use :reload", socket)
            response = {
                "continue": False,
                "stopReason": f"✓ MCP server '{server_name}' added with approval proxy.\n\nUse :reload to start Claude with the new MCP server."
            }
            print(json.dumps(response))
            return

        # Check for :skills-mcp command
        if prompt == ':skills-mcp':
            session_id = input_data.get('session_id')
            if not session_id:
                send_tmux_message("❌ No session ID available", socket)
                response = {"continue": False, "stopReason": "❌ No session ID available"}
                print(json.dumps(response))
                return

            kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

            # Store in session metadata
            state_dir = get_state_dir()
            metadata_file = state_dir / "sessions" / f"{session_id}.json"
            metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
            if "mcpServers" not in metadata:
                metadata["mcpServers"] = {}
            metadata["mcpServers"]["kitty-claude-skills"] = {
                "command": kitty_claude_path,
                "args": ["--skills-mcp"],
            }
            metadata_file.parent.mkdir(parents=True, exist_ok=True)
            metadata_file.write_text(json.dumps(metadata, indent=2))

            send_tmux_message("✓ Skills MCP added - use :reload to apply", socket)
            response = {"continue": False, "stopReason": "✓ Skills MCP server added.\n\nUse :reload to start Claude with the skills MCP server."}
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

        # Check for :skill command (fzf picker if no name given)
        if prompt == ':skill' or prompt.startswith(':skill '):
            skill_name = prompt[7:].strip() if prompt.startswith(':skill ') else ''

            # If no name, show fzf picker of existing skills
            if not skill_name:
                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    global_skills_base = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile / "claude-data" / "skills"
                else:
                    global_skills_base = Path.home() / ".config" / "kitty-claude" / "claude-data" / "skills"

                skills = []
                if global_skills_base.exists():
                    for skill_dir in sorted(global_skills_base.iterdir()):
                        if skill_dir.is_dir():
                            skill_file = skill_dir / "SKILL.md"
                            desc = "(no description)"
                            if skill_file.exists():
                                content = skill_file.read_text()
                                for line in content.split('\n'):
                                    if line.startswith('description:'):
                                        desc = line[12:].strip()
                                        break
                            skills.append((skill_dir.name, desc))

                if not skills:
                    send_tmux_message("No skills found. Use :skill <name> to create one.", socket)
                    response = {"continue": False, "stopReason": "No skills found.\n\nUse :skill <name> to create a new skill."}
                    print(json.dumps(response))
                    return

                # Build fzf input
                import tempfile
                fzf_lines = [f"{name}\t{desc}" for name, desc in skills]
                tmp_input = Path(tempfile.mktemp())
                tmp_output = Path(tempfile.mktemp())
                tmp_input.write_text("\n".join(fzf_lines))

                subprocess.run([
                    "tmux", "-L", socket,
                    "display-popup", "-E", "-w", "60%", "-h", "50%",
                    f"cat {tmp_input} | fzf --delimiter='\\t' --with-nth=1,2 --header='Select skill to edit' > {tmp_output}"
                ])

                if tmp_output.exists() and tmp_output.read_text().strip():
                    skill_name = tmp_output.read_text().strip().split('\t')[0]
                    tmp_input.unlink(missing_ok=True)
                    tmp_output.unlink(missing_ok=True)
                else:
                    tmp_input.unlink(missing_ok=True)
                    tmp_output.unlink(missing_ok=True)
                    response = {"continue": False, "stopReason": "No skill selected."}
                    print(json.dumps(response))
                    return

            # Validate skill name (alphanumeric, dash, underscore only)
            if not all(c.isalnum() or c in '-_' for c in skill_name):
                send_tmux_message("❌ Skill name can only contain letters, numbers, dash, underscore", socket)
                response = {"continue": False, "stopReason": "❌ Invalid skill name"}
                print(json.dumps(response))
                return

            try:
                # Use global skills directory (not session-local) so all sessions share skills
                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    global_skills_base = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile / "claude-data" / "skills"
                else:
                    global_skills_base = Path.home() / ".config" / "kitty-claude" / "claude-data" / "skills"

                skills_dir = global_skills_base / skill_name
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

        # Check for :rule command (fzf picker if no name given)
        if prompt == ':rule' or prompt.startswith(':rule '):
            rule_name = prompt[6:].strip() if prompt.startswith(':rule ') else ''

            # Determine config directory based on profile
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            if profile:
                config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                config_dir = Path.home() / ".config" / "kitty-claude"
            rules_dir = config_dir / "rules"

            # If no name, show fzf picker of existing rules
            if not rule_name:
                rules = []
                if rules_dir.exists():
                    for rule_file in sorted(rules_dir.iterdir()):
                        if rule_file.suffix == '.md':
                            name = rule_file.stem
                            # Get first non-header line as description
                            content = rule_file.read_text()
                            desc = "(no description)"
                            for line in content.split('\n'):
                                line = line.strip()
                                if line and not line.startswith('#'):
                                    desc = line[:50] + ('...' if len(line) > 50 else '')
                                    break
                            rules.append((name, desc))

                if not rules:
                    send_tmux_message("No rules found. Use :rule <name> to create one.", socket)
                    response = {"continue": False, "stopReason": "No rules found.\n\nUse :rule <name> to create a new rule."}
                    print(json.dumps(response))
                    return

                # Build fzf input
                import tempfile
                fzf_lines = [f"{name}\t{desc}" for name, desc in rules]
                tmp_input = Path(tempfile.mktemp())
                tmp_output = Path(tempfile.mktemp())
                tmp_input.write_text("\n".join(fzf_lines))

                subprocess.run([
                    "tmux", "-L", socket,
                    "display-popup", "-E", "-w", "60%", "-h", "50%",
                    f"cat {tmp_input} | fzf --delimiter='\\t' --with-nth=1,2 --header='Select rule to edit' > {tmp_output}"
                ])

                if tmp_output.exists() and tmp_output.read_text().strip():
                    rule_name = tmp_output.read_text().strip().split('\t')[0]
                    tmp_input.unlink(missing_ok=True)
                    tmp_output.unlink(missing_ok=True)
                else:
                    tmp_input.unlink(missing_ok=True)
                    tmp_output.unlink(missing_ok=True)
                    response = {"continue": False, "stopReason": "No rule selected."}
                    print(json.dumps(response))
                    return

            # Validate rule name (alphanumeric, dash, underscore only)
            if not all(c.isalnum() or c in '-_' for c in rule_name):
                send_tmux_message("❌ Rule name can only contain letters, numbers, dash, underscore", socket)
                response = {"continue": False, "stopReason": "❌ Invalid rule name"}
                print(json.dumps(response))
                return

            try:
                # Ensure rules directory exists
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
        # :mcps - list MCP servers in current session
        if prompt == ':mcps':
            session_id = input_data.get('session_id')
            if not session_id:
                response = {"continue": False, "stopReason": "No session ID."}
                print(json.dumps(response))
                return

            state_dir = get_state_dir()
            metadata_file = state_dir / "sessions" / f"{session_id}.json"
            metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
            servers = metadata.get("mcpServers", {})

            if servers:
                lines = "MCP servers in this session:\n\n"
                for name, config in servers.items():
                    cmd = config.get("command", "?")
                    args = " ".join(config.get("args", []))
                    lines += f"  {name}: {cmd} {args}\n"
            else:
                lines = "No MCP servers in this session."

            response = {"continue": False, "stopReason": lines}
            print(json.dumps(response))
            return

        # Check for :mcp-remove command - remove an MCP server from session
        if prompt.startswith(':mcp-remove '):
            server_name = prompt[12:].strip()
            session_id = input_data.get('session_id')

            if not session_id or not server_name:
                send_tmux_message("❌ Usage: :mcp-remove <server-name>", socket)
                response = {"continue": False, "stopReason": "Usage: :mcp-remove <server-name>"}
                print(json.dumps(response))
                return

            state_dir = get_state_dir()
            metadata_file = state_dir / "sessions" / f"{session_id}.json"
            metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
            mcp_servers = metadata.get("mcpServers", {})

            if server_name not in mcp_servers:
                send_tmux_message(f"❌ '{server_name}' not found", socket)
                available = ", ".join(mcp_servers.keys()) if mcp_servers else "none"
                response = {"continue": False, "stopReason": f"MCP server '{server_name}' not in session metadata.\nAvailable: {available}"}
                print(json.dumps(response))
                return

            del mcp_servers[server_name]
            metadata["mcpServers"] = mcp_servers
            metadata_file.write_text(json.dumps(metadata, indent=2))

            send_tmux_message(f"✓ Removed '{server_name}' - use :reload", socket)
            response = {"continue": False, "stopReason": f"✓ MCP server '{server_name}' removed.\n\nUse :reload to apply."}
            print(json.dumps(response))
            return

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
                response = {"continue": False, "stopReason": "No roles found. Use :role-add <name> <num> to create one."}
                print(json.dumps(response))
                return

            lines = []
            for role_file in sorted(roles_dir.glob("*.json")):
                try:
                    role = json.loads(role_file.read_text())
                    servers = list(role.get("mcpServers", {}).keys())
                    perms = role.get("permissions", {}).get("allow", [])
                    parts = []
                    if servers:
                        parts.append(f"servers: {', '.join(servers)}")
                    if perms:
                        parts.append(f"{len(perms)} permissions")
                    desc = "; ".join(parts) if parts else "(empty)"
                    lines.append(f"  {role_file.stem}: {desc}")
                except:
                    lines.append(f"  {role_file.stem}: (error reading)")

            message = "Roles:\n" + "\n".join(lines)
            send_tmux_message(f"📋 {len(lines)} roles", socket)
            response = {"continue": False, "stopReason": message}
            print(json.dumps(response))
            return

        # :role-add-all <role> - add all current permissions to a role
        if prompt.startswith(':role-add-all '):
            role_name = prompt[14:].strip()
            if not role_name:
                response = {"continue": False, "stopReason": "Usage: :role-add-all <role-name>"}
                print(json.dumps(response))
                return

            if not all(c.isalnum() or c in '-_' for c in role_name):
                response = {"continue": False, "stopReason": "Role name can only contain letters, numbers, dash, underscore."}
                print(json.dumps(response))
                return

            # Gather all permissions (same as :permissions)
            allow_rules = []
            settings_file = claude_data_dir / "settings.json"
            if settings_file.exists():
                try:
                    settings = json.loads(settings_file.read_text())
                    allow_rules.extend(settings.get("permissions", {}).get("allow", []))
                except (json.JSONDecodeError, OSError):
                    pass

            cwd = input_data.get('cwd', os.getcwd())
            project_settings = Path(cwd) / ".claude" / "settings.local.json"
            if project_settings.exists():
                try:
                    proj = json.loads(project_settings.read_text())
                    allow_rules.extend(proj.get("permissions", {}).get("allow", []))
                except (json.JSONDecodeError, OSError):
                    pass

            # Deduplicate
            seen = set()
            unique_rules = []
            for rule in allow_rules:
                if rule not in seen:
                    seen.add(rule)
                    unique_rules.append(rule)

            if not unique_rules:
                response = {"continue": False, "stopReason": "No permissions to add."}
                print(json.dumps(response))
                return

            # Load or create role file
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            if profile:
                config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                config_dir = Path.home() / ".config" / "kitty-claude"

            roles_dir = config_dir / "mcp-roles"
            roles_dir.mkdir(parents=True, exist_ok=True)
            role_file = roles_dir / f"{role_name}.json"

            if role_file.exists():
                role_data = json.loads(role_file.read_text())
            else:
                role_data = {"mcpServers": {}, "permissions": {"allow": []}}

            existing = set(role_data.setdefault("permissions", {}).setdefault("allow", []))
            added = 0
            for rule in unique_rules:
                if rule not in existing:
                    role_data["permissions"]["allow"].append(rule)
                    existing.add(rule)
                    added += 1

            role_file.write_text(json.dumps(role_data, indent=2))
            send_tmux_message(f"Added {added} permissions to '{role_name}'", socket)
            response = {"continue": False, "stopReason": f"Added {added} permissions to role '{role_name}' ({len(role_data['permissions']['allow'])} total)."}
            print(json.dumps(response))
            return

        # :role-add-mcp <role> <server-name> - add MCP server from session to a role
        if prompt.startswith(':role-add-mcp '):
            parts = prompt[14:].strip().split(None, 1)
            if len(parts) != 2:
                response = {"continue": False, "stopReason": "Usage: :role-add-mcp <role-name> <server-name>"}
                print(json.dumps(response))
                return

            role_name, server_name = parts

            if not all(c.isalnum() or c in '-_' for c in role_name):
                response = {"continue": False, "stopReason": "Role name can only contain letters, numbers, dash, underscore."}
                print(json.dumps(response))
                return

            # Get MCP servers from session metadata
            session_id = input_data.get('session_id')
            if not session_id:
                response = {"continue": False, "stopReason": "No session ID."}
                print(json.dumps(response))
                return

            state_dir = get_state_dir()
            metadata_file = state_dir / "sessions" / f"{session_id}.json"
            metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
            session_servers = metadata.get("mcpServers", {})

            if server_name not in session_servers:
                available = ", ".join(session_servers.keys()) if session_servers else "none"
                response = {"continue": False, "stopReason": f"Server '{server_name}' not in session. Available: {available}"}
                print(json.dumps(response))
                return

            # Load or create role file
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            if profile:
                config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                config_dir = Path.home() / ".config" / "kitty-claude"

            roles_dir = config_dir / "mcp-roles"
            roles_dir.mkdir(parents=True, exist_ok=True)
            role_file = roles_dir / f"{role_name}.json"

            if role_file.exists():
                role_data = json.loads(role_file.read_text())
            else:
                role_data = {"mcpServers": {}, "permissions": {"allow": []}}

            role_data.setdefault("mcpServers", {})[server_name] = session_servers[server_name]
            role_file.write_text(json.dumps(role_data, indent=2))
            send_tmux_message(f"Added '{server_name}' to role '{role_name}'", socket)
            response = {"continue": False, "stopReason": f"Added MCP server '{server_name}' to role '{role_name}'."}
            print(json.dumps(response))
            return

        # :role-add <role> <num> - add permission by number to a role
        if prompt.startswith(':role-add '):
            parts = prompt[10:].strip().split()
            if len(parts) != 2 or not parts[1].isdigit():
                response = {"continue": False, "stopReason": "Usage: :role-add <role-name> <num>\nRun :permissions to see numbered list."}
                print(json.dumps(response))
                return

            role_name, num_str = parts
            target_num = int(num_str)

            if not all(c.isalnum() or c in '-_' for c in role_name):
                response = {"continue": False, "stopReason": "Role name can only contain letters, numbers, dash, underscore."}
                print(json.dumps(response))
                return

            # Rebuild the same numbered list as :permissions
            allow_rules = []
            settings_file = claude_data_dir / "settings.json"
            if settings_file.exists():
                try:
                    settings = json.loads(settings_file.read_text())
                    for rule in settings.get("permissions", {}).get("allow", []):
                        allow_rules.append((rule, str(settings_file)))
                except (json.JSONDecodeError, OSError):
                    pass

            cwd = input_data.get('cwd', os.getcwd())
            project_settings = Path(cwd) / ".claude" / "settings.local.json"
            if project_settings.exists():
                try:
                    proj = json.loads(project_settings.read_text())
                    for rule in proj.get("permissions", {}).get("allow", []):
                        allow_rules.append((rule, str(project_settings)))
                except (json.JSONDecodeError, OSError):
                    pass

            seen = set()
            unique_rules = []
            for rule, source in allow_rules:
                if rule not in seen:
                    seen.add(rule)
                    unique_rules.append((rule, source))

            if target_num < 1 or target_num > len(unique_rules):
                response = {"continue": False, "stopReason": f"Invalid number {target_num}. Run :permissions to see valid range (1-{len(unique_rules)})."}
                print(json.dumps(response))
                return

            rule_to_add = unique_rules[target_num - 1][0]

            # Load or create role file
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            if profile:
                config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                config_dir = Path.home() / ".config" / "kitty-claude"

            roles_dir = config_dir / "mcp-roles"
            roles_dir.mkdir(parents=True, exist_ok=True)
            role_file = roles_dir / f"{role_name}.json"

            if role_file.exists():
                role_data = json.loads(role_file.read_text())
            else:
                role_data = {"mcpServers": {}, "permissions": {"allow": []}}

            role_data.setdefault("permissions", {}).setdefault("allow", [])
            if rule_to_add in role_data["permissions"]["allow"]:
                response = {"continue": False, "stopReason": f"Already in role '{role_name}': {rule_to_add}"}
            else:
                role_data["permissions"]["allow"].append(rule_to_add)
                role_file.write_text(json.dumps(role_data, indent=2))
                send_tmux_message(f"Added to role '{role_name}'", socket)
                response = {"continue": False, "stopReason": f"Added to role '{role_name}': {rule_to_add}"}

            print(json.dumps(response))
            return

        # :roles-current - show active roles in this session
        if prompt == ':roles-current':
            session_id = input_data.get('session_id')
            if not session_id:
                response = {"continue": False, "stopReason": "No session ID."}
                print(json.dumps(response))
                return

            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            if profile:
                config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                config_dir = Path.home() / ".config" / "kitty-claude"

            state_dir = get_state_dir()
            metadata_file = state_dir / "sessions" / f"{session_id}.json"
            metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
            active_roles = metadata.get("activeRoles", [])

            # Check for implicit roles
            implicit_roles = []

            # Default role (always active if exists)
            default_role_file = config_dir / "mcp-roles" / "default.json"
            if default_role_file.exists() and "default" not in active_roles:
                implicit_roles.append("default (implicit)")

            # Title-based roles
            title_roles_file = config_dir / "title-roles.json"
            if title_roles_file.exists():
                try:
                    title_mappings = json.loads(title_roles_file.read_text())
                    tmux_socket = os.environ.get('KITTY_CLAUDE_TMUX_SOCKET', 'kitty-claude')
                    result = subprocess.run(
                        ["tmux", "-L", tmux_socket, "display-message", "-p", "#{window_name}"],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        window_name = result.stdout.strip()
                        if window_name in title_mappings:
                            for role_name in title_mappings[window_name]:
                                if role_name not in active_roles:
                                    implicit_roles.append(f"{role_name} (from title '{window_name}')")
                except:
                    pass

            all_roles = [f"  {r}" for r in active_roles] + [f"  {r}" for r in implicit_roles]

            if all_roles:
                lines = "Active roles in this session:\n\n" + "\n".join(all_roles)
            else:
                lines = "No active roles. Use :role <name> to activate one."

            response = {"continue": False, "stopReason": lines}
            print(json.dumps(response))
            return

        # :title-role <title> <role> - map a tmux window title to a role
        if prompt.startswith(':title-role'):
            parts = prompt[11:].strip().split()
            if len(parts) < 1:
                # Show current mappings
                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
                else:
                    config_dir = Path.home() / ".config" / "kitty-claude"

                title_roles_file = config_dir / "title-roles.json"
                if title_roles_file.exists():
                    mappings = json.loads(title_roles_file.read_text())
                else:
                    mappings = {}

                if mappings:
                    lines = "Title-role mappings:\n\n"
                    for title, role_list in mappings.items():
                        lines += f"  {title} -> {', '.join(role_list)}\n"
                else:
                    lines = "No title-role mappings. Use :title-role <title> <role> to add one."

                response = {"continue": False, "stopReason": lines}
                print(json.dumps(response))
                return

            if len(parts) < 2:
                response = {"continue": False, "stopReason": "Usage: :title-role <title> <role>\n       :title-role (show mappings)"}
                print(json.dumps(response))
                return

            title, role_name = parts[0], parts[1]

            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            if profile:
                config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            else:
                config_dir = Path.home() / ".config" / "kitty-claude"

            title_roles_file = config_dir / "title-roles.json"
            if title_roles_file.exists():
                mappings = json.loads(title_roles_file.read_text())
            else:
                mappings = {}

            if title not in mappings:
                mappings[title] = []
            if role_name not in mappings[title]:
                mappings[title].append(role_name)

            title_roles_file.write_text(json.dumps(mappings, indent=2))
            send_tmux_message(f"Mapped '{title}' -> {role_name}", socket)
            response = {"continue": False, "stopReason": f"Mapped title '{title}' -> role '{role_name}'\nCurrent: {title} -> {', '.join(mappings[title])}"}
            print(json.dumps(response))
            return

        # :role (no args) - fuzzy pick a role to activate
        if prompt == ':role':
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

                roles_dir = config_dir / "mcp-roles"
                if not roles_dir.exists() or not any(roles_dir.glob("*.json")):
                    send_tmux_message("No roles found", socket)
                    response = {"continue": False, "stopReason": "No roles found. Use :role-add <name> <num> to create one."}
                    print(json.dumps(response))
                    return

                # Build fzf input: "role_name\tdescription"
                fzf_lines = []
                for role_file in sorted(roles_dir.glob("*.json")):
                    try:
                        role = json.loads(role_file.read_text())
                        servers = list(role.get("mcpServers", {}).keys())
                        perms = role.get("permissions", {}).get("allow", [])
                        parts = []
                        if servers:
                            parts.append(f"{len(servers)} servers")
                        if perms:
                            parts.append(f"{len(perms)} perms")
                        desc = ", ".join(parts) if parts else "empty"
                        fzf_lines.append(f"{role_file.stem}\t{desc}")
                    except:
                        fzf_lines.append(f"{role_file.stem}\t(error reading)")

                uid = os.getuid()
                tmp_input = Path(f"/tmp/kc-role-{uid}.txt")
                tmp_output = Path(f"/tmp/kc-role-{uid}-out.txt")
                tmp_input.write_text("\n".join(fzf_lines))
                tmp_output.unlink(missing_ok=True)

                subprocess.run([
                    "tmux", "-L", socket,
                    "display-popup", "-E", "-w", "60%", "-h", "40%",
                    f"cat {tmp_input} | fzf --delimiter='\\t' --with-nth=1,2 --header='Select role to activate' > {tmp_output}"
                ])

                if tmp_output.exists():
                    selection = tmp_output.read_text().strip()
                    tmp_output.unlink(missing_ok=True)
                    if selection:
                        role_name = selection.split('\t')[0]
                        # Activate the selected role (same logic as :role <name>)
                        role_file = roles_dir / f"{role_name}.json"
                        role = json.loads(role_file.read_text())
                        role_servers = role.get("mcpServers", {})

                        state_dir = get_state_dir()
                        metadata_file = state_dir / "sessions" / f"{session_id}.json"
                        metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
                        if "mcpServers" not in metadata:
                            metadata["mcpServers"] = {}
                        metadata["mcpServers"].update(role_servers)

                        active_roles = metadata.get("activeRoles", [])
                        if role_name not in active_roles:
                            active_roles.append(role_name)
                        metadata["activeRoles"] = active_roles
                        metadata_file.write_text(json.dumps(metadata, indent=2))

                        server_names = ", ".join(role_servers.keys()) if role_servers else "none"
                        perm_count = len(role.get("permissions", {}).get("allow", []))
                        send_tmux_message(f"✓ Role '{role_name}' activated - use :reload", socket)
                        response = {
                            "continue": False,
                            "stopReason": f"✓ Role '{role_name}' activated (servers: {server_names}, permissions: {perm_count})\n\nUse :reload to apply."
                        }
                        print(json.dumps(response))
                        return

                send_tmux_message("No role selected", socket)
                response = {"continue": False, "stopReason": "No role selected."}
                print(json.dumps(response))
                return

            except Exception as e:
                send_tmux_message(f"❌ Error: {e}", socket)
                response = {"continue": False, "stopReason": f"❌ Error: {str(e)}"}
                print(json.dumps(response))
                return

        # :role <name> - activate a role in the current session
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

                # Add to session metadata: merge MCP servers + track as active role
                state_dir = get_state_dir()
                metadata_file = state_dir / "sessions" / f"{session_id}.json"
                metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
                if "mcpServers" not in metadata:
                    metadata["mcpServers"] = {}
                metadata["mcpServers"].update(role_servers)

                active_roles = metadata.get("activeRoles", [])
                if role_name not in active_roles:
                    active_roles.append(role_name)
                metadata["activeRoles"] = active_roles
                metadata_file.write_text(json.dumps(metadata, indent=2))

                server_names = ", ".join(role_servers.keys()) if role_servers else "none"
                perm_count = len(role.get("permissions", {}).get("allow", []))
                send_tmux_message(f"✓ Role '{role_name}' activated - use :reload", socket)
                response = {
                    "continue": False,
                    "stopReason": f"✓ Role '{role_name}' activated (servers: {server_names}, permissions: {perm_count})\n\nUse :reload to apply."
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
                from kitty_claude.events import get_all_windows

                # Get all windows from the central file
                windows = get_all_windows()
                my_session_id = input_data.get('session_id')

                # Build fzf lines: session_id, title, socket, path
                fzf_lines = []
                for session_id, info in windows.items():
                    if session_id == my_session_id:
                        continue  # Skip current window
                    title = info.get("title", session_id[:8])
                    win_socket = info.get("socket", "")
                    path = info.get("path", "")
                    fzf_lines.append(f"{session_id}\t{title}\t{win_socket}\t{path}")

                if not fzf_lines:
                    send_tmux_message("No other windows", socket)
                    response = {"continue": False, "stopReason": "No other windows to send to"}
                    print(json.dumps(response))
                    return

                uid = os.getuid()
                tmp_input = Path(f"/tmp/kc-send-{uid}.txt")
                tmp_output = Path(f"/tmp/kc-send-{uid}-out.txt")
                tmp_input.write_text("\n".join(fzf_lines))
                tmp_output.unlink(missing_ok=True)

                subprocess.run([
                    "tmux", "-L", socket,
                    "display-popup", "-E", "-w", "70%", "-h", "40%",
                    f"cat {tmp_input} | fzf --delimiter='\\t' --with-nth=2,4 --header='Send to:' > {tmp_output}"
                ])

                if tmp_output.exists():
                    selected = tmp_output.read_text().strip()
                    if selected:
                        parts = selected.split("\t")
                        target_session_id = parts[0]
                        target_title = parts[1] if len(parts) > 1 else target_session_id[:8]
                        target_socket = parts[2] if len(parts) > 2 else socket

                        # Find the target pane - in one-tab mode (kc1-*) there's only one pane
                        if target_socket.startswith("kc1-"):
                            # One-tab mode: send directly to the only pane
                            target_pane = "%0"
                        else:
                            # Multi-tab mode: find the window with matching session_id
                            result = run(
                                ["tmux", "-L", target_socket, "list-windows", "-F", "#{window_id} #{@session_id}"],
                                capture_output=True, text=True, check=True
                            )
                            target_pane = None
                            for line in result.stdout.strip().split("\n"):
                                line_parts = line.split()
                                if len(line_parts) >= 2 and line_parts[1] == target_session_id:
                                    target_pane = line_parts[0]
                                    break

                        if target_pane:
                            run([
                                "tmux", "-L", target_socket,
                                "send-keys", "-t", target_pane, "-l", message
                            ])
                            run([
                                "tmux", "-L", target_socket,
                                "send-keys", "-t", target_pane, "Enter"
                            ])
                            # Store message in target's inbox
                            import time
                            from kitty_claude.events import get_runtime_dir, get_all_windows
                            msgs_dir = get_runtime_dir() / "messages"
                            msgs_dir.mkdir(exist_ok=True)
                            inbox_file = msgs_dir / f"{target_session_id}.jsonl"
                            # Get sender info
                            my_windows = get_all_windows()
                            my_info = my_windows.get(my_session_id, {})
                            my_title = my_info.get("title", "unknown")
                            msg_entry = {
                                "from": my_title,
                                "from_session": my_session_id,
                                "message": message,
                                "ts": time.time(),
                                "read": False
                            }
                            with open(inbox_file, "a") as f:
                                f.write(json.dumps(msg_entry) + "\n")
                            send_tmux_message(f"✓ Sent to {target_title}", socket)
                            response = {"continue": False, "stopReason": f"✓ Message sent to {target_title}"}
                        else:
                            send_tmux_message(f"❌ Could not find window (stale?)", socket)
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

        # :msgs - show inbox messages
        if prompt == ':msgs':
            my_session_id = input_data.get('session_id')
            if not my_session_id:
                response = {"continue": False, "stopReason": "No session ID"}
                print(json.dumps(response))
                return

            try:
                from kitty_claude.events import get_runtime_dir
                msgs_dir = get_runtime_dir() / "messages"
                inbox_file = msgs_dir / f"{my_session_id}.jsonl"

                if not inbox_file.exists():
                    send_tmux_message("📭 No messages", socket)
                    response = {"continue": False, "stopReason": "📭 No messages in inbox"}
                    print(json.dumps(response))
                    return

                # Read all messages
                messages = []
                for line in inbox_file.read_text().strip().split("\n"):
                    if line:
                        try:
                            messages.append(json.loads(line))
                        except:
                            pass

                if not messages:
                    send_tmux_message("📭 No messages", socket)
                    response = {"continue": False, "stopReason": "📭 No messages in inbox"}
                    print(json.dumps(response))
                    return

                # Format messages for display
                lines = []
                for msg in messages:
                    ts = msg.get("ts", 0)
                    time_str = time.strftime("%H:%M", time.localtime(ts))
                    from_title = msg.get("from", "unknown")
                    text = msg.get("message", "")
                    read_mark = "" if msg.get("read") else "●"
                    lines.append(f"{read_mark} [{time_str}] {from_title}: {text}")

                # Mark all as read
                updated_messages = []
                for msg in messages:
                    msg["read"] = True
                    updated_messages.append(msg)
                with open(inbox_file, "w") as f:
                    for msg in updated_messages:
                        f.write(json.dumps(msg) + "\n")

                # Show in popup
                uid = os.getuid()
                tmp_msgs = Path(f"/tmp/kc-msgs-{uid}.txt")
                tmp_msgs.write_text("\n".join(lines))
                subprocess.run([
                    "tmux", "-L", socket,
                    "display-popup", "-E", "-w", "80%", "-h", "60%",
                    f"cat {tmp_msgs}; read -n1"
                ])
                tmp_msgs.unlink(missing_ok=True)

                response = {"continue": False, "stopReason": f"📬 {len(messages)} message(s)"}
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

                # Build env vars for plugin
                env_exports = []
                if session_id:
                    env_exports.append(f"KITTY_CLAUDE_SESSION_ID={session_id}")
                env_exports.append(f"KITTY_CLAUDE_SOCKET={socket}")
                env_exports.append(f"KITTY_CLAUDE_CWD={input_data.get('cwd', os.getcwd())}")
                env_str = " ".join(env_exports)

                # Run plugin in tmux popup (allows fzf etc), output to temp file
                import tempfile
                tmp_output = Path(tempfile.mktemp())

                plugin_cmd = f"{plugin_bin}"
                if cmd_args:
                    plugin_cmd += f" {cmd_args}"

                subprocess.run([
                    "tmux", "-L", socket,
                    "display-popup", "-E", "-w", "60%", "-h", "50%",
                    f"{env_str} {plugin_cmd} > {tmp_output}"
                ])

                output = ""
                if tmp_output.exists():
                    output = tmp_output.read_text().strip()
                    tmp_output.unlink(missing_ok=True)

                if output.startswith(':'):
                    # Re-dispatch as a colon command
                    print(output)
                elif output:
                    response = {"continue": False, "stopReason": output}
                    print(json.dumps(response))
                else:
                    response = {"continue": False, "stopReason": f"✓ {cmd_name}"}
                    print(json.dumps(response))
                return

        # Not a custom command, save start time and pass through
        session_id = input_data.get('session_id')
        if session_id:
            save_request_start_time(session_id)
        print(prompt)

    except Exception as e:
        import traceback
        # Log error with full traceback
        error_msg = f"Hook error: {str(e)}"
        tb = traceback.format_exc()
        send_tmux_message(f"❌ {error_msg}", socket)
        profile = os.environ.get('KITTY_CLAUDE_PROFILE')
        log(f"COLON COMMAND ERROR: {error_msg}\n{tb}", profile)
        with open("/tmp/kitty-claude-hook-error.log", "a") as f:
            f.write(f"{error_msg}\n{tb}\n")
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


def handle_session_start():
    """Handle SessionStart hook - increment run counter, show messages from previous run."""
    try:
        input_data = json.loads(sys.stdin.read())
        session_id = input_data.get('session_id')
        reason = input_data.get('reason', 'startup')

        if not session_id:
            print(json.dumps({"continue": True}))
            return

        profile = os.environ.get('KITTY_CLAUDE_PROFILE')
        if profile:
            base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
        else:
            base_config = Path.home() / ".config" / "kitty-claude"

        session_dir = base_config / "session-configs" / session_id
        run_file = session_dir / ".run-counter"
        messages_file = session_dir / ".startup-messages"

        # Read and increment run counter
        current_run = 0
        if run_file.exists():
            try:
                current_run = int(run_file.read_text().strip())
            except (ValueError, OSError):
                pass
        current_run += 1
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            run_file.write_text(str(current_run))
        except OSError:
            pass

        # Check for messages from previous run
        messages_to_show = []
        if messages_file.exists():
            try:
                all_messages = json.loads(messages_file.read_text())
                previous_run = current_run - 1
                for msg in all_messages:
                    if msg.get("run") == previous_run:
                        messages_to_show.append(msg.get("text", ""))
                # Clean up - delete the file
                messages_file.unlink()
            except (json.JSONDecodeError, OSError):
                pass

        # Show messages via tmux popup and as additionalContext
        if messages_to_show:
            context = "\n".join(messages_to_show)
            socket = os.environ.get('KITTY_CLAUDE_TMUX_SOCKET')
            if socket:
                # Write script to temp file that shows message and waits
                uid = os.getuid()
                msg_file = Path(f"/tmp/kc-popup-{uid}.txt")
                script_file = Path(f"/tmp/kc-popup-{uid}.sh")
                display_text = "\n".join(messages_to_show)
                msg_file.write_text(display_text)
                script_file.write_text(f'''#!/bin/bash
cat {msg_file}
echo ""
echo "[press Enter to close, or wait 30s]"
read -t 30
''')
                script_file.chmod(0o755)
                subprocess.Popen([
                    "tmux", "-L", socket, "display-popup",
                    "-w", "70", "-h", str(len(messages_to_show) + 5),
                    "-E", str(script_file)
                ], stderr=subprocess.DEVNULL)
            print(json.dumps({"continue": True, "additionalContext": context}))
        else:
            print(json.dumps({"continue": True}))

    except Exception as e:
        with open("/tmp/kitty-claude-session-start-error.log", "a") as f:
            f.write(f"SessionStart hook error: {str(e)}\n")
        print(json.dumps({"continue": True}))


def handle_stop():
    """Handle Stop hook - calculate and save response duration, drain command queue."""
    try:
        # Read JSON from stdin
        input_data = json.loads(sys.stdin.read())
        session_id = input_data.get('session_id')

        if session_id:
            save_response_duration(session_id)
            # Remove from open sessions list
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            remove_open_session(session_id, profile)

        # Drain command queue - send next queued command to the tmux pane
        socket = os.environ.get('KITTY_CLAUDE_TMUX_SOCKET')
        if socket:
            uid = os.getuid()
            queue_file = Path(f"/run/user/{uid}/kc-queue-{socket}.txt")
            if queue_file.exists():
                try:
                    lines = queue_file.read_text().splitlines()
                    if lines:
                        command = lines[0]
                        # Write remaining lines back (or remove file if empty)
                        remaining = lines[1:]
                        if remaining:
                            queue_file.write_text("\n".join(remaining) + "\n")
                        else:
                            queue_file.unlink()
                        # Send command to tmux pane, then press Enter
                        # Delay to let Claude's input be ready
                        import time
                        time.sleep(1)
                        subprocess.run(
                            ["tmux", "-L", socket, "send-keys", "-l", command],
                            capture_output=True, timeout=5,
                        )
                        time.sleep(0.3)
                        subprocess.run(
                            ["tmux", "-L", socket, "send-keys", "Enter"],
                            capture_output=True, timeout=5,
                        )
                except Exception:
                    pass
    except Exception as e:
        # Log error silently
        with open("/tmp/kitty-claude-stop-hook-error.log", "a") as f:
            f.write(f"Stop hook error: {str(e)}\n")


def handle_pre_tool_use():
    """Handle PreToolUse hook - deny expired timed permissions."""
    import time
    import fnmatch

    try:
        input_data = json.loads(sys.stdin.read())
        tool_name = input_data.get('tool_name', '')
        tool_input = input_data.get('tool_input', {})

        # Build the tool string to match against patterns
        # Format: ToolName or ToolName(param:value) for Bash
        if tool_name == 'Bash':
            command = tool_input.get('command', '')
            tool_string = f"Bash({command})"
        elif tool_name.startswith('mcp__'):
            tool_string = tool_name
        else:
            tool_string = tool_name

        # Load timed permissions
        timed_perms = load_timed_permissions()
        now = time.time()

        for perm in timed_perms:
            pattern = perm.get('pattern', '')
            expires = perm.get('expires', 0)

            # Check if pattern matches
            # Support glob-style matching: Bash(npm:*) matches Bash(npm run test)
            # Convert pattern to regex-friendly form
            if pattern.endswith(':*)'):
                # Pattern like Bash(npm:*) should match Bash(npm anything)
                prefix = pattern[:-2]  # Remove :*)
                if tool_string.startswith(prefix):
                    # This tool matches the pattern
                    if now > expires:
                        # Permission expired - deny
                        print(json.dumps({
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": f"Timed permission expired: {pattern}"
                            }
                        }))
                        return
                    # Not expired - let Claude's normal permission handle it
                    return
            elif fnmatch.fnmatch(tool_string, pattern) or tool_string == pattern:
                # Exact match or glob match
                if now > expires:
                    print(json.dumps({
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": f"Timed permission expired: {pattern}"
                        }
                    }))
                    return
                return

        # No matching timed permission - let normal flow continue
        # Exit 0 without JSON output

    except Exception as e:
        with open("/tmp/kitty-claude-pre-tool-use-error.log", "a") as f:
            f.write(f"PreToolUse hook error: {str(e)}\n")
        # On error, don't block - let normal flow continue