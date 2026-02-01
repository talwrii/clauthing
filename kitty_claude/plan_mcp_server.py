#!/usr/bin/env python3
"""
MCP server for kitty-claude planning mode.

Provides tools to get a high-level view of all kitty-claude sessions:
- list_sessions: List all sessions with metadata
- get_session_notes: Read notes for a specific session
- get_window_status: Get status of all open windows
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


def get_config_dir(profile: Optional[str] = None) -> Path:
    """Get the kitty-claude config directory."""
    if profile:
        return Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    return Path.home() / ".config" / "kitty-claude"


def get_claude_data_dir(profile: Optional[str] = None) -> Path:
    """Get the Claude data directory."""
    config_dir = get_config_dir(profile)
    return config_dir / "claude-data"


def get_state_dir() -> Path:
    """Get the XDG state directory for kitty-claude."""
    xdg_state = os.environ.get('XDG_STATE_HOME')
    if xdg_state:
        return Path(xdg_state) / "kitty-claude"
    return Path.home() / ".local" / "state" / "kitty-claude"


def list_all_sessions(profile: Optional[str] = None) -> list[dict]:
    """List only currently running sessions."""
    config_dir = get_config_dir(profile)
    running_file = config_dir / "running-sessions.json"

    if not running_file.exists():
        return []

    try:
        running = json.loads(running_file.read_text())
    except:
        return []

    state_dir = get_state_dir()
    notes_dir = config_dir / "notes"
    sessions = []

    for session_id, info in running.items():
        pid = info.get("pid")
        # Check if process is still alive
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError, TypeError):
            continue

        # Load session metadata from state dir
        metadata_file = state_dir / "sessions" / f"{session_id}.json"
        meta = {}
        if metadata_file.exists():
            try:
                meta = json.loads(metadata_file.read_text())
            except:
                pass

        # Check notes
        notes_file = notes_dir / f"{session_id}.md"
        has_notes = notes_file.exists()

        sessions.append({
            "session_id": session_id,
            "name": meta.get("name", "Unnamed"),
            "path": meta.get("path", info.get("cwd", "")),
            "pid": pid,
            "has_notes": has_notes,
        })

    return sessions


def get_session_notes_content(session_id: str, profile: Optional[str] = None) -> Optional[str]:
    """Get notes content for a specific session."""
    notes_dir = get_config_dir(profile) / "notes"
    notes_file = notes_dir / f"{session_id}.md"

    if not notes_file.exists():
        return None

    try:
        return notes_file.read_text()
    except Exception as e:
        return f"Error reading notes: {e}"


def get_window_status_info(profile: Optional[str] = None) -> dict:
    """Get status of all open windows from tmux runtime state."""
    config_dir = get_config_dir(profile)

    # Try to find runtime state file
    # For multi-tab mode
    runtime_file = config_dir / "tmux-runtime-state.json"

    if not runtime_file.exists():
        return {
            "error": "No runtime state file found",
            "mode": "unknown",
            "windows": []
        }

    try:
        state = json.loads(runtime_file.read_text())
        windows_data = state.get("windows", {})

        windows = []
        for window_id, window_info in windows_data.items():
            windows.append({
                "window_id": window_id,
                "session_id": window_info.get("session_id"),
                "session_name": window_info.get("session_name"),
                "cwd": window_info.get("cwd"),
            })

        return {
            "mode": "multi-tab",
            "windows": windows,
            "total_windows": len(windows)
        }
    except Exception as e:
        return {
            "error": f"Error reading runtime state: {e}",
            "mode": "unknown",
            "windows": []
        }


async def run_plan_mcp_server(profile: Optional[str] = None):
    """Run the planning MCP server."""
    server = Server("kitty-claude-planning")

    # Define tools
    list_sessions_tool = Tool(
        name="list_sessions",
        description="List all kitty-claude sessions with metadata (name, path, notes status, last modified)",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    )

    get_session_notes_tool = Tool(
        name="get_session_notes",
        description="Get the notes content for a specific session by session_id",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID to get notes for"
                }
            },
            "required": ["session_id"],
        },
    )

    get_window_status_tool = Tool(
        name="get_window_status",
        description="Get status of all open kitty-claude windows (session IDs, working directories, session names)",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    )

    @server.list_tools()
    async def list_tools():
        return [list_sessions_tool, get_session_notes_tool, get_window_status_tool]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name == "list_sessions":
            sessions = list_all_sessions(profile)
            return [TextContent(
                type="text",
                text=json.dumps(sessions, indent=2)
            )]

        elif name == "get_session_notes":
            session_id = arguments.get("session_id")
            if not session_id:
                return [TextContent(type="text", text="Error: session_id required")]

            notes = get_session_notes_content(session_id, profile)
            if notes is None:
                return [TextContent(type="text", text=f"No notes found for session {session_id}")]

            return [TextContent(type="text", text=notes)]

        elif name == "get_window_status":
            status = get_window_status_info(profile)
            return [TextContent(
                type="text",
                text=json.dumps(status, indent=2)
            )]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """Entry point for --plan-mcp flag."""
    profile = os.environ.get('KITTY_CLAUDE_PROFILE')
    asyncio.run(run_plan_mcp_server(profile))


if __name__ == "__main__":
    main()
