#!/usr/bin/env python3
"""MCP server: interactive file editing via vim in a tmux popup.

Exposes a single tool `edit_file(path)` that opens vim in a tmux popup so the
user can edit the file by hand. Blocks until the user closes vim. Returns the
post-edit file size + a short head/tail preview so claude has feedback.

Auto-included in every clauthing session — see setup_session_config in
clauthing/claude.py.
"""

import asyncio
import os
import shlex
import subprocess
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


def get_tmux_socket() -> str:
    return os.environ.get('CLAUTHING_TMUX_SOCKET', 'clauthing')


def _file_summary(path: Path, max_head_lines: int = 10) -> str:
    if not path.exists():
        return "(file does not exist)"
    try:
        size = path.stat().st_size
        text = path.read_text(errors="replace")
        lines = text.splitlines()
        head = "\n".join(lines[:max_head_lines])
        more = "" if len(lines) <= max_head_lines else f"\n... ({len(lines) - max_head_lines} more lines)"
        return f"{len(lines)} lines, {size} bytes\n--- begin ---\n{head}{more}\n--- end ---"
    except Exception as e:
        return f"(could not read file: {e})"


async def run_edit_mcp_server():
    server = Server("clauthing-edit")

    edit_tool = Tool(
        name="edit_file",
        description=(
            "Open the given file in vim inside a tmux popup so the user can edit "
            "it by hand. Blocks until the user closes vim (e.g. `:wq`). Use this "
            "for edits where the user wants control — review of generated code, "
            "freeform notes, sensitive config. Path may be absolute or relative "
            "to the cwd. Vim will create the file if missing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file (absolute or relative to cwd).",
                },
            },
            "required": ["path"],
        },
    )

    @server.list_tools()
    async def list_tools():
        return [edit_tool]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name != "edit_file":
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        raw_path = arguments.get("path", "").strip()
        if not raw_path:
            return [TextContent(type="text", text="Error: path is required")]

        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path

        socket = get_tmux_socket()
        # tmux display-popup -E waits for the command to exit before closing.
        # -w / -h are popup size; vim wants room.
        # The final positional arg is interpreted by tmux as a shell command,
        # so quote the path properly to handle spaces / special chars.
        shell_cmd = f"vim {shlex.quote(str(path))}"
        cmd = [
            "tmux", "-L", socket, "display-popup",
            "-E", "-w", "90%", "-h", "90%",
            "-d", str(path.parent if path.parent.exists() else Path.cwd()),
            shell_cmd,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            return [TextContent(type="text", text="Error: edit timed out (1h)")]
        except FileNotFoundError as e:
            return [TextContent(type="text", text=f"Error: {e}")]

        if proc.returncode != 0:
            return [TextContent(
                type="text",
                text=f"vim/tmux exited with code {proc.returncode}\n"
                     f"stderr: {proc.stderr.strip()[:500]}"
            )]

        return [TextContent(
            type="text",
            text=f"Edit done: {path}\n{_file_summary(path)}"
        )]

    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_options)


def main():
    asyncio.run(run_edit_mcp_server())


if __name__ == "__main__":
    main()
