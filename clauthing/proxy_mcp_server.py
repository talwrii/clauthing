#!/usr/bin/env python3
"""
MCP proxy server that adds tmux popup approval to tool calls.

Wraps any MCP server, forwarding list_tools directly but intercepting
call_tool to show a tmux popup for user approval before forwarding.

Usage:
    clauthing --proxy-mcp '{"command": "/path/to/server", "args": ["--flag"]}'
"""

import asyncio
import json
import os
import subprocess
import sys

from clauthing.tmux import focus_mcp_origin
from clauthing.mcp_lazy import get_mcp


def confirm_popup(tool_name, arguments):
    """Show a tmux popup to approve a tool call. Returns True if approved."""
    socket = os.environ.get('CLAUTHING_TMUX_SOCKET', 'clauthing')
    prev_window = focus_mcp_origin(socket)

    # Format for display
    args_summary = json.dumps(arguments, indent=2)
    # Truncate long args for the popup
    if len(args_summary) > 400:
        args_summary = args_summary[:400] + "\n..."

    # Escape single quotes for shell
    safe_name = tool_name.replace("'", "'\\''")
    safe_args = args_summary.replace("'", "'\\''").replace("\n", "\\n")

    confirm_script = f"""
printf 'MCP tool call: {safe_name}\\n\\n{safe_args}\\n\\n[Enter] to approve, [q] to deny\\n'
read -n1 key
if [ "$key" = "q" ]; then exit 1; fi
exit 0
"""
    try:
        result = subprocess.run(
            ["tmux", "-L", socket, "display-popup", "-E", "-w", "70%", "-h", "40%",
             "bash", "-c", confirm_script],
            capture_output=True, text=True, timeout=60,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    finally:
        if prev_window:
            try:
                subprocess.run(
                    ["tmux", "-L", socket, "select-window", "-t", prev_window],
                    capture_output=True, timeout=2,
                )
            except Exception:
                pass


async def run_proxy(mcpdef_json):
    """Run the MCP proxy server."""
    mcp = get_mcp()
    mcpdef = json.loads(mcpdef_json)
    command = mcpdef["command"]
    args = mcpdef.get("args", [])
    env_overrides = mcpdef.get("env", None)

    # Build env for the real server (inherit current env + overrides)
    real_env = dict(os.environ)
    if env_overrides:
        real_env.update(env_overrides)

    server_params = mcp.StdioServerParameters(
        command=command,
        args=args,
        env=real_env,
    )

    proxy = mcp.Server("mcp-proxy")

    async with mcp.stdio_client(server_params) as (client_read, client_write):
        async with mcp.ClientSession(client_read, client_write) as client:
            await client.initialize()

            # Get tools from real server
            tools_result = await client.list_tools()

            @proxy.list_tools()
            async def list_tools():
                return tools_result.tools

            @proxy.call_tool()
            async def call_tool(name, arguments):
                if not confirm_popup(name, arguments):
                    return [mcp.TextContent(type="text", text=f"User denied: {name}")]

                result = await client.call_tool(name, arguments)
                return result.content

            async with mcp.stdio_server() as (read_stream, write_stream):
                await proxy.run(read_stream, write_stream, proxy.create_initialization_options())


def main():
    """Entry point for --proxy-mcp flag."""
    # The MCPDEF JSON is the last argument
    mcpdef_json = sys.argv[-1]
    asyncio.run(run_proxy(mcpdef_json))


if __name__ == "__main__":
    main()
