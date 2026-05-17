#!/usr/bin/env python3
"""
MCP server that gives Claude control over clauthing.

Exposes a single tool that runs a colon command (e.g. ':cd /path', ':tmuxpath')
after user confirmation via tmux popup. Calls clauthing --run-command
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

from clauthing.tmux import focus_mcp_origin


def get_tmux_socket():
    """Get the clauthing tmux socket name."""
    return os.environ.get('CLAUTHING_TMUX_SOCKET', 'clauthing')


def get_session_id():
    """Extract session ID from CLAUDE_CONFIG_DIR path."""
    config_dir = os.environ.get('CLAUDE_CONFIG_DIR', '')
    if config_dir:
        return Path(config_dir).name
    return None


def get_state_dir():
    """Get the XDG state directory for clauthing."""
    xdg_state = os.environ.get('XDG_STATE_HOME')
    if xdg_state:
        return Path(xdg_state) / "clauthing"
    return Path.home() / ".local" / "state" / "clauthing"


def confirm_popup(message):
    """Show a tmux confirmation popup. Returns True if confirmed."""
    socket = get_tmux_socket()
    focus_mcp_origin(socket)
    # Use a temp file for the message body so we don't have to worry about
    # quoting (the message contains arbitrary user-controlled text including
    # colons, dashes, paths).
    import tempfile
    msg_file = Path(tempfile.mktemp(prefix="cl-confirm-", suffix=".txt"))
    msg_file.write_text(message + "\n")
    confirm_script = f"""
clear
echo
echo '──────────────────────────────────────────────'
echo '  claude wants to run:'
echo '──────────────────────────────────────────────'
cat {msg_file}
echo '──────────────────────────────────────────────'
echo
echo '  [Enter] confirm   [q] cancel'
read -n1 key
[ "$key" = "q" ] && exit 1
exit 0
"""
    try:
        result = subprocess.run(
            ["tmux", "-L", socket, "display-popup", "-E",
             "-w", "70%", "-h", "40%",
             "bash", "-c", confirm_script],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    finally:
        msg_file.unlink(missing_ok=True)


def read_linked_window_id():
    """Return the linked tmux window id (or None)."""
    session_id = get_session_id()
    if not session_id:
        return None
    metadata_file = get_state_dir() / "sessions" / f"{session_id}.json"
    if not metadata_file.exists():
        return None
    try:
        metadata = json.loads(metadata_file.read_text())
    except Exception:
        return None
    return metadata.get("linked_tmux_window")


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


# Defaults used when CLAUTHING_ALLOWED_COMMANDS env var is unset/empty.
# main.py also references these as the launch-time default.
DEFAULT_ALLOWED_COMMANDS = (
    ":tmuxpath-current",
)


def is_command_allowed(command):
    """True if the colon command is in the auto-allow list."""
    raw = os.environ.get("CLAUTHING_ALLOWED_COMMANDS")
    if raw is None or raw.strip() == "":
        # Env not set (e.g. server started outside clauthing's launch flow) —
        # fall back to the built-in defaults so common safe commands still
        # auto-approve.
        allowed = set(DEFAULT_ALLOWED_COMMANDS)
    else:
        allowed = {c.strip() for c in raw.split(",") if c.strip()}
    # Match on the command itself (':tmuxpath-current') OR the bare name + first
    # word (so ':cd /tmp' matches an entry of ':cd' if present, but
    # ':tmuxpath-current' matches exactly). Default behaviour: match exact whole
    # command first; fallback to verb-only for entries that look like a verb.
    cmd = command.strip()
    if cmd in allowed:
        return True
    verb = cmd.split(None, 1)[0]
    if verb in allowed:
        return True
    return False


def run_command(command):
    """Run a colon command, after user confirms via tmux popup unless the
    command is in the auto-allow list."""
    if not is_command_allowed(command):
        if not confirm_popup(f"{command}"):
            return f"Cancelled: {command}"

    # User confirmed — call the colon command handler
    clauthing_path = shutil.which("clauthing") or "clauthing"

    try:
        result = subprocess.run(
            [clauthing_path, "--run-command", command],
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
    server = Server(
        "clauthing-commands",
        instructions=(
            "Tools for interacting with the user's tmux setup.\n"
            "\n"
            "DESIGN: clauthing exposes a paired tmux window that the USER drives, "
            "not claude. claude can read its contents (read_tmux), focus it "
            "(focus_tmux), or trigger a colon command (kitty_command) — but "
            "intentionally CANNOT send keystrokes / write into it. The window "
            "exists so the user can run things directly without claude in the "
            "loop.\n"
            "\n"
            "Use :tmux-spawn (via kitty_command) to create + link a fresh window "
            "in the user's default tmux server. Use :tmux to link an existing one. "
            "Once linked, focus_tmux is auto-approved (no popup); read_tmux and "
            "kitty_command will pop up a confirmation."
        ),
    )

    read_tmux_tool = Tool(
        name="read_tmux",
        description=(
            "Read the contents of the linked tmux pane (the user's terminal). "
            "Shows what's currently visible on screen. User must confirm via popup."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    )

    focus_tmux_tool = Tool(
        name="focus_tmux",
        description=(
            "Switch the user's default tmux server to the linked window. "
            "Useful for drawing the user's attention to that window. "
            "Auto-approved (no popup) — only operates on the already-linked window."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    )

    kill_tmux_tool = Tool(
        name="kill_tmux",
        description=(
            "Kill the linked tmux window in the user's default tmux server and "
            "clear the link. Auto-approved (only operates on the already-linked "
            "window). After this you can :tmux-spawn a fresh window."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    )

    kitty_command_tool = Tool(
        name="kitty_command",
        description=(
            "Run a clauthing colon command. The user will be asked to confirm via popup. "
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

    tools = [read_tmux_tool, focus_tmux_tool, kill_tmux_tool]
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

        if name == "focus_tmux":
            linked_window = read_linked_window_id()
            if not linked_window:
                return [TextContent(type="text", text="No tmux window linked.")]
            try:
                subprocess.run(
                    ["tmux", "-L", "default", "select-window", "-t", linked_window],
                    check=True, capture_output=True, text=True, timeout=5,
                )
                return [TextContent(type="text", text=f"Focused {linked_window}")]
            except subprocess.CalledProcessError as e:
                return [TextContent(
                    type="text",
                    text=f"Failed to focus {linked_window}: {e.stderr or e}"
                )]

        if name == "kill_tmux":
            linked_window = read_linked_window_id()
            if not linked_window:
                return [TextContent(type="text", text="No tmux window linked.")]
            session_id = get_session_id()
            try:
                subprocess.run(
                    ["tmux", "-L", "default", "kill-window", "-t", linked_window],
                    capture_output=True, text=True, timeout=5,
                )
            except Exception:
                pass  # window may already be gone
            # Always clear the link
            if session_id:
                mf = get_state_dir() / "sessions" / f"{session_id}.json"
                if mf.exists():
                    try:
                        meta = json.loads(mf.read_text())
                        meta.pop("linked_tmux_window", None)
                        mf.write_text(json.dumps(meta, indent=2))
                    except Exception:
                        pass
            return [TextContent(type="text", text=f"Killed and unlinked {linked_window}")]

        if name == "kitty_command" and enable_commands:
            command = arguments.get("command", "")
            if not command.startswith(':'):
                command = ':' + command
            result = run_command(command)
            return [TextContent(type="text", text=result)]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(enable_commands=False):
    """Entry point for --command-mcp flag."""
    asyncio.run(run_command_mcp_server(enable_commands))


if __name__ == "__main__":
    main()
