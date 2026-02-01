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


def run_command(command):
    """Run a colon command after user confirms via tmux popup."""
    socket = get_tmux_socket()

    # Show confirmation popup - exits 0 if confirmed, 1 if cancelled
    confirm_script = f"""
printf 'Run: {command}\\n\\n[Enter] to confirm, [q] to cancel\\n'
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
    except subprocess.TimeoutExpired:
        return "Confirmation timed out."

    if result.returncode != 0:
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


async def run_command_mcp_server():
    """Run the command MCP server."""
    server = Server("kitty-claude-commands")

    tool = Tool(
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

    @server.list_tools()
    async def list_tools():
        return [tool]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name == "kitty_command":
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
    asyncio.run(run_command_mcp_server())


if __name__ == "__main__":
    main()
