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

from kitty_claude.logging import log, get_log_dir, get_run_log_file, cleanup_old_run_logs, run
from kitty_claude.logs import handle_last_logs, handle_follow_logs
from kitty_claude.window_utils import (
    find_and_focus_window,
    open_session_notes
)
from kitty_claude.tmux import (
    send_tmux_message,
    new_window,
    get_runtime_tmux_state_file
)
from kitty_claude.colon_command import (
    handle_user_prompt_submit,
    handle_stop
)
from kitty_claude.session import (
    save_session_metadata,
    get_session_name,
    get_open_sessions_file,
    add_open_session,
    remove_open_session,
    get_open_sessions
)

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

def handle_rename(new_name, profile, tmux_socket):
    """Rename current window's session (looks up session ID from state file)."""
    log(f"Rename request: new_name={new_name}, profile={profile}, tmux_socket={tmux_socket}", profile)
    
    # Get current window index
    try:
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
    
    # Rename current tmux window
    try:
        cmd = ["tmux", "-L", tmux_socket, "rename-window", new_name]
        result = run(cmd, capture_output=True, text=True, check=True, profile=profile)
        log(f"Rename successful", profile)
    except Exception as e:
        log(f"Error renaming window: {e}", profile)
    
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
    
    # Regenerate tmux config
    remain_config = "# Keep panes open after command exits (for debugging)\nset -g remain-on-exit on\n" if remain_on_exit else ""
    tmux_config_path.write_text(f"""\
# kitty-claude tmux config (isolated server)

# Kill session when kitty window closes
set -g destroy-unattached on

{remain_config}# Set CLAUDE_CONFIG_DIR for isolated Claude data
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
bind -n M-r run-shell "kitty-claude {f'--profile {profile} ' if profile else ''}--restart"

# Alt-e to open session notes
bind -n M-e run-shell "kitty-claude {f'--profile {profile} ' if profile else ''}--notes"

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
bind -n M-n command-prompt -I "#W" -p "Session name:" "run-shell 'kitty-claude {f'--profile {profile} ' if profile else ''}--rename \\"%%\\"'"

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

def handle_no_kitty(config_dir, profile, kitty_claude_cmd, tmux_socket, remain_on_exit=False):
    """Run tmux directly without kitty (for testing)."""
    # Set up isolated Claude config
    claude_data_dir = setup_claude_config(config_dir)
    
    # Set up jail directory
    jail_dir = setup_jail_directory()
    
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

# Bind M-n to prompt for window name and update session metadata
bind -n M-n command-prompt -I "#W" -p "Session name:" "run-shell 'kitty-claude {f'--profile {profile} ' if profile else ''}--rename \\"%%\\"'"
""")
    
    # Launch tmux directly
    os.execvp("tmux", ["tmux", "-L", tmux_socket, "-f", str(tmux_config_path),
                       "new-session", "-As", tmux_socket, "-c", str(jail_dir)])

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
    
    # Remove old config files if they exist (they're read-only)
    if tmux_config_path.exists():
        tmux_config_path.unlink()
    if kitty_config_path.exists():
        kitty_config_path.unlink()
    
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
bind -n M-r run-shell "kitty-claude {f'--profile {profile} ' if profile else ''}--restart"

# Alt-e to open session notes
bind -n M-e run-shell "kitty-claude {f'--profile {profile} ' if profile else ''}--notes"

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
bind -n M-n command-prompt -I "#W" -p "Session name:" "run-shell 'kitty-claude {f'--profile {profile} ' if profile else ''}--rename \\"%%\\"'"

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
            
            # Create first window
            result = run(
                ["tmux", "-L", tmux_socket, "-f", str(tmux_config_path),
                 "new-session", "-d", "-s", tmux_socket, "-c", str(jail_dir),
                 "claude", "--resume", first_session_id],
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
                        run(
                            ["tmux", "-L", tmux_socket, "new-window", "-t", tmux_socket,
                             "-c", path, "-n", win_name, "claude", "--resume", sess_id],
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
        parser.add_argument("--last-logs", action="store_true", help="Show all logs from last run")
        parser.add_argument("--remain", action="store_true", help="Keep panes open after command exits (for debugging)")
        
        args = parser.parse_args()
        
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
        
        claude_data_dir = config_dir / "claude-data"
        
        # Dispatch to command handlers
        if args.last_logs:
            handle_last_logs(profile)
        
        if args.follow_logs:
            handle_follow_logs(profile)
        
        if args.copy_profile:
            source_profile, dest_profile = args.copy_profile
            handle_copy_profile(source_profile, dest_profile)
        
        if args.notes:
            open_session_notes(get_runtime_tmux_state_file)
            sys.exit(0)
        
        if args.user_prompt_submit:
            handle_user_prompt_submit()
            sys.exit(0)
        
        if args.stop:
            handle_stop()
            sys.exit(0)
        
        if args.new_window:
            new_window(profile=profile, resume_session_id=args.resume_session, socket=tmux_socket)
            sys.exit(0)
        
        if args.restart:
            restart()
            sys.exit(0)
        
        if args.rename:
            handle_rename(args.rename, profile, tmux_socket)
        
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
        
        if not shutil.which("claude"):
            print("Error: claude not found. Please install Claude Code first.")
            sys.exit(1)
        
        if args.no_kitty:
            handle_no_kitty(config_dir, profile, kitty_claude_cmd, tmux_socket, args.remain)
        
        # Default: launch kitty-claude
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