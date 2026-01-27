#!/usr/bin/env python3
"""Claude-specific operations for kitty-claude."""
import os
import sys
import json
import uuid
import shutil
import subprocess
from pathlib import Path
from kitty_claude.logging import log, run
from kitty_claude.session import (
    get_session_name,
    add_open_session,
    get_state_dir,
    save_session_metadata,
    get_open_sessions,
    remove_open_session
)
from kitty_claude.tmux import get_runtime_tmux_state_file
from kitty_claude.rules import build_claude_md


def setup_session_config(session_id, profile=None):
    """Create a unique config directory for this session with shared projects.

    This allows each session to have its own settings.json (with different MCP servers)
    while sharing conversation history through a symlinked projects directory.

    Args:
        session_id: Unique session identifier
        profile: Profile name (optional)

    Returns:
        Path to the session-specific claude-data directory
    """
    # Get base config directory
    if profile:
        base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "kitty-claude"

    # Canonical shared projects location
    canonical_projects = base_config / "claude-data" / "projects"
    canonical_projects.mkdir(parents=True, exist_ok=True)

    # Create session-specific config directory
    session_configs = base_config / "session-configs"
    session_configs.mkdir(parents=True, exist_ok=True)

    session_config_dir = session_configs / session_id
    session_config_dir.mkdir(parents=True, exist_ok=True)

    # Copy settings.json from canonical location if it exists
    canonical_settings = base_config / "claude-data" / "settings.json"
    session_settings = session_config_dir / "settings.json"

    if canonical_settings.exists() and not session_settings.exists():
        shutil.copy2(canonical_settings, session_settings)
        log(f"Copied settings from {canonical_settings}", profile)
    elif not session_settings.exists():
        # Create minimal settings file
        session_settings.write_text('{"model": "sonnet"}\n')
        log(f"Created default settings.json", profile)

    # Symlink projects directory to canonical location
    session_projects = session_config_dir / "projects"
    if not session_projects.exists():
        session_projects.symlink_to(canonical_projects)
        log(f"Symlinked projects: {session_projects} -> {canonical_projects}", profile)

    log(f"Session config ready: {session_config_dir}", profile)
    return session_config_dir


def cleanup_session_config(session_id, profile=None):
    """Remove session-specific config directory after session ends.

    Args:
        session_id: Unique session identifier
        profile: Profile name (optional)
    """
    # Get base config directory
    if profile:
        base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "kitty-claude"

    session_config_dir = base_config / "session-configs" / session_id

    if session_config_dir.exists():
        try:
            # Remove symlink and directory
            shutil.rmtree(session_config_dir)
            log(f"Cleaned up session config: {session_config_dir}", profile)
        except Exception as e:
            log(f"Error cleaning up session config: {e}", profile)

def new_window(profile=None, resume_session_id=None, socket="kitty-claude"):
    """Create a new Claude window with session tracking.
    
    Args:
        profile: Profile name (optional)
        resume_session_id: Optional session ID to resume instead of creating new
        socket: Tmux socket name (optional, defaults to "kitty-claude")
    """
    log(f"new_window called: profile={profile}, resume_session_id={resume_session_id}, socket={socket}", profile)
    
    state_file = get_runtime_tmux_state_file(profile)
    
    # Get current window index
    try:
        result = run(
            ["tmux", "-L", socket, "display-message", "-p", "#{window_index}"],
            capture_output=True,
            text=True,
            check=True,
            profile=profile
        )
        window_index = result.stdout.strip()
        log(f"Current window index: {window_index}", profile)
    except Exception as e:
        window_index = "unknown"
        log(f"Error getting window index: {e}", profile)
    
    # If this is the first window (index 1, since base-index is 1), restore open sessions
    if window_index == "1":
        log("Window index is 1, checking for sessions to restore", profile)
        try:
            open_sessions = get_open_sessions(profile)
            log(f"Restore: Found {len(open_sessions)} open sessions: {open_sessions}", profile)
            
            if open_sessions:
                # Get jail directory
                uid = os.getuid()
                jail_dir = Path(f"/var/run/{uid}/kitty-claude")
                if not jail_dir.exists():
                    jail_dir = Path(f"/tmp/kitty-claude-{uid}")
                
                # Get kitty-claude command
                kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
                
                # Restore all open sessions except the first one (we're in window 1)
                log(f"Restore: Restoring {len(open_sessions[1:])} sessions (skipping first)", profile)
                for sess_id in open_sessions[1:]:
                    win_name = get_session_name(sess_id)
                    
                    # Get path from session metadata if available
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
                    
                    # Create window using kitty-claude indirection
                    log(f"Restore: Creating window for session {sess_id} at {path}", profile)
                    
                    # Build command string
                    cmd_parts = [kitty_claude_path]
                    if profile:
                        cmd_parts.extend(["--profile", profile])
                    cmd_parts.extend(["--new-window", "--resume-session", sess_id])
                    cmd_str = " ".join(cmd_parts)
                    
                    log(f"Restore: Running command: tmux new-window -c {path} -n {win_name} {cmd_str}", profile)
                    
                    run(
                        ["tmux", "-L", socket, "new-window", "-c", path, "-n", win_name, cmd_str],
                        stderr=subprocess.DEVNULL,
                        profile=profile
                    )
            else:
                log("Restore: No open sessions to restore", profile)
        except Exception as e:
            log(f"Restore error: {e}", profile)
            print(f"Warning: Could not restore sessions: {e}", file=sys.stderr)
    
    # Get current path before any changes
    original_cwd = os.getcwd()
    log(f"Original working directory: {original_cwd}", profile)
    
    # If resuming, change to the session's original directory
    if resume_session_id:
        session_id = resume_session_id
        state_dir = get_state_dir()
        metadata_file = state_dir / "sessions" / f"{resume_session_id}.json"
        
        if metadata_file.exists():
            try:
                metadata = json.loads(metadata_file.read_text())
                session_path = metadata.get("path", original_cwd)
                log(f"Session {resume_session_id} was created in: {session_path}", profile)
                
                # ACTUALLY CHANGE TO THAT DIRECTORY
                os.chdir(session_path)
                log(f"Changed working directory to: {os.getcwd()}", profile)
            except Exception as e:
                log(f"Error reading session path or changing directory: {e}", profile)
    else:
        # Generate new session ID
        session_id = str(uuid.uuid4())
    
    # Get current path (after potentially changing directory)
    current_path = os.getcwd()
    log(f"Current working directory when launching Claude: {current_path}", profile)
    
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
        run(
            ["tmux", "-L", socket, "rename-window", default_name],
            stderr=subprocess.DEVNULL,
            profile=profile
        )
        run(
            ["tmux", "-L", socket, "set-option", "-w", f"@session_id", session_id],
            stderr=subprocess.DEVNULL,
            profile=profile
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
    
    # Add to open sessions list
    add_open_session(session_id, profile)

    # Build CLAUDE.md from rules before launching
    build_claude_md(profile)

    # Set up session-specific config directory with shared projects
    session_config_dir = setup_session_config(session_id, profile)

    # Launch claude and wait for it to exit
    if resume_session_id:
        cmd = ["claude", "--resume", session_id]
    else:
        cmd = ["claude", "--session-id", session_id]

    log(f"Starting claude: {' '.join(cmd)}", profile)

    try:
        # Use run() wrapper and override CLAUDE_CONFIG_DIR for this session
        env = os.environ.copy()
        env['CLAUDE_CONFIG_DIR'] = str(session_config_dir)
        result = run(cmd, stderr=subprocess.PIPE, text=True, env=env, profile=profile)
        
        # Log the exit
        log(f"Claude exited with code {result.returncode} for session {session_id}", profile)
        
        # Log stderr if present (errors/warnings)
        if result.stderr and result.stderr.strip():
            log(f"Claude stderr: {result.stderr.strip()}", profile)
        
        # If claude exited cleanly (exit code 0), remove from open sessions and cleanup config
        if result.returncode == 0:
            log(f"Clean exit - removing session {session_id} from open sessions", profile)
            remove_open_session(session_id, profile)
            cleanup_session_config(session_id, profile)
        else:
            log(f"Non-zero exit code {result.returncode} - keeping session {session_id} in open sessions", profile)
            # Don't cleanup config on error in case user wants to debug
            
    except KeyboardInterrupt:
        log(f"Claude interrupted (Ctrl+C) for session {session_id} - keeping in open sessions", profile)
    except Exception as e:
        log(f"Error running claude for session {session_id}: {e}", profile)