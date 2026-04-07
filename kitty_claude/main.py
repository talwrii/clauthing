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
import signal
import time
from pathlib import Path

from kitty_claude.logging import log, get_log_dir, get_run_log_file, cleanup_old_run_logs, run
from kitty_claude.logs import handle_last_logs, handle_follow_logs
from kitty_claude.window_utils import (
    find_and_focus_window,
    open_session_notes
)
from kitty_claude.tmux import (
    send_tmux_message,
    get_runtime_tmux_state_file
)
from kitty_claude.claude import new_window
from kitty_claude.colon_command import cleanup_expired_timed_permissions
from kitty_claude.hooks import (
    handle_session_start,
    handle_user_prompt_submit,
    handle_run_command,
    handle_stop,
    handle_pre_tool_use,
)
from kitty_claude.session import (
    save_session_metadata,
    get_session_name,
    get_open_sessions_file,
    add_open_session,
    remove_open_session,
    get_open_sessions
)
from kitty_claude.tmux_status import handle_tmux_status
from kitty_claude.rules import save_rule, build_claude_md, list_rules, show_rule

def regenerate_tmux_config(config_dir, profile=None, tmux_socket=None):
    """Regenerate the tmux.conf file and source it if tmux is running.

    This ensures hooks and bindings are updated when code changes.
    """
    if tmux_socket is None:
        tmux_socket = f"kitty-claude-{profile}" if profile else "kitty-claude"

    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
    profile_arg = f"--profile {profile} " if profile else ""
    jail_dir = f"/tmp/{tmux_socket}"
    kitty_claude_cmd = f"'{kitty_claude_path}' {profile_arg}--session"

    tmux_config_path = Path(config_dir) / "tmux.conf"
    instance_uuid = os.environ.get("KITTY_CLAUDE_INSTANCE_UUID", "")

    config_content = f"""\
# kitty-claude tmux config (auto-regenerated)
# Kill session when kitty window closes
set -g destroy-unattached on

# Set tmux socket name so hooks can find it
set-environment -g KITTY_CLAUDE_TMUX_SOCKET "{tmux_socket}"
set-environment -g KITTY_CLAUDE_INSTANCE_UUID "{instance_uuid}"

# Default command is claude wrapper for session tracking
set -g default-command "{kitty_claude_cmd}"

# Bind C-n directly (no prefix) to open new window with claude in jail
bind -n C-n new-window -c "{jail_dir}" {kitty_claude_cmd}

# Also override default C-b c
bind c new-window -c "{jail_dir}" {kitty_claude_cmd}

# C-w closes current window, but not the last one
bind -n C-w run-shell "kitty-claude --close-window"

# C-v passthrough for paste
bind -n C-v send-keys C-v

# Alt-r to restart kitty-claude
bind -n M-r run-shell "kitty-claude {profile_arg}--restart"

# Alt-l to reload (send :reload to pane)
bind -n M-l send-keys ':reload' Enter

# Alt-e to open session notes
bind -n M-e run-shell "kitty-claude {profile_arg}--notes"

# C-p for session picker (fuzzy find with popup)
bind -n C-p display-popup -E -w 80% -h 60% "kitty-claude {profile_arg}--picker"

# C-q: queue a command for when Claude finishes responding
bind -n C-q display-popup -E -w 60% -h 20% "printf 'Queue command (runs when Claude finishes):\\n'; read cmd; echo \\"$cmd\\" >> /run/user/$(id -u)/kc-queue-{tmux_socket}.txt; printf \\"Queued: $cmd\\n\\"; sleep 0.5"

# M-k: show keybindings help
bind -n M-k display-popup -E -w 50% -h 70% "kitty-claude --show-help"

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
bind -n M-n command-prompt -I "#W" -p "Session name:" "rename-window '%%'"

# Simple status bar (use status-format to avoid conflicts)
set -g status on
set -g status-style bg=colour235,fg=colour248
set -g status-format[0] '#[align=left] #W #[align=right] #{{pane_current_path}} '
set -gu status-format[1]
set -gu status-format[2]

# Mirror tmux window renames into kitty-claude state.
set-hook -g window-renamed 'run-shell "kitty-claude {profile_arg}--socket {tmux_socket} --window-id #{{hook_window}} --rename \\"#W\\" 2>&1 | tee -a /tmp/kc-rename-hook.log"'
"""

    tmux_config_path.write_text(config_content)

    # Source the new config if tmux is running
    try:
        result = subprocess.run(
            ["tmux", "-L", tmux_socket, "source-file", str(tmux_config_path)],
            capture_output=True, text=True
        )
        with open("/tmp/kc-reload-debug.txt", "a") as f:
            f.write(f"source-file returncode: {result.returncode}\n")
            if result.stderr:
                f.write(f"source-file stderr: {result.stderr}\n")
    except Exception as e:
        with open("/tmp/kc-reload-debug.txt", "a") as f:
            f.write(f"source-file exception: {e}\n")

    return tmux_config_path


def fork_with_log_tailing(exec_func, profile=None):
    """Fork process: child execs, parent tails logs to stderr.
    
    Args:
        exec_func: Function that calls os.execvp (will be called in child)
        profile: Profile name for finding log file
    """
    import select
    
    log_file = get_run_log_file(profile)
    
    # Ensure log file exists
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.touch()
    
    pid = os.fork()
    
    if pid == 0:
        # Child process - do the exec
        exec_func()
        # exec doesn't return, but just in case:
        sys.exit(0)
    else:
        # Parent process - tail the log file
        print(f"[kitty-claude] Streaming logs from {log_file}", file=sys.stderr)
        print(f"[kitty-claude] Child PID: {pid}", file=sys.stderr)
        print("-" * 60, file=sys.stderr)
        
        try:
            with open(log_file, 'r') as f:
                # Seek to end
                f.seek(0, 2)
                
                while True:
                    # Check if child is still alive
                    result = os.waitpid(pid, os.WNOHANG)
                    if result[0] != 0:
                        # Child exited
                        print("-" * 60, file=sys.stderr)
                        print(f"[kitty-claude] Child exited with status {result[1]}", file=sys.stderr)
                        break
                    
                    # Read any new lines
                    line = f.readline()
                    if line:
                        sys.stderr.write(line)
                        sys.stderr.flush()
                    else:
                        # No new data, wait a bit
                        time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n[kitty-claude] Interrupted, killing child...", file=sys.stderr)
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        
        sys.exit(0)

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_state_dir():
    """Get the XDG state directory for kitty-claude."""
    xdg_state = os.environ.get('XDG_STATE_HOME')
    if xdg_state:
        state_dir = Path(xdg_state) / "kitty-claude"
    else:
        state_dir = Path.home() / ".local" / "state" / "kitty-claude"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir

def get_claude_binary(profile=None):
    """Get the path to the claude binary from config."""
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    config_file = config_dir / "config.json"
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
            claude_path = config.get("claude_binary")
            if claude_path:
                return claude_path
        except:
            pass
    return "claude"

def set_claude_binary(path, profile=None):
    """Set the path to the claude binary in config."""
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.json"
    config = {}
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
        except:
            pass
    config["claude_binary"] = str(path)
    config_file.write_text(json.dumps(config, indent=2))
    print(f"✓ Set claude binary to: {path}")
    path_obj = Path(path)
    if not path_obj.exists():
        print(f"⚠  Warning: {path} does not exist")
    elif not os.access(path, os.X_OK):
        print(f"⚠  Warning: {path} is not executable")

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

    # Create settings.json with hooks (or add missing hooks to existing file)
    settings_file = claude_data_dir / "settings.json"
    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

    if not settings_file.exists():
        settings_file.write_text(json.dumps({
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{kitty_claude_path} --session-start"
                            }
                        ]
                    }
                ],
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
                ],
                "PreToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{kitty_claude_path} --pre-tool-use"
                            }
                        ]
                    }
                ]
            }
        }, indent=2))
        print(f"Created settings with hooks at {settings_file}")
    else:
        # Add missing hooks
        try:
            settings = json.loads(settings_file.read_text())
            if "hooks" not in settings:
                settings["hooks"] = {}
            modified = False
            if "SessionStart" not in settings["hooks"]:
                settings["hooks"]["SessionStart"] = [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{kitty_claude_path} --session-start"
                            }
                        ]
                    }
                ]
                modified = True
            if "PreToolUse" not in settings["hooks"]:
                settings["hooks"]["PreToolUse"] = [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{kitty_claude_path} --pre-tool-use"
                            }
                        ]
                    }
                ]
                modified = True
            if modified:
                settings_file.write_text(json.dumps(settings, indent=2))
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: Could not update settings.json: {e}")

    # Clean up any expired timed permissions from previous sessions
    cleanup_expired_timed_permissions(claude_data_dir)

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

def save_state():
    """State is maintained automatically by new_window()."""
    profile = os.environ.get('KITTY_CLAUDE_PROFILE')
    state_file = get_runtime_tmux_state_file(profile)
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
    state_file = get_runtime_tmux_state_file(profile)
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
                run(
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
        run(
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

def handle_session_picker(profile, socket="kitty-claude"):
    """Fuzzy find and switch to an open session."""
    open_sessions = get_open_sessions(profile)

    if not open_sessions:
        print("No open sessions.")
        return

    # Build list with session info
    items = []
    state_dir = get_state_dir()

    for session_id in open_sessions:
        name = get_session_name(session_id)

        # Get path from metadata
        metadata_file = state_dir / "sessions" / f"{session_id}.json"
        if metadata_file.exists():
            try:
                metadata = json.loads(metadata_file.read_text())
                path = metadata.get("path", "Unknown")
            except:
                path = "Unknown"
        else:
            path = "Unknown"

        items.append(f"{name} | {path} | {session_id}")

    # Pipe to fzf (works in tmux popup)
    try:
        result = subprocess.run(
            ["fzf", "--height=100%", "--reverse", "--prompt=Switch to session: "],
            input="\n".join(items),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode == 0 and result.stdout.strip():
            # Extract session_id
            selected = result.stdout.strip()
            print(f"Selected: {selected}")

            session_id = selected.split(" | ")[-1]
            print(f"Session ID: {session_id}")

            # Find window with this session
            cmd = ["tmux", "-L", socket, "list-windows", "-F", "#{window_index} #{@session_id}"]
            print(f"Running: {' '.join(cmd)}")

            windows = run(
                cmd,
                capture_output=True,
                text=True,
                profile=profile
            )

            print(f"Windows found:")
            for line in windows.stdout.strip().split("\n"):
                print(f"  {line}")

            for line in windows.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 2 and parts[1] == session_id:
                    print(f"Match! Window {parts[0]} has session {session_id}")
                    switch_cmd = ["tmux", "-L", socket, "select-window", "-t", parts[0]]
                    print(f"Running: {' '.join(switch_cmd)}")
                    run(switch_cmd, profile=profile)
                    print("Done!")
                    return

            print(f"No window found with session {session_id}, opening new window...")
            # Not found? Open new window
            kitty_claude_cmd = ["kitty-claude"]
            if profile:
                kitty_claude_cmd.extend(["--profile", profile])
            kitty_claude_cmd.extend(["--new-window", "--resume-session", session_id])
            print(f"Running: {' '.join(kitty_claude_cmd)}")
            subprocess.Popen(kitty_claude_cmd)
        else:
            print("Cancelled or no selection")

    except FileNotFoundError:
        print("Error: fzf not found. Install: sudo apt install fzf")

def handle_one_tab(config_dir, profile, remain_on_exit=False, no_kitty=False, resume_session_id=None, window_name=None, cwd=None):
    """Launch kitty-claude in single-tab mode.

    Uses tmux but disables new tab creation and skips session restoration.
    Each invocation creates a completely independent instance.

    Args:
        cwd: Working directory override (used when resuming sessions in their original dir)
    """
    import time

    # Unique ID for this instance
    instance_id = f"{int(time.time())}-{os.getpid()}"

    # Put ephemeral kitty config in temp directory (tmux config goes in session dir)
    tmp_config_dir = Path(f"/tmp/kitty-claude-one-tab-{os.getuid()}")
    tmp_config_dir.mkdir(parents=True, exist_ok=True)

    kitty_config_path = tmp_config_dir / f"kitty-{instance_id}.conf"

    # Unique socket and session name for each instance
    if profile:
        tmux_socket = f"kc1-{profile}-{instance_id}"
    else:
        tmux_socket = f"kc1-{instance_id}"

    # Set up isolated Claude config
    claude_data_dir = setup_claude_config(config_dir)

    # Set up working directory - use cwd override if provided, otherwise jail dir
    if cwd and Path(cwd).exists():
        working_dir = cwd
    else:
        working_dir = setup_jail_directory()

    config_dir.mkdir(parents=True, exist_ok=True)

    # Start logging
    log_dir = get_log_dir(profile)
    log_dir.mkdir(exist_ok=True)
    cleanup_old_run_logs(profile, keep=5)

    # Create new run ID
    run_id_file = log_dir / "current-run-id"
    existing_runs = sorted(log_dir.glob("run-*.log"))
    if existing_runs:
        last_num = int(existing_runs[-1].stem.split("-")[1])
        run_num = last_num + 1
    else:
        run_num = 1
    run_id_file.write_text(str(run_num))
    log(f"=== ONE-TAB MODE (run {run_num}, instance {instance_id}) ===", profile)

    remain_config = "set -g remain-on-exit on\n" if remain_on_exit else ""

    # Get claude binary
    claude_bin = get_claude_binary(profile)

    # Set up session config - reuse existing if resuming, create new otherwise
    import uuid
    from kitty_claude.claude import setup_session_config
    if resume_session_id:
        session_id = resume_session_id
        log(f"Resuming session: {session_id}", profile)
    else:
        session_id = str(uuid.uuid4())
    session_config_dir = setup_session_config(session_id, profile)
    log(f"Session config ready for: {session_id}", profile)

    # tmux config goes in session directory
    tmux_config_path = session_config_dir / "tmux.conf"

    # Build the claude command (with --resume if resuming)
    if resume_session_id:
        claude_command = f"{claude_bin} --resume {resume_session_id}"
    else:
        claude_command = claude_bin

    # Register this instance (one-tab launch)
    from kitty_claude.instances import register_instance, ENV_VAR as _IUUID
    instance_uuid = register_instance(tmux_socket, profile, os.getcwd())
    os.environ[_IUUID] = instance_uuid

    # Simplified tmux config - NO C-n, NO session restoration hooks
    tmux_config_path.write_text(f"""\
# kitty-claude tmux config (ONE-TAB MODE)
# No new tabs, no session management
set -g destroy-unattached on
{remain_config}
# Set CLAUDE_CONFIG_DIR for isolated Claude data (session-specific)
set-environment -g CLAUDE_CONFIG_DIR "{session_config_dir}"

# Set tmux socket name so hooks can find it
set-environment -g KITTY_CLAUDE_TMUX_SOCKET "{tmux_socket}"
set-environment -g KITTY_CLAUDE_INSTANCE_UUID "{instance_uuid}"

# Default command is claude
set -g default-command "{claude_command}"

# DISABLED: C-n does nothing in one-tab mode
bind -n C-n display-message "New tabs disabled in --one-tab mode"

# C-w closes window (will exit since it's the only one)
bind -n C-w run-shell "kitty-claude --close-window"

# C-v passthrough for paste
bind -n C-v send-keys C-v

# M-e opens session notes in vim popup
bind -n M-e run-shell "kitty-claude {f'--profile {profile} ' if profile else ''}--notes"

# M-n to rename window and record in title history
bind -n M-n command-prompt -I "#W" -p "Session name:" "rename-window '%%'"

# M-l to reload
bind -n M-l send-keys ':reload' Enter

# C-q: queue a command for when Claude finishes responding
bind -n C-q display-popup -E -w 60% -h 20% "printf 'Queue command (runs when Claude finishes):\\n'; read cmd; echo \\"$cmd\\" >> /run/user/$(id -u)/kc-queue-{tmux_socket}.txt; printf \\"Queued: $cmd\\n\\"; sleep 0.5"

# M-k: show keybindings help
bind -n M-k display-popup -E -w 50% -h 70% "kitty-claude --show-help"

# Some sensible defaults
set -g mouse on
set -g history-limit 10000
set -g base-index 1
setw -g pane-base-index 1

# Quick escape time
set -sg escape-time 0

# Simple status bar (use status-format to avoid conflicts)
set -g status on
set -g status-style bg=colour235,fg=colour248
set -g status-format[0] '#[align=left] #W #[align=right] #{{pane_current_path}} '
set -gu status-format[1]
set -gu status-format[2]
""")
    log(f"Created one-tab tmux config at {tmux_config_path}", profile)

    # Kitty config
    window_name_arg = f" -n '{window_name}'" if window_name else ""
    kitty_config_path.write_text(f"""\
# kitty-claude config (ONE-TAB MODE)
include {Path.home()}/.config/kitty/kitty.conf
shell tmux -L {tmux_socket} -f {tmux_config_path} new-session -As {tmux_socket} -c {working_dir}{window_name_arg}
""")
    log(f"Created one-tab kitty config at {kitty_config_path}", profile)

    # Check dependencies
    if not shutil.which("tmux"):
        print("Error: tmux not found.")
        sys.exit(1)
    if not no_kitty and not shutil.which("kitty"):
        print("Error: kitty not found.")
        sys.exit(1)
    if not shutil.which(claude_bin):
        print(f"Error: claude not found at '{claude_bin}'.")
        print("Please install Claude Code or set path with: kitty-claude --set-claude /path/to/claude")
        sys.exit(1)

    if no_kitty:
        # Launch tmux directly (for testing)
        log(f"Launching tmux directly in one-tab mode (no kitty)", profile)
        tmux_cmd = [
            "tmux", "-L", tmux_socket,
            "-f", str(tmux_config_path),
            "new-session", "-As", tmux_socket,
            "-c", str(jail_dir)
        ]
        if window_name:
            tmux_cmd.extend(["-n", window_name])
        os.execvp("tmux", tmux_cmd)
    else:
        # Launch kitty - NO session restoration, just start fresh
        log(f"Launching kitty in one-tab mode", profile)
        os.execvp("kitty", [
            "kitty",
            "--class=kitty-claude",
            f"--config={kitty_config_path}"
        ])

def handle_list_sessions(profile):
    """List all open sessions with their metadata."""
    open_sessions = get_open_sessions(profile)

    if not open_sessions:
        print("No open sessions found.")
        return

    print(f"\n{'='*80}")
    print(f"Open Sessions ({len(open_sessions)})")
    print(f"{'='*80}\n")

    state_dir = get_state_dir()

    # Get Claude data directory to check for conversation files
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    claude_data_dir = config_dir / "claude-data"
    projects_dir = claude_data_dir / "projects"

    for i, session_id in enumerate(open_sessions, 1):
        # Load metadata
        metadata_file = state_dir / "sessions" / f"{session_id}.json"

        if metadata_file.exists():
            try:
                metadata = json.loads(metadata_file.read_text())
                name = metadata.get("name", "Unknown")
                path = metadata.get("path", "Unknown")
                created = metadata.get("created", "Unknown")

                print(f"{i}. {name}")
                print(f"   Session ID: {session_id}")
                print(f"   Path: {path}")
                print(f"   Created: {created}")
            except Exception as e:
                print(f"{i}. {session_id}")
                print(f"   Error reading metadata: {e}")
        else:
            print(f"{i}. {session_id}")
            print(f"   No metadata found")

        # Look for conversation file
        conv_file = None
        if projects_dir.exists():
            for project_path in projects_dir.iterdir():
                session_file = project_path / f"{session_id}.jsonl"
                if session_file.exists():
                    conv_file = session_file
                    break

        if conv_file:
            print(f"   Conversation: {conv_file} ✓")
        else:
            print(f"   Conversation: Not found ✗")

        print()

    print(f"{'='*80}\n")


# ============================================================================
# COMMAND HANDLERS
# ============================================================================

def handle_copy_profile(source_profile, dest_profile):
    """Copy a profile to a new profile."""
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

def handle_rename(new_name, profile, tmux_socket, window_id=None):
    """Rename a window's session (looks up session ID from state file).

    When invoked from the window-renamed hook, ``window_id`` is the tmux
    window id (e.g. ``@5``) of the window that was actually renamed —
    NOT necessarily the currently focused window. Without it we'd resolve
    via display-message and end up renaming the wrong session.
    """
    log(f"Rename request: new_name={new_name}, profile={profile}, tmux_socket={tmux_socket}, window_id={window_id}", profile)

    # Resolve window_index — by id if the hook gave us one, else current.
    try:
        if window_id:
            cmd = ["tmux", "-L", tmux_socket, "display-message", "-p", "-t", window_id, "#{window_index}"]
        else:
            cmd = ["tmux", "-L", tmux_socket, "display-message", "-p", "#{window_index}"]
        result = run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            profile=profile
        )
        window_index = result.stdout.strip()
        log(f"Got window_index='{window_index}'", profile)
    except Exception as e:
        log(f"Error getting window index: {e}", profile)
        print("Error: Could not get window index from tmux", file=sys.stderr)
        sys.exit(1)

    # Load state file to get session ID
    state_file = get_runtime_tmux_state_file(profile)
    if not state_file.exists():
        log(f"ERROR: State file does not exist: {state_file}", profile)
        print("Error: No state file found", file=sys.stderr)
        sys.exit(1)

    try:
        state = json.loads(state_file.read_text())
        windows = state.get("windows", {})
        window_data = windows.get(window_index)

        if not window_data:
            log(f"ERROR: No window data for index {window_index}", profile)
            print(f"Error: No session data for window {window_index}", file=sys.stderr)
            sys.exit(1)

        session_id = window_data.get("session_id")
        log(f"Got session_id from state: '{session_id}'", profile)

        if not session_id:
            log("ERROR: Session ID is empty in state", profile)
            print("Error: No session ID found for this window", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        log(f"Error reading state file: {e}", profile)
        print(f"Error: Could not read state file: {e}", file=sys.stderr)
        sys.exit(1)

    # Now call the rename logic with the looked-up session ID
    rename_session(session_id, new_name, profile, tmux_socket)

def rename_session(session_id, new_name, profile, tmux_socket):
    """Rename a session by ID."""
    log(f"Rename session handler: session_id={session_id}, new_name={new_name}", profile)

    # Record title in history
    from kitty_claude.colon_command import record_title
    record_title(new_name, profile)

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
    state_file = get_runtime_tmux_state_file(profile)
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

    # NB: do NOT call `tmux rename-window` here. rename_session is invoked
    # from the window-renamed hook, so tmux already has the new name.
    # Calling rename-window would re-fire the hook → infinite loop.

    # Emit title_changed event
    try:
        from kitty_claude.events import emit_event
        emit_event({
            "type": "title_changed",
            "session_id": session_id,
            "name": new_name,
        }, profile)
    except Exception as e:
        log(f"Error emitting title_changed event: {e}", profile)

    sys.exit(0)

def handle_update_config(config_dir, claude_data_dir, profile, kitty_claude_cmd, tmux_socket, remain_on_exit=False):
    """Regenerate configuration files."""
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

    # Get kitty-claude executable for status bar
    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
    profile_arg = f"--profile {profile} " if profile else ""

    # Regenerate tmux config (preserve existing instance uuid from env)
    instance_uuid = os.environ.get("KITTY_CLAUDE_INSTANCE_UUID", "")
    remain_config = "# Keep panes open after command exits (for debugging)\nset -g remain-on-exit on\n" if remain_on_exit else ""
    tmux_config_path.write_text(f"""\
# kitty-claude tmux config (isolated server)
# Kill session when kitty window closes
set -g destroy-unattached on
{remain_config}# Set CLAUDE_CONFIG_DIR for isolated Claude data
set-environment -g CLAUDE_CONFIG_DIR "{claude_data_dir}"

# Set tmux socket name so hooks can find it
set-environment -g KITTY_CLAUDE_TMUX_SOCKET "{tmux_socket}"
set-environment -g KITTY_CLAUDE_INSTANCE_UUID "{instance_uuid}"

# Default command is claude wrapper for session tracking
set -g default-command "{kitty_claude_cmd}"

# Bind C-n directly (no prefix) to open new window with claude in jail
bind -n C-n new-window -c "{jail_dir}" {kitty_claude_cmd}

# Also override default C-b c
bind c new-window -c "{jail_dir}" {kitty_claude_cmd}

# C-w closes current window, but not the last one
bind -n C-w run-shell "kitty-claude --close-window"

# C-v passthrough for paste
bind -n C-v send-keys C-v

# Alt-r to restart kitty-claude
bind -n M-r run-shell "kitty-claude {f'--profile {profile} ' if profile else ''}--restart"

# Alt-l to reload (send :reload to pane)
bind -n M-l send-keys ':reload' Enter

# Alt-e to open session notes
bind -n M-e run-shell "kitty-claude {f'--profile {profile} ' if profile else ''}--notes"

# C-p for session picker (fuzzy find with popup)
bind -n C-p display-popup -E -w 80% -h 60% "kitty-claude {f'--profile {profile} ' if profile else ''}--picker"

# C-q: queue a command for when Claude finishes responding
bind -n C-q display-popup -E -w 60% -h 20% "printf 'Queue command (runs when Claude finishes):\\n'; read cmd; echo \\"$cmd\\" >> /run/user/$(id -u)/kc-queue-{tmux_socket}.txt; printf \\"Queued: $cmd\\n\\"; sleep 0.5"

# M-k: show keybindings help
bind -n M-k display-popup -E -w 50% -h 70% "kitty-claude --show-help"

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
bind -n M-n command-prompt -I "#W" -p "Session name:" "rename-window '%%'"

# 3-line status bar with custom window display
set -g status-interval 5
set -g status 3
set -g status-style bg=colour235,fg=colour248

# Line 0: label (left) and path (right)
set -g status-format[0] '#[bg=colour235,fg=colour248,align=left] [kitty-claude] #[align=right]#{{pane_current_path}} '

# Lines 1 & 2: windows (split across two lines)
set -g status-format[1] '#({kitty_claude_path} {profile_arg}--tmux-status 1)'
set -g status-format[2] '#({kitty_claude_path} {profile_arg}--tmux-status 2)'

# Refresh status bar on window changes
set-hook -g after-select-window 'refresh-client -S'
set-hook -g window-renamed 'run-shell "kitty-claude {f'--profile {profile} ' if profile else ''}--socket {tmux_socket} --window-id #{{hook_window}} --rename \\"#W\\" 2>&1 | tee -a /tmp/kc-rename-hook.log" ; refresh-client -S'

# Window status styling (for reference)
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

def handle_no_kitty(config_dir, profile, kitty_claude_cmd, tmux_socket, remain_on_exit=False):
    """Run tmux directly without kitty (for testing)."""
    # Set up isolated Claude config
    claude_data_dir = setup_claude_config(config_dir)

    # Set up jail directory
    jail_dir = setup_jail_directory()

    # Register this instance (no-kitty launch)
    from kitty_claude.instances import register_instance, ENV_VAR as _IUUID
    instance_uuid = register_instance(tmux_socket, profile, os.getcwd())
    os.environ[_IUUID] = instance_uuid

    # Create tmux config
    tmux_config_path = config_dir / "tmux.conf"
    if not tmux_config_path.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
        remain_config = "# Keep panes open after command exits (for debugging)\nset -g remain-on-exit on\n" if remain_on_exit else ""
        tmux_config_path.write_text(f"""\
# kitty-claude tmux config (isolated server)
# Kill session when kitty window closes
set -g destroy-unattached on
{remain_config}# Set CLAUDE_CONFIG_DIR for isolated Claude data
set-environment -g CLAUDE_CONFIG_DIR "{claude_data_dir}"

# Set tmux socket name so hooks can find it
set-environment -g KITTY_CLAUDE_TMUX_SOCKET "{tmux_socket}"
set-environment -g KITTY_CLAUDE_INSTANCE_UUID "{instance_uuid}"

# Default command is claude wrapper for session tracking
set -g default-command "{kitty_claude_cmd}"

# Bind C-n directly (no prefix) to open new window with claude in jail
bind -n C-n new-window -c "{jail_dir}" {kitty_claude_cmd}

# Also override default C-b c
bind c new-window -c "{jail_dir}" {kitty_claude_cmd}

# Easier window switching
bind -n C-j previous-window
bind -n C-k next-window
bind -n M-o last-window

# Some sensible defaults
set -g mouse on
set -g history-limit 10000
set -g base-index 1
setw -g pane-base-index 1

# C-q: queue a command for when Claude finishes responding
bind -n C-q display-popup -E -w 60% -h 20% "printf 'Queue command (runs when Claude finishes):\\n'; read cmd; echo \\"$cmd\\" >> /run/user/$(id -u)/kc-queue-{tmux_socket}.txt; printf \\"Queued: $cmd\\n\\"; sleep 0.5"

# M-k: show keybindings help
bind -n M-k display-popup -E -w 50% -h 70% "kitty-claude --show-help"

# Bind M-n to prompt for window name and update session metadata
bind -n M-n command-prompt -I "#W" -p "Session name:" "rename-window '%%'"
""")

    # Launch tmux directly
    os.execvp("tmux", ["tmux", "-L", tmux_socket, "-f", str(tmux_config_path),
                       "new-session", "-As", tmux_socket, "-c", str(jail_dir)])

KEYBINDINGS_HELP = """\
\033[1mKitty-Claude Keybindings\033[0m

  C-n    New window
  C-w    Close window
  C-j    Previous window
  C-k    Next window
  C-p    Session picker
  C-q    Queue command
  M-k    This help

  M-r    Restart claude
  M-l    Reload (:reload)
  M-e    Session notes
  M-n    Rename window
  M-o    Last window
"""


def handle_show_help():
    """Print the keybindings help and wait for a keypress.

    Designed to be wrapped in `tmux display-popup -E "kitty-claude --show-help"`
    so the binding list lives in code (testable standalone) rather than
    being baked into the tmux config string.
    """
    print(KEYBINDINGS_HELP)
    print("Press any key to close...")
    try:
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        try:
            sys.stdin.readline()
        except Exception:
            pass


def handle_close_window(profile, tmux_socket):
    """Close the current tmux window, removing its session from open_sessions
    so it isn't restored on next launch. (User pressed C-w.)"""
    from kitty_claude.session import remove_open_session
    # Refuse to close the last window.
    try:
        result = run(
            ["tmux", "-L", tmux_socket, "list-windows", "-F", "#{window_index}"],
            capture_output=True, text=True, check=True, profile=profile,
        )
        if len([l for l in result.stdout.splitlines() if l.strip()]) <= 1:
            run(
                ["tmux", "-L", tmux_socket, "display-message", "Cannot close last window"],
                profile=profile,
            )
            return
    except Exception as e:
        log(f"close-window: list-windows failed: {e}", profile)

    # Look up the session_id for the current window from the runtime state.
    session_id = None
    try:
        result = run(
            ["tmux", "-L", tmux_socket, "display-message", "-p", "#{window_index}"],
            capture_output=True, text=True, check=True, profile=profile,
        )
        window_index = result.stdout.strip()
        state_file = get_runtime_tmux_state_file(profile)
        if state_file.exists():
            state = json.loads(state_file.read_text())
            window_data = state.get("windows", {}).get(window_index) or {}
            session_id = window_data.get("session_id")
    except Exception as e:
        log(f"close-window: could not resolve session_id: {e}", profile)

    if session_id:
        try:
            remove_open_session(session_id, profile)
            log(f"close-window: removed session {session_id} from open_sessions", profile)
        except Exception as e:
            log(f"close-window: remove_open_session failed: {e}", profile)

    try:
        run(["tmux", "-L", tmux_socket, "kill-window"], profile=profile)
    except Exception as e:
        log(f"close-window: kill-window failed: {e}", profile)


def handle_instances(json_output=False):
    """Print running kitty-claude instances."""
    from kitty_claude.instances import list_instances
    instances = list_instances()
    if json_output:
        print(json.dumps(instances, indent=2))
        return
    if not instances:
        print("No running kitty-claude instances.")
        return
    # Table output. Columns chosen to be useful from a terminal.
    headers = ("PID", "SOCKET", "PROFILE", "STARTED", "CWD", "LOG_DIR", "UUID")
    rows = [
        (
            str(e["pid"]),
            e.get("tmux_socket", "") or "",
            e.get("profile") or "-",
            e.get("started_at", "") or "",
            e.get("cwd", "") or "",
            e.get("log_dir", "") or "",
            e["uuid"],
        )
        for e in instances
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for r in rows:
        print(fmt.format(*r))


def launch_kitty_claude(config_dir, profile, kitty_claude_cmd, tmux_socket, remain_on_exit=False):
    """Main launch logic for kitty-claude."""
    kitty_config_path = config_dir / "kitty.conf"
    tmux_config_path = config_dir / "tmux.conf"

    # Set up isolated Claude config
    claude_data_dir = setup_claude_config(config_dir)

    # Set up jail directory
    jail_dir = setup_jail_directory()

    # Create config dir if it doesn't exist
    config_dir.mkdir(parents=True, exist_ok=True)

    # Register this instance so logs/state are routed to a per-uuid dir.
    # This must happen before any log() calls so they land in the right place.
    from kitty_claude.instances import register_instance, ENV_VAR as _IUUID
    instance_uuid = register_instance(tmux_socket, profile, os.getcwd())
    os.environ[_IUUID] = instance_uuid

    # Start a new run (cleanup old logs and create new run ID)
    log_dir = get_log_dir(profile)
    log_dir.mkdir(exist_ok=True)
    cleanup_old_run_logs(profile, keep=5)

    # Create new run ID
    run_id_file = log_dir / "current-run-id"
    existing_runs = sorted(log_dir.glob("run-*.log"))
    if existing_runs:
        last_num = int(existing_runs[-1].stem.split("-")[1])
        run_num = last_num + 1
    else:
        run_num = 1
    run_id_file.write_text(str(run_num))
    log(f"=== NEW RUN {run_num} ===", profile)

    # Start plugin event pipelines
    from kitty_claude.events import start_all_plugins
    start_all_plugins(profile)
    log("Started plugin event pipelines", profile)

    # Remove old config files if they exist (they're read-only)
    if tmux_config_path.exists():
        tmux_config_path.unlink()
    if kitty_config_path.exists():
        kitty_config_path.unlink()

    # Get kitty-claude executable for status bar
    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
    profile_arg = f"--profile {profile} " if profile else ""

    # Always regenerate tmux config (it's ephemeral, not user-editable)
    remain_config = "# Keep panes open after command exits (for debugging)\nset -g remain-on-exit on\n" if remain_on_exit else ""
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
{remain_config}# Set CLAUDE_CONFIG_DIR for isolated Claude data
set-environment -g CLAUDE_CONFIG_DIR "{claude_data_dir}"

# Set tmux socket name so hooks can find it
set-environment -g KITTY_CLAUDE_TMUX_SOCKET "{tmux_socket}"
set-environment -g KITTY_CLAUDE_INSTANCE_UUID "{instance_uuid}"

# Default command is claude wrapper for session tracking
set -g default-command "{kitty_claude_cmd}"

# Bind C-n directly (no prefix) to open new window with claude in jail
bind -n C-n new-window -c "{jail_dir}" {kitty_claude_cmd}

# Also override default C-b c
bind c new-window -c "{jail_dir}" {kitty_claude_cmd}

# C-w closes current window, but not the last one
bind -n C-w run-shell "kitty-claude --close-window"

# C-v passthrough for paste
bind -n C-v send-keys C-v

# Alt-r to restart kitty-claude
bind -n M-r run-shell "kitty-claude {f'--profile {profile} ' if profile else ''}--restart"

# Alt-l to reload (send :reload to pane)
bind -n M-l send-keys ':reload' Enter

# Alt-e to open session notes
bind -n M-e run-shell "kitty-claude {f'--profile {profile} ' if profile else ''}--notes"

# C-p for session picker (fuzzy find) - use popup for interactive fzf
bind -n C-p display-popup -E -w 80% -h 60% "kitty-claude {f'--profile {profile} ' if profile else ''}--picker"

# C-q: queue a command for when Claude finishes responding
bind -n C-q display-popup -E -w 60% -h 20% "printf 'Queue command (runs when Claude finishes):\\n'; read cmd; echo \\"$cmd\\" >> /run/user/$(id -u)/kc-queue-{tmux_socket}.txt; printf \\"Queued: $cmd\\n\\"; sleep 0.5"

# M-k: show keybindings help
bind -n M-k display-popup -E -w 50% -h 70% "kitty-claude --show-help"

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
bind -n M-n command-prompt -I "#W" -p "Session name:" "rename-window '%%'"

# 3-line status bar with custom window display
set -g status-interval 5
set -g status 3
set -g status-style bg=colour235,fg=colour248

# Line 0: label (left) and path (right)
set -g status-format[0] '#[bg=colour235,fg=colour248,align=left] [kitty-claude] #[align=right]#{{pane_current_path}} '

# Lines 1 & 2: windows (split across two lines)
set -g status-format[1] '#({kitty_claude_path} {profile_arg}--tmux-status 1)'
set -g status-format[2] '#({kitty_claude_path} {profile_arg}--tmux-status 2)'

# Refresh status bar on window changes
set-hook -g after-select-window 'refresh-client -S'
set-hook -g window-renamed 'run-shell "kitty-claude {f'--profile {profile} ' if profile else ''}--socket {tmux_socket} --window-id #{{hook_window}} --rename \\"#W\\" 2>&1 | tee -a /tmp/kc-rename-hook.log" ; refresh-client -S'

# Window status styling (for reference)
set -g window-status-style bg=colour235,fg=colour248
set -g window-status-current-style bg=colour39,fg=colour235,bold
set -g window-status-format " #I:#W "
set -g window-status-current-format " #I:#W "
""")
    print(f"Created tmux config at {tmux_config_path}")

    # Always regenerate kitty config (it's ephemeral, not user-editable)
    kitty_config_path.write_text(f"""\
# ============================================================================
# DO NOT MODIFY THIS FILE - IT IS AUTO-GENERATED ON EVERY LAUNCH
# ============================================================================
include {Path.home()}/.config/kitty/kitty.conf
shell tmux -L {tmux_socket} -f {tmux_config_path} new-session -As {tmux_socket} -c {jail_dir} {kitty_claude_cmd}
""")
    print(f"Created kitty config at {kitty_config_path}")

    # Check if tmux session exists
    try:
        result = run(
            ["tmux", "-L", tmux_socket, "has-session", "-t", tmux_socket],
            capture_output=True,
            text=True,
            profile=profile
        )
        session_exists = (result.returncode == 0)
        log(f"Tmux session exists: {session_exists}", profile)
    except Exception as e:
        session_exists = False
        log(f"Error checking tmux session: {e}", profile)

    # If session doesn't exist, restore open sessions
    if not session_exists:
        log("Session doesn't exist, restoring open sessions", profile)
        open_sessions = get_open_sessions(profile)
        log(f"Restore: Found {len(open_sessions)} open sessions: {open_sessions}", profile)

        if open_sessions:
            # Start tmux session with first session
            first_session_id = open_sessions[0]
            log(f"Restore: Creating initial session with {first_session_id}", profile)

            # Create first window via kitty-claude --new-window so the
            # has_messages / blank-session decision goes through new_window
            # rather than running `claude --resume` directly here.
            kc_cmd_parts = [kitty_claude_path]
            if profile:
                kc_cmd_parts.extend(["--profile", profile])
            kc_cmd_parts.extend(["--new-window", "--resume-session", first_session_id])
            result = run(
                ["tmux", "-L", tmux_socket, "-f", str(tmux_config_path),
                 "new-session", "-d", "-s", tmux_socket, "-c", str(jail_dir),
                 *kc_cmd_parts],
                capture_output=True,
                text=True,
                profile=profile
            )

            # Check if session was created successfully
            if result.returncode != 0:
                log(f"Restore: Failed to create initial session, aborting restore", profile)
            else:
                # Wait for session to be ready (poll with timeout)
                import time
                session_ready = False
                max_attempts = 60  # 6 seconds total
                for attempt in range(max_attempts):
                    check_result = run(
                        ["tmux", "-L", tmux_socket, "has-session", "-t", tmux_socket],
                        capture_output=True,
                        text=True,
                        profile=profile
                    )
                    if check_result.returncode == 0:
                        session_ready = True
                        log(f"Restore: Session ready after {attempt * 100}ms", profile)
                        break
                    time.sleep(0.1)

                if not session_ready:
                    log(f"Restore: Session died or never started after {max_attempts * 100}ms, skipping restore", profile)
                else:
                    # Restore remaining sessions
                    for sess_id in open_sessions[1:]:
                        win_name = get_session_name(sess_id)
                        state_dir = get_state_dir()
                        metadata_file = state_dir / "sessions" / f"{sess_id}.json"
                        if metadata_file.exists():
                            try:
                                metadata = json.loads(metadata_file.read_text())
                                path = metadata.get("path", str(jail_dir))
                            except:
                                path = str(jail_dir)
                        else:
                            path = str(jail_dir)

                        log(f"Restore: Creating window for session {sess_id} at {path}", profile)

                        # Build wrapper command (FIXED - use indirection)
                        cmd_parts = [kitty_claude_path]
                        if profile:
                            cmd_parts.extend(["--profile", profile])
                        cmd_parts.extend(["--new-window", "--resume-session", sess_id])
                        cmd_str = " ".join(cmd_parts)

                        run(
                            ["tmux", "-L", tmux_socket, "new-window", "-t", tmux_socket,
                             "-c", path, "-n", win_name, cmd_str],
                            capture_output=True,
                            text=True,
                            profile=profile
                        )
        else:
            log("Restore: No open sessions to restore", profile)

    # Launch kitty
    os.execvp("kitty", [
        "kitty",
        "--class=kitty-claude",
        f"--config={kitty_config_path}"
    ])


# ============================================================================
# MAIN FUNCTION
# ============================================================================

def main():
    try:
        parser = argparse.ArgumentParser(description="Launch Claude Code in isolated kitty+tmux environment")
        parser.add_argument("--reinstall", action="store_true", help="Remove all config except credentials and exit")
        parser.add_argument("--session-start", action="store_true", help="Handle SessionStart hook (internal use)")
        parser.add_argument("--user-prompt-submit", action="store_true", help="Handle UserPromptSubmit hook (internal use)")
        parser.add_argument("--stop", action="store_true", help="Handle Stop hook (internal use)")
        parser.add_argument("--pre-tool-use", action="store_true", help="Handle PreToolUse hook (internal use)")
        parser.add_argument("--new-window", action="store_true", help="Create new window with session tracking (internal use)")
        parser.add_argument("--resume-session", type=str, metavar="SESSION_ID", help="Resume specific session in new window (internal use)")
        parser.add_argument("--cwd", type=str, metavar="PATH", help="Working directory for resumed session (internal use)")
        parser.add_argument("--restart", action="store_true", help="Restart kitty-claude with state preservation")
        parser.add_argument("--update-config", action="store_true", help="Regenerate tmux and kitty config files")
        parser.add_argument("--instances", action="store_true", help="List running kitty-claude instances")
        parser.add_argument("--close-window", action="store_true", help="Close the current tmux window and remove its session from the restore list")
        parser.add_argument("--show-help", action="store_true", help="Print the keybindings help (used by the M-k popup)")
        parser.add_argument("--json", action="store_true", help="Output JSON instead of a table (used with --instances)")
        parser.add_argument("--force-new", action="store_true", help="Launch new kitty window regardless of existing windows")
        parser.add_argument("--rename-session", nargs=2, metavar=("SESSION_ID", "NAME"), help="Rename session (internal use)")
        parser.add_argument("--rename", type=str, metavar="NAME", help="Mirror a tmux window rename into kitty-claude state (called from window-renamed hook)")
        parser.add_argument("--window-id", type=str, metavar="ID", help="tmux window id (e.g. @5), passed by the window-renamed hook so --rename targets the correct window rather than whichever is focused")
        parser.add_argument("--socket", type=str, metavar="SOCKET", help="Tmux socket name (for --rename etc)")
        parser.add_argument("--no-kitty", action="store_true", help="Run tmux directly without kitty (for testing)")
        parser.add_argument("--notes", action="store_true", help="Open session notes in vim popup")
        parser.add_argument("--profile", type=str, help="Use specific profile (required for non-internal commands)")
        parser.add_argument("--copy-profile", nargs=2, metavar=("SOURCE", "DEST"), help="Copy profile SOURCE to DEST")
        parser.add_argument("--follow-logs", action="store_true", help="Follow log file for current profile")
        parser.add_argument("--last-logs", action="store_true", help="Show all logs from last run")
        parser.add_argument("--remain", action="store_true", help="Keep panes open after command exits (for debugging)")
        parser.add_argument("--tmux-status", type=int, metavar="LINE", choices=[1, 2], help="Display tmux status line (1 or 2) - internal use")
        parser.add_argument("--list-sessions", action="store_true", help="List all open sessions with metadata")
        parser.add_argument("--picker", action="store_true", help="Fuzzy find and switch to a session (internal use)")
        parser.add_argument("--one-tab", action="store_true", help="Single-tab mode - no session restoration, no new tabs")
        parser.add_argument("--window-name", type=str, metavar="NAME", help="Set tmux window name (used with --one-tab)")
        parser.add_argument("--log", action="store_true", help="Stream logs to stderr while running")
        parser.add_argument("--add-rules", nargs='+', metavar="NAME [FILE]", help="Add a rule: --add-rules NAME (reads stdin) or --add-rules NAME FILE")
        parser.add_argument("--list-rules", action="store_true", help="List all rules")
        parser.add_argument("--show-rule", metavar="NAME", help="Show content of a specific rule")
        parser.add_argument("--set-claude", metavar="PATH", help="Set path to claude binary")
        parser.add_argument("--mcp-exec", nargs=argparse.REMAINDER, help="Run mcp-exec with given arguments (internal use)")
        parser.add_argument("--plan-mcp", action="store_true", help="Run planning MCP server (provides session/notes overview)")
        parser.add_argument("--command-mcp", action="store_true", help="Run command MCP server (exposes colon commands to Claude)")
        parser.add_argument("--skills-mcp", action="store_true", help="Run skills MCP server (lets Claude create kc-skills)")
        parser.add_argument("--claude-skills-mcp", action="store_true", help="Run Claude Code skills MCP server (lets Claude manage /skills)")
        parser.add_argument("--with-commands", action="store_true", help="Enable kitty_command tool in command MCP server")
        parser.add_argument("--run-command", type=str, metavar="COMMAND", help="Run a colon command directly (e.g. ':tmuxpath')")
        parser.add_argument("--proxy-mcp", type=str, metavar="MCPDEF_JSON", help="Run MCP proxy with tmux approval (internal use)")
        parser.add_argument("--permissions-gui", type=str, metavar="SESSION_ID", help="Open permissions editor GUI (internal use)")
        parser.add_argument("--events", action="store_true", help="Tail events log to stdout (blocks, uses inotify)")
        parser.add_argument("--events-since", type=float, metavar="TIMESTAMP", help="With --events, replay from this unix timestamp")
        parser.add_argument("--set-title", nargs=2, metavar=("SESSION_ID", "NAME"), help="Set session title (updates metadata, tmux, emits event)")
        parser.add_argument("--send-login", nargs=2, metavar=("SOCKET", "WINDOW"), help="Test send :login to a kc1 socket/window (debug)")

        args = parser.parse_args()

        # Enable stderr logging if --log flag is set
        if args.log:
            os.environ['KITTY_CLAUDE_LOG_STDERR'] = '1'

        if args.instances:
            handle_instances(json_output=args.json)
            sys.exit(0)

        if args.show_help:
            handle_show_help()
            sys.exit(0)

        # Determine profile name
        profile = args.profile or os.environ.get('KITTY_CLAUDE_PROFILE')

        # Log this invocation
        log(f"=== COMMAND: {' '.join(sys.argv)} ===", profile)

        # Set up directories based on profile
        if profile:
            config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
            tmux_socket = f"kitty-claude-{profile}"
            kitty_claude_cmd = f"kitty-claude --profile {profile} --new-window"
        else:
            config_dir = Path.home() / ".config" / "kitty-claude"
            tmux_socket = "kitty-claude"
            kitty_claude_cmd = "kitty-claude --new-window"

        # Override socket if explicitly provided
        if args.socket:
            tmux_socket = args.socket

        claude_data_dir = config_dir / "claude-data"

        # Dispatch to command handlers
        if args.send_login:
            import subprocess
            import time
            socket, window = args.send_login
            print(f"Sending hello + Enter to {socket} window {window}", file=sys.stderr)

            cmd1 = ["tmux", "-L", socket, "send-keys", "-t", window, "-l", "hello"]
            print(f"CMD: {' '.join(cmd1)}", file=sys.stderr)
            r1 = subprocess.run(cmd1)
            print(f"  rc={r1.returncode}", file=sys.stderr)

            time.sleep(1.0)

            cmd2 = ["tmux", "-L", socket, "send-keys", "-t", window, "Enter"]
            print(f"CMD: {' '.join(cmd2)}", file=sys.stderr)
            r2 = subprocess.run(cmd2)
            print(f"  rc={r2.returncode}", file=sys.stderr)

            sys.exit(0)

        if args.set_claude:
            set_claude_binary(args.set_claude, profile)
            sys.exit(0)

        if args.add_rules:
            # Parse arguments: NAME or NAME FILE
            if len(args.add_rules) == 1:
                # Read from stdin
                rule_name = args.add_rules[0]
                rule_content = sys.stdin.read()
            elif len(args.add_rules) == 2:
                # Read from file
                rule_name = args.add_rules[0]
                rule_file_path = Path(args.add_rules[1])
                if not rule_file_path.exists():
                    print(f"Error: File not found: {rule_file_path}", file=sys.stderr)
                    sys.exit(1)
                rule_content = rule_file_path.read_text()
            else:
                print("Error: --add-rules takes 1 or 2 arguments: NAME or NAME FILE", file=sys.stderr)
                sys.exit(1)

            # Save the rule
            save_rule(rule_name, rule_content, profile)

            # Rebuild CLAUDE.md
            build_claude_md(profile)
            sys.exit(0)

        if args.list_rules:
            rules = list_rules(profile)
            if not rules:
                print("No rules found.")
            else:
                print("Rules:")
                for rule in rules:
                    print(f"  {rule}")
            sys.exit(0)

        if args.show_rule:
            content = show_rule(args.show_rule, profile)
            if content is None:
                print(f"Error: Rule not found: {args.show_rule}", file=sys.stderr)
                sys.exit(1)
            print(content)
            sys.exit(0)

        if args.picker:
            handle_session_picker(profile, tmux_socket)
            sys.exit(0)

        if args.one_tab:
            if args.log:
                fork_with_log_tailing(
                    lambda: handle_one_tab(config_dir, profile, args.remain, args.no_kitty, args.resume_session, args.window_name, args.cwd),
                    profile
                )
            else:
                handle_one_tab(config_dir, profile, args.remain, args.no_kitty, args.resume_session, args.window_name, args.cwd)
            # execvp doesn't return
            sys.exit(0)

        if args.list_sessions:
            handle_list_sessions(profile)
            sys.exit(0)

        if args.tmux_status:
            handle_tmux_status(args.tmux_status, profile)
            sys.exit(0)

        if args.last_logs:
            handle_last_logs(profile)

        if args.follow_logs:
            handle_follow_logs(profile)

        if args.copy_profile:
            source_profile, dest_profile = args.copy_profile
            handle_copy_profile(source_profile, dest_profile)

        if args.mcp_exec:
            # Run mcp-exec with the provided arguments
            from kitty_claude.mcp_exec.__main__ import main as mcp_exec_main
            sys.argv = ['mcp-exec'] + args.mcp_exec
            mcp_exec_main()
            sys.exit(0)

        if args.plan_mcp:
            # Run planning MCP server
            from kitty_claude.plan_mcp_server import main as plan_mcp_main
            plan_mcp_main()
            sys.exit(0)

        if args.command_mcp:
            # Run command MCP server
            from kitty_claude.command_mcp_server import main as command_mcp_main
            command_mcp_main(enable_commands=args.with_commands)
            sys.exit(0)

        if args.skills_mcp:
            # Run skills MCP server
            from kitty_claude.skills_mcp_server import main as skills_mcp_main
            skills_mcp_main()
            sys.exit(0)

        if args.claude_skills_mcp:
            # Run Claude Code skills MCP server
            from kitty_claude.claude_skills_mcp_server import main as claude_skills_mcp_main
            claude_skills_mcp_main()
            sys.exit(0)

        if args.proxy_mcp:
            # Run MCP proxy with approval popups
            from kitty_claude.proxy_mcp_server import main as proxy_mcp_main
            proxy_mcp_main()
            sys.exit(0)

        if args.permissions_gui:
            session_id = args.permissions_gui
            session_config_dir = config_dir / "session-configs" / session_id
            cwd_file = get_state_dir() / "sessions" / f"{session_id}.json"
            cwd = "."
            if cwd_file.exists():
                try:
                    meta = json.loads(cwd_file.read_text())
                    cwd = meta.get("path", ".")
                except:
                    pass
            roles_dir = config_dir / "mcp-roles"
            from kitty_claude.permissions_gui import run_gui
            run_gui(str(session_config_dir), cwd, roles_dir, config_dir=str(config_dir), session_id=session_id)
            sys.exit(0)

        if args.events:
            from kitty_claude.events import subscribe_events
            sys.exit(subscribe_events(profile, since=args.events_since))

        if args.set_title:
            session_id, name = args.set_title
            from kitty_claude.events import set_title
            set_title(session_id, name, profile)
            print(f"Title set: {session_id} -> {name}")
            sys.exit(0)

        if args.run_command:
            handle_run_command(args.run_command)
            sys.exit(0)

        if args.notes:
            open_session_notes(get_runtime_tmux_state_file)
            sys.exit(0)

        if args.session_start:
            handle_session_start()
            sys.exit(0)

        if args.user_prompt_submit:
            handle_user_prompt_submit()
            sys.exit(0)

        if args.stop:
            handle_stop()
            sys.exit(0)

        if args.pre_tool_use:
            handle_pre_tool_use()
            sys.exit(0)

        if args.new_window:
            new_window(profile=profile, resume_session_id=args.resume_session, socket=tmux_socket)
            sys.exit(0)

        if args.restart:
            restart()
            sys.exit(0)

        if args.close_window:
            handle_close_window(profile, tmux_socket)
            sys.exit(0)

        if args.rename:
            handle_rename(args.rename, profile, tmux_socket, window_id=args.window_id)

        if args.rename_session:
            session_id, new_name = args.rename_session
            rename_session(session_id, new_name, profile, tmux_socket)

        if args.update_config:
            handle_update_config(config_dir, claude_data_dir, profile, kitty_claude_cmd, tmux_socket, args.remain)

        if args.reinstall:
            reinstall(config_dir)
            sys.exit(0)

        # Check dependencies
        if not shutil.which("tmux"):
            print("Error: tmux not found. Please install tmux first.")
            sys.exit(1)

        if not shutil.which("kitty"):
            print("Error: kitty not found. Please install kitty first.")
            sys.exit(1)

        claude_bin = get_claude_binary(profile)
        if not shutil.which(claude_bin):
            print(f"Error: claude not found at '{claude_bin}'.")
            print("Please install Claude Code or set path with: kitty-claude --set-claude /path/to/claude")
            sys.exit(1)

        if args.no_kitty:
            if args.log:
                fork_with_log_tailing(
                    lambda: handle_no_kitty(config_dir, profile, kitty_claude_cmd, tmux_socket, args.remain),
                    profile
                )
            else:
                handle_no_kitty(config_dir, profile, kitty_claude_cmd, tmux_socket, args.remain)

        # Default: launch kitty-claude
        if args.log:
            fork_with_log_tailing(
                lambda: launch_kitty_claude(config_dir, profile, kitty_claude_cmd, tmux_socket, args.remain),
                profile
            )
        else:
            launch_kitty_claude(config_dir, profile, kitty_claude_cmd, tmux_socket, args.remain)

    except Exception as e:
        # Log any uncaught exceptions
        profile = os.environ.get('KITTY_CLAUDE_PROFILE')
        log(f"FATAL ERROR: {e}", profile)
        import traceback
        log(f"TRACEBACK:\n{traceback.format_exc()}", profile)
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()