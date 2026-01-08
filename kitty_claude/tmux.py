#!/usr/bin/env python3
"""Tmux operations for kitty-claude."""
import os
import sys
import json
import uuid
import shutil
import subprocess
from pathlib import Path
from kitty_claude.logging import log, run
from kitty_claude.session import get_session_name, add_open_session, get_state_dir, save_session_metadata, get_open_sessions

def send_tmux_message(message, socket="kitty-claude"):
    """Send a message via tmux display-message"""
    try:
        run([
            "tmux", "-L", socket,
            "display-message", message
        ], stderr=subprocess.DEVNULL)
    except:
        pass

def get_runtime_tmux_state_file(profile=None):
    """Get the runtime tmux state file path (for window restoration)."""
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
        return runtime_dir / f"tmux-state-{profile}.json"
    else:
        return runtime_dir / "tmux-state.json"

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
            check=True
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
                    
                    # Create window using kitty-claude indirection (FIXED!)
                    log(f"Restore: Creating window for session {sess_id} at {path}", profile)
                    
                    # Build command string
                    cmd_parts = [kitty_claude_path]
                    if profile:
                        cmd_parts.extend(["--profile", profile])
                    cmd_parts.extend(["--new-window", "--resume-session", sess_id])
                    cmd_str = " ".join(cmd_parts)
                    
                    run(
                        ["tmux", "-L", socket, "new-window", "-c", path, "-n", win_name, cmd_str],
                        stderr=subprocess.DEVNULL
                    )
            else:
                log("Restore: No open sessions to restore", profile)
        except Exception as e:
            log(f"Restore error: {e}", profile)
            print(f"Warning: Could not restore sessions: {e}", file=sys.stderr)
    
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
        run(
            ["tmux", "-L", socket, "rename-window", default_name],
            stderr=subprocess.DEVNULL
        )
        run(
            ["tmux", "-L", socket, "set-option", "-w", f"@session_id", session_id],
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
    
    # Add to open sessions list
    add_open_session(session_id, profile)
    
    # Launch claude with the session ID (resume if provided, otherwise use --session-id)
    if resume_session_id:
        os.execvp("claude", ["claude", "--resume", session_id])
    else:
        os.execvp("claude", ["claude", "--session-id", session_id])