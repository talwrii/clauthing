#!/usr/bin/env python3
"""Claude-specific operations for clauthing."""
import os
import sys
import json
import uuid
import shutil
import subprocess
from pathlib import Path
from clauthing.logging import log, run
from clauthing.session import (
    get_session_name,
    add_open_session,
    get_state_dir,
    save_session_metadata,
    get_open_sessions,
    remove_open_session
)
from clauthing.tmux import get_runtime_tmux_state_file
from clauthing.rules import build_claude_md
import time


def deep_merge(base, override):
    """Deep merge two dictionaries, with override taking precedence.

    Args:
        base: Base dictionary
        override: Override dictionary

    Returns:
        Merged dictionary
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def propagate_credentials(profile=None):
    """Copy fresh credentials from any running session back to the shared claude-data dir.

    When Claude refreshes an OAuth token, it replaces the session's .credentials.json
    symlink with a regular file. This means the shared claude-data/.credentials.json
    becomes stale. This function finds the freshest credentials across all sessions
    and updates the shared file.
    """
    if profile:
        base_config = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "clauthing"

    shared_creds = base_config / "claude-data" / ".credentials.json"
    session_configs_dir = base_config / "session-configs"

    if not session_configs_dir.exists():
        return

    # Find the freshest credentials across all sessions
    best_expiry = 0
    best_creds_content = None

    # Check current shared credentials expiry
    if shared_creds.exists():
        try:
            shared_data = json.loads(shared_creds.read_text())
            best_expiry = shared_data.get("claudeAiOauth", {}).get("expiresAt", 0)
        except Exception:
            pass

    # Scan all session directories for fresher credentials
    for session_dir in session_configs_dir.iterdir():
        if not session_dir.is_dir():
            continue
        creds_file = session_dir / ".credentials.json"
        # Only check regular files (not symlinks) - those are the ones Claude has refreshed
        if creds_file.exists() and not creds_file.is_symlink():
            try:
                content = creds_file.read_text()
                data = json.loads(content)
                expiry = data.get("claudeAiOauth", {}).get("expiresAt", 0)
                if expiry > best_expiry:
                    best_expiry = expiry
                    best_creds_content = content
            except Exception:
                continue

    if best_creds_content:
        try:
            # Remove existing file/symlink and write fresh credentials
            if shared_creds.exists() or shared_creds.is_symlink():
                shared_creds.unlink()
            shared_creds.write_text(best_creds_content)
            log(f"Propagated fresh credentials (expires {best_expiry}) to shared location", profile)
        except Exception as e:
            log(f"Failed to propagate credentials: {e}", profile)


def setup_session_config(session_id, profile=None):
    """Create a unique config directory for this session with shared projects.

    This merges global settings.json with session-specific session.json overrides.
    The session.json only contains differences from global settings.

    Args:
        session_id: Unique session identifier
        profile: Profile name (optional)

    Returns:
        Path to the session-specific claude-data directory
    """
    # Propagate fresh credentials from running sessions before setting up new one
    propagate_credentials(profile)

    # Get base config directory
    if profile:
        base_config = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "clauthing"

    # Canonical shared projects location
    canonical_projects = base_config / "claude-data" / "projects"
    canonical_projects.mkdir(parents=True, exist_ok=True)

    # Canonical shared skills location (so all sessions share skills)
    canonical_skills = base_config / "claude-data" / "skills"
    canonical_skills.mkdir(parents=True, exist_ok=True)

    # Create session-specific config directory
    session_configs = base_config / "session-configs"
    session_configs.mkdir(parents=True, exist_ok=True)

    session_config_dir = session_configs / session_id
    session_config_dir.mkdir(parents=True, exist_ok=True)

    # Load global settings
    canonical_settings_file = base_config / "claude-data" / "settings.json"
    if canonical_settings_file.exists():
        global_settings = json.loads(canonical_settings_file.read_text())
    else:
        global_settings = {"model": "sonnet"}

    # Ensure Skill tool is always allowed globally (so all sessions can use skills)
    if "permissions" not in global_settings:
        global_settings["permissions"] = {}
    if "allow" not in global_settings["permissions"]:
        global_settings["permissions"]["allow"] = []
    if "Skill" not in global_settings["permissions"]["allow"]:
        global_settings["permissions"]["allow"].append("Skill")
        canonical_settings_file.parent.mkdir(parents=True, exist_ok=True)
        canonical_settings_file.write_text(json.dumps(global_settings, indent=2))
        log(f"Added Skill to global permissions", profile)

    # Load session overrides (if exist)
    session_overrides_file = session_config_dir / "session.json"
    if session_overrides_file.exists():
        session_overrides = json.loads(session_overrides_file.read_text())
    else:
        # Create empty overrides file
        session_overrides = {}
        session_overrides_file.write_text('{}\n')
        log(f"Created empty session.json for overrides", profile)

    # Merge global + session overrides
    merged_settings = deep_merge(global_settings, session_overrides)

    # Write merged settings.json for Claude Code to use
    merged_settings_file = session_config_dir / "settings.json"
    merged_settings_file.write_text(json.dumps(merged_settings, indent=2))
    log(f"Merged global + session settings", profile)

    # Link/copy everything from claude-data except files we manage ourselves
    claude_data_dir = base_config / "claude-data"
    skip_files = {"settings.json", ".claude.json"}
    # Files that Claude overwrites (breaking symlinks) - copy these instead
    copy_files = {".credentials.json"}
    for item in claude_data_dir.iterdir():
        if item.name in skip_files:
            continue
        link_path = session_config_dir / item.name
        if not link_path.exists():
            if item.name in copy_files:
                shutil.copy2(item, link_path)
            else:
                link_path.symlink_to(item)

    # Create session-specific .claude.json with saved auth + mcpServers from existing session
    session_claude_json = session_config_dir / ".claude.json"

    # Load saved auth from previous sessions
    saved_auth_file = base_config / "claude-auth.json"
    if saved_auth_file.exists():
        saved_auth = json.loads(saved_auth_file.read_text())
    else:
        saved_auth = {}

    # Load MCP servers and active roles from session metadata
    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"
    mcp_servers = {}
    active_roles = []
    if metadata_file.exists():
        try:
            metadata = json.loads(metadata_file.read_text())
            mcp_servers = metadata.get("mcpServers", {})
            active_roles = metadata.get("activeRoles", [])
        except:
            pass

    # Always include "default" role (create if doesn't exist)
    roles_dir = base_config / "mcp-roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    default_role_file = roles_dir / "default.json"
    if not default_role_file.exists():
        default_role_file.write_text(json.dumps({
            "permissions": {"allow": []},
            "mcpServers": {}
        }, indent=2))
    if "default" not in active_roles:
        active_roles.insert(0, "default")

    # Auto-activate roles from tmux window title
    title_roles_file = base_config / "title-roles.json"
    if title_roles_file.exists():
        try:
            title_mappings = json.loads(title_roles_file.read_text())
            tmux_socket = os.environ.get('CLAUTHING_TMUX_SOCKET', 'clauthing')
            result = subprocess.run(
                ["tmux", "-L", tmux_socket, "display-message", "-p", "#{window_name}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                window_name = result.stdout.strip()
                if window_name in title_mappings:
                    for role_name in title_mappings[window_name]:
                        if role_name not in active_roles:
                            active_roles.append(role_name)
        except:
            pass

    # Merge active roles: MCP servers + permissions
    role_permissions = []
    if active_roles:
        roles_dir = base_config / "mcp-roles"
        for role_name in active_roles:
            role_file = roles_dir / f"{role_name}.json"
            if role_file.exists():
                try:
                    role = json.loads(role_file.read_text())
                    mcp_servers.update(role.get("mcpServers", {}))
                    role_permissions.extend(role.get("permissions", {}).get("allow", []))
                except:
                    pass

    # Include built-in command MCP server
    clauthing_path = shutil.which("clauthing") or "clauthing"
    if "clauthing-commands" not in mcp_servers:
        mcp_servers["clauthing-commands"] = {
            "command": clauthing_path,
            "args": ["--command-mcp", "--with-commands"],
        }

    # Include Claude Code skills MCP server (for managing /skills)
    # NOTE: This is dangerous and should NOT be auto-approved
    if "claude-skills" not in mcp_servers:
        mcp_servers["claude-skills"] = {
            "command": clauthing_path,
            "args": ["--claude-skills-mcp"],
        }

    # Auto-approve all MCP server tools + role permissions in settings
    # NOTE: skills MCPs write tools are NOT auto-approved (dangerous - can write arbitrary code)
    # But read/list are safe and auto-approved
    dangerous_mcp_servers = {"claude-skills", "clauthing-skills"}
    allow = merged_settings.get("permissions", {}).get("allow", [])
    for server_name in mcp_servers:
        if server_name in dangerous_mcp_servers:
            continue  # Skip dangerous servers
        rule = f"mcp__{server_name}__*"
        if rule not in allow:
            allow.append(rule)
    # Auto-approve read-only skills tools (both claude-skills and cl-skills)
    safe_skill_tools = [
        "mcp__claude-skills__read_claude_skill",
        "mcp__claude-skills__list_claude_skills",
        "mcp__clauthing-skills__read_skill",
        "mcp__clauthing-skills__list_skills",
    ]
    for safe_tool in safe_skill_tools:
        if safe_tool not in allow:
            allow.append(safe_tool)
    for rule in role_permissions:
        if rule not in allow:
            allow.append(rule)
    merged_settings.setdefault("permissions", {})["allow"] = allow
    merged_settings_file.write_text(json.dumps(merged_settings, indent=2))

    # Auto-trust the current working directory
    cwd = os.getcwd()
    if "projects" not in saved_auth:
        saved_auth["projects"] = {}
    if cwd not in saved_auth["projects"]:
        saved_auth["projects"][cwd] = {}
    if not saved_auth["projects"][cwd].get("hasTrustDialogAccepted"):
        saved_auth["projects"][cwd]["hasTrustDialogAccepted"] = True
        log(f"Auto-trusted directory: {cwd}", profile)

    # Build new config with saved auth + session MCP servers
    session_config = {"mcpServers": mcp_servers, **saved_auth}
    session_claude_json.write_text(json.dumps(session_config, indent=2))
    log(f"Created/updated session-specific .claude.json", profile)

    log(f"Session config ready: {session_config_dir}", profile)
    return session_config_dir


def get_running_sessions_file(profile=None):
    """Get path to running sessions tracking file."""
    if profile:
        base_config = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "clauthing"
    return base_config / "running-sessions.json"


def register_running_session(session_id, pid, cwd, profile=None):
    """Register a running Claude session."""
    running_file = get_running_sessions_file(profile)

    # Load existing
    if running_file.exists():
        try:
            running = json.loads(running_file.read_text())
        except:
            running = {}
    else:
        running = {}

    # Add this session
    running[session_id] = {
        "pid": pid,
        "cwd": cwd,
        "started": int(time.time())
    }

    # Save
    running_file.write_text(json.dumps(running, indent=2))


def unregister_running_session(session_id, profile=None):
    """Remove a session from running sessions."""
    running_file = get_running_sessions_file(profile)

    if not running_file.exists():
        return

    try:
        running = json.loads(running_file.read_text())
        if session_id in running:
            del running[session_id]
            running_file.write_text(json.dumps(running, indent=2))
    except:
        pass


def get_running_sessions(profile=None):
    """Get list of currently running sessions (checks PIDs are alive)."""
    running_file = get_running_sessions_file(profile)

    if not running_file.exists():
        return []

    try:
        running = json.loads(running_file.read_text())
    except:
        return []

    # Check which PIDs are still alive
    alive_sessions = []
    stale_sessions = []

    for session_id, info in running.items():
        pid = info["pid"]
        # Check if process exists
        try:
            os.kill(pid, 0)  # Signal 0 just checks if process exists
            alive_sessions.append({
                "session_id": session_id,
                **info
            })
        except (OSError, ProcessLookupError):
            stale_sessions.append(session_id)

    # Clean up stale sessions
    if stale_sessions:
        for session_id in stale_sessions:
            del running[session_id]
        running_file.write_text(json.dumps(running, indent=2))

    return alive_sessions


def get_last_user_message(session_id, cwd, profile=None):
    """Get the last user message from a session transcript.

    Args:
        session_id: The session UUID
        cwd: The project/cwd path for the session (unused, we search all projects)
        profile: Profile name (optional)

    Returns:
        The last user message text (truncated), or None if not found
    """
    if profile:
        base_config = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "clauthing"

    projects_base = base_config / "claude-data" / "projects"
    if not projects_base.exists():
        return None

    # Search all project directories for this session's transcript
    # Find the most recently modified one if multiple exist
    transcript_file = None
    latest_mtime = 0

    for project_dir in projects_base.iterdir():
        if project_dir.is_dir():
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.exists():
                mtime = candidate.stat().st_mtime
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    transcript_file = candidate

    if not transcript_file:
        return None

    def is_valid_user_text(text):
        """Check if text is a valid user message (not system/hook/reminder)."""
        if not text:
            return False
        skip_prefixes = (
            "<system-reminder>",
            "Operation stopped by hook:",
            "<system>",
        )
        return not text.startswith(skip_prefixes)

    try:
        # Read all lines and process in reverse
        # Lines can be very long (tool results) so byte-based seeking doesn't work well
        with open(transcript_file, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "user":
                    msg_content = entry.get("message", {}).get("content")
                    if isinstance(msg_content, str):
                        if is_valid_user_text(msg_content):
                            return msg_content[:60]
                    elif isinstance(msg_content, list):
                        # Find text content, skip tool_results
                        for item in msg_content:
                            if isinstance(item, dict):
                                if item.get("type") == "text":
                                    text = item.get("text", "")
                                    if is_valid_user_text(text):
                                        return text[:60]
            except json.JSONDecodeError:
                continue

        return None
    except Exception:
        return None


def get_session_cwd_from_projects(session_id, base_config):
    """Look up a session's original cwd from the project directory structure.

    This is more reliable than .claude.json because the project directory hash
    reflects where the session was originally created.
    """
    projects_dir = base_config / "claude-data" / "projects"
    if not projects_dir.exists():
        return None

    for proj_dir in projects_dir.iterdir():
        if proj_dir.is_dir():
            session_file = proj_dir / f"{session_id}.jsonl"
            if session_file.exists():
                # Reverse the path hash: -home-user-project -> /home/user/project
                # But paths may contain hyphens (e.g., note-frame), so we
                # progressively try converting hyphens to slashes and check if path exists
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
                        return candidate
                # Fallback: simple conversion
                return '/' + '/'.join(parts)
    return None


def get_recent_sessions(profile=None, limit=10):
    """Get list of recent sessions ordered by last activity."""
    if profile:
        base_config = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "clauthing"

    session_configs = base_config / "session-configs"
    if not session_configs.exists():
        return []

    # Get all session directories with their last modified time
    sessions = []
    for session_dir in session_configs.iterdir():
        if session_dir.is_dir():
            session_id = session_dir.name
            # Skip lock directories and other non-session directories
            if session_id.endswith('.lock'):
                continue
            # Get last modified time (from .claude.json if exists, otherwise directory)
            claude_json = session_dir / ".claude.json"
            if claude_json.exists():
                mtime = claude_json.stat().st_mtime
            else:
                mtime = session_dir.stat().st_mtime

            # Try to get CWD from project directory structure (more reliable)
            cwd = get_session_cwd_from_projects(session_id, base_config)
            # Fallback to .claude.json if project lookup fails
            if not cwd and claude_json.exists():
                try:
                    config = json.loads(claude_json.read_text())
                    projects = config.get("projects", {})
                    if projects:
                        cwd = list(projects.keys())[0]  # Get first project path
                except:
                    pass

            # Get last message early so we can filter
            last_message = get_last_user_message(session_id, cwd, profile)

            # Skip sessions from default temp directory if they have no messages
            if cwd and cwd.startswith("/tmp/clauthing") and not last_message:
                continue

            # Get session name/title from state metadata
            title = None
            state_dir = get_state_dir()
            metadata_file = state_dir / "sessions" / f"{session_id}.json"
            if metadata_file.exists():
                try:
                    metadata = json.loads(metadata_file.read_text())
                    name = metadata.get("name")
                    # Only use as title if it's meaningful (not session ID or default)
                    if name and name != session_id and not name.startswith("clauthing-"):
                        # Also skip if it looks like another session ID (UUID format)
                        if not (len(name) == 36 and name.count('-') == 4):
                            title = name
                except:
                    pass

            sessions.append({
                "session_id": session_id,
                "title": title,
                "last_modified": mtime,
                "cwd": cwd,
                "last_message": last_message
            })

    # Sort by last modified (most recent first)
    sessions.sort(key=lambda s: s["last_modified"], reverse=True)

    return sessions[:limit]


def save_auth_from_session(session_id, profile=None):
    """Extract and save auth info from session's .claude.json for reuse.

    Args:
        session_id: Unique session identifier
        profile: Profile name (optional)
    """
    # Get base config directory
    if profile:
        base_config = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "clauthing"

    # Read session's .claude.json
    session_config_dir = base_config / "session-configs" / session_id
    session_claude_json = session_config_dir / ".claude.json"

    if not session_claude_json.exists():
        return

    try:
        session_config = json.loads(session_claude_json.read_text())

        # Extract auth + onboarding fields needed for new sessions to skip login
        auth_fields = [
            "userID", "oauthAccount", "claudeCodeFirstTokenDate",
            "hasCompletedOnboarding", "lastOnboardingVersion",
        ]
        auth_data = {}
        for field in auth_fields:
            if field in session_config:
                auth_data[field] = session_config[field]

        if auth_data:
            # Save to global auth file
            saved_auth_file = base_config / "claude-auth.json"
            saved_auth_file.write_text(json.dumps(auth_data, indent=2))
            log(f"Saved auth from session {session_id}", profile)
    except Exception as e:
        log(f"Failed to save auth: {e}", profile)


def cleanup_session_config(session_id, profile=None):
    """Remove session-specific config directory after session ends.

    Args:
        session_id: Unique session identifier
        profile: Profile name (optional)
    """
    # Save auth and credentials before cleanup
    save_auth_from_session(session_id, profile)
    propagate_credentials(profile)

    # Unregister from running sessions
    unregister_running_session(session_id, profile)

    # Get base config directory
    if profile:
        base_config = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "clauthing"

    session_config_dir = base_config / "session-configs" / session_id

    if session_config_dir.exists():
        try:
            # Remove symlink and directory
            shutil.rmtree(session_config_dir)
            log(f"Cleaned up session config: {session_config_dir}", profile)
        except Exception as e:
            log(f"Error cleaning up session config: {e}", profile)

def new_window(profile=None, resume_session_id=None, socket="clauthing"):
    """Create a new Claude window with session tracking.
    
    Args:
        profile: Profile name (optional)
        resume_session_id: Optional session ID to resume instead of creating new
        socket: Tmux socket name (optional, defaults to "clauthing")
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
                jail_dir = Path(f"/var/run/{uid}/clauthing")
                if not jail_dir.exists():
                    jail_dir = Path(f"/tmp/clauthing-{uid}")

                # Get clauthing command
                clauthing_path = shutil.which("clauthing") or "clauthing"

                # If the caller didn't specify a session to resume, use the
                # first open session for window 1. (Without this, window 1
                # would silently spawn a brand-new session and the first
                # entry of open_sessions would be dropped.)
                if not resume_session_id:
                    resume_session_id = open_sessions[0]
                    log(f"Restore: Using {resume_session_id} for window 1", profile)

                # Restore all sessions other than the one we just claimed
                # for window 1.
                log(f"Restore: Restoring {len(open_sessions[1:])} additional sessions", profile)
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
                    
                    # Create window using clauthing indirection
                    log(f"Restore: Creating window for session {sess_id} at {path}", profile)
                    
                    # Build command string
                    cmd_parts = [clauthing_path]
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
        # Check if tmux window already has a custom name (from :spawn --window-name)
        try:
            result = run(
                ["tmux", "-L", socket, "display-message", "-p", "#{window_name}"],
                capture_output=True,
                text=True,
                profile=profile
            )
            tmux_window_name = result.stdout.strip() if result.returncode == 0 else None
            # Use tmux window name if it's not a default tmux name
            if tmux_window_name and not tmux_window_name.startswith("bash") and not tmux_window_name.startswith("zsh"):
                default_name = tmux_window_name
            else:
                default_name = Path(current_path).name or "claude"
        except:
            default_name = Path(current_path).name or "claude"
        # Save session metadata with name
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
    
    # Record this session as a window-to-restore. Whether we --resume it
    # or spawn fresh on next launch is decided at restore time based on
    # session metadata's has_messages flag (set by the UserPromptSubmit
    # hook). This way blank windows still get their slot back.
    add_open_session(session_id, profile)

    # Emit session_opened event and update windows file
    try:
        from clauthing.events import emit_event, update_window
        emit_event({
            "type": "session_opened",
            "session_id": session_id,
            "name": default_name,
            "path": current_path,
        }, profile)
        update_window(session_id, default_name, socket, current_path, profile)
    except Exception as e:
        log(f"Error emitting session_opened event: {e}", profile)

    # Build CLAUDE.md from rules before launching
    build_claude_md(profile)

    # Set up session-specific config directory with shared projects
    session_config_dir = setup_session_config(session_id, profile)

    # Launch claude and wait for it to exit. Resume only if the session
    # actually has messages — blank sessions get re-spawned with the same
    # id so the window slot is preserved without claude failing on
    # "No conversation found".
    from clauthing.session import session_metadata_has_messages
    if resume_session_id and session_metadata_has_messages(resume_session_id):
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
            # Emit session_closed event and remove from windows file
            try:
                from clauthing.events import emit_event, remove_window
                emit_event({
                    "type": "session_closed",
                    "session_id": session_id,
                }, profile)
                remove_window(session_id, profile)
            except Exception as e:
                log(f"Error emitting session_closed event: {e}", profile)
        else:
            log(f"Non-zero exit code {result.returncode} - keeping session {session_id} in open sessions", profile)
            # Don't cleanup config on error in case user wants to debug
            
    except KeyboardInterrupt:
        log(f"Claude interrupted (Ctrl+C) for session {session_id} - keeping in open sessions", profile)
    except Exception as e:
        log(f"Error running claude for session {session_id}: {e}", profile)