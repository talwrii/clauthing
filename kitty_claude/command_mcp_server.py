#!/usr/bin/env python3
"""
MCP server that gives Claude control over kitty-claude.

Exposes a single tool that runs a colon command (e.g. ':cd /path', ':tmuxpath')
after user confirmation via tmux popup. Calls kitty-claude --run-command
which shares the colon command handler logic.
"""

import asyncio
import json
import os
import subprocess
import shutil
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


def get_tmux_socket():
    """Get the kitty-claude tmux socket name."""
    return os.environ.get('KITTY_CLAUDE_TMUX_SOCKET', 'kitty-claude')


def get_session_id():
    """Extract session ID from CLAUDE_CONFIG_DIR path."""
    config_dir = os.environ.get('CLAUDE_CONFIG_DIR', '')
    if config_dir:
        return Path(config_dir).name
    return None


def get_state_dir():
    """Get the XDG state directory for kitty-claude."""
    xdg_state = os.environ.get('XDG_STATE_HOME')
    if xdg_state:
        return Path(xdg_state) / "kitty-claude"
    return Path.home() / ".local" / "state" / "kitty-claude"


def confirm_popup(message):
    """Show a tmux confirmation popup. Returns True if confirmed."""
    socket = get_tmux_socket()
    confirm_script = f"""
printf '{message}\\n\\n[Enter] to confirm, [q] to cancel\\n'
read -n1 key
if [ "$key" = "q" ]; then exit 1; fi
exit 0
"""
    try:
        result = subprocess.run(
            ["tmux", "-L", socket, "display-popup", "-E", "-w", "60%", "-h", "20%",
             "sh", "-c", confirm_script],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def read_linked_tmux():
    """Read the linked tmux pane contents after user confirms."""
    session_id = get_session_id()
    if not session_id:
        return "No session ID available."

    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"
    if not metadata_file.exists():
        return "No session metadata found."

    try:
        metadata = json.loads(metadata_file.read_text())
    except:
        return "Could not read session metadata."

    linked_window = metadata.get("linked_tmux_window")
    if not linked_window:
        return "No tmux window linked. User needs to run :tmux first."

    # Ask for confirmation
    if not confirm_popup(f"Let Claude read linked tmux pane ({linked_window})?"):
        return "User denied access to tmux pane."

    # Capture the pane contents
    try:
        result = subprocess.run(
            ["tmux", "-L", "default", "capture-pane", "-t", linked_window, "-p"],
            capture_output=True, text=True, check=True
        )
        content = result.stdout

        # Also get the cwd
        cwd_result = subprocess.run(
            ["tmux", "-L", "default", "display-message", "-p", "-t", linked_window, "#{pane_current_path}"],
            capture_output=True, text=True, check=True
        )
        cwd = cwd_result.stdout.strip()

        return f"Linked tmux window ({linked_window}) at: {cwd}\n\n{content}"
    except subprocess.CalledProcessError as e:
        return f"Could not capture pane: {e}"


def run_command(command):
    """Run a colon command after user confirms via tmux popup."""
    if not confirm_popup(f"Run: {command}"):
        return f"Cancelled: {command}"

    # User confirmed — call the colon command handler
    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

    try:
        result = subprocess.run(
            [kitty_claude_path, "--run-command", command],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "Command timed out."

    try:
        response = json.loads(result.stdout)
        return response.get("stopReason", json.dumps(response))
    except (json.JSONDecodeError, ValueError):
        return result.stdout or result.stderr or "No output"


async def run_command_mcp_server(enable_commands=False):
    """Run the command MCP server."""
    server = Server("kitty-claude-commands")

    read_tmux_tool = Tool(
        name="read_tmux",
        description=(
            "Read the contents of the linked tmux pane (the user's terminal). "
            "Shows what's currently visible on screen. User must confirm via popup."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    )

    kitty_command_tool = Tool(
        name="kitty_command",
        description=(
            "Run a kitty-claude colon command. The user will be asked to confirm via popup. "
            "Examples: ':cd /path', ':tmuxpath', ':tmux', ':reload', ':role myRole'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The colon command to run (e.g. ':cd /home/user/project')"
                }
            },
            "required": ["command"],
        },
    )

    tools = [read_tmux_tool]
    if enable_commands:
        tools.append(kitty_command_tool)

    @server.list_tools()
    async def list_tools():
        return tools

    @server.call_tool()
    async def call_tool(name, arguments):
        if name == "read_tmux":
            result = read_linked_tmux()
            return [TextContent(type="text", text=result)]

        if name == "kitty_command" and enable_commands:
            command = arguments.get("command", "")
            if not command.startswith(':'):
                command = ':' + command
            result = run_command(command)
            return [TextContent(type="text", text=result)]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """Entry point for --command-mcp flag."""
    enable_commands = "--with-commands" in os.sys.argv
    asyncio.run(run_command_mcp_server(enable_commands))


if __name__ == "__main__":
    main()
