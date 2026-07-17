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
import sys
from pathlib import Path

from clauthing.tmux import focus_mcp_origin
from clauthing.mcp_lazy import get_mcp


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


def confirm_popup(message, restore=True, width="80%", height="60%"):
    """Show a tmux confirmation popup. Returns True if confirmed.

    restore=False: skip switching back to the previous window after the popup,
    so the caller can run commands on the origin window before restoring.
    Returns (approved, prev_window) when restore=False so the caller can
    restore later.
    """
    socket = get_tmux_socket()
    prev_window = focus_mcp_origin(socket)
    import tempfile
    msg_file = Path(tempfile.mktemp(prefix="cl-confirm-", suffix=".txt"))
    sep = "─" * 46
    msg_file.write_text(f"{sep}\n  claude wants to run:\n{sep}\n{message}\n{sep}\n")
    # Show it in a small curses pager (clauthing.confirm_pager): scroll with the
    # arrow keys / space, Enter confirms (exit 0), q cancels (exit 1). No
    # external `less` — scroll and confirm live in one prompt.
    try:
        result = subprocess.run(
            ["tmux", "-L", socket, "display-popup", "-E",
             "-w", width, "-h", height,
             sys.executable, "-m", "clauthing.confirm_pager", str(msg_file)],
            capture_output=True, text=True, timeout=120,
        )
        approved = result.returncode == 0
    except subprocess.TimeoutExpired:
        approved = False
    finally:
        msg_file.unlink(missing_ok=True)

    if restore:
        _restore_window(socket, prev_window)
        return approved
    return approved, prev_window


def _restore_window(socket, prev_window):
    if prev_window:
        try:
            subprocess.run(
                ["tmux", "-L", socket, "select-window", "-t", prev_window],
                capture_output=True, timeout=2,
            )
        except Exception:
            pass


def _profile():
    return os.environ.get("CLAUTHING_PROFILE")


def _clauthing_window():
    """Resolve this MCP session's stable clauthing_window (or None)."""
    sid = get_session_id()
    if not sid:
        return None
    from clauthing.session import get_clauthing_window
    return get_clauthing_window(sid)


def read_linked_window_id():
    """Return the linked tmux window id (or None). Keyed by clauthing_window."""
    cw = _clauthing_window()
    if not cw:
        return None
    from clauthing import linked_tmux
    return linked_tmux.get_linked_window(cw, _profile())


def read_linked_tmux():
    """Read the linked tmux pane contents.

    No confirmation: explicitly pairing a window with :tmux IS the consent to
    *read* it — that's the whole point of pairing. (Destructive ops like
    kill_tmux still confirm.)
    """
    linked_window = read_linked_window_id()
    if not linked_window:
        return "No tmux window linked. User needs to run :tmux first."

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
    socket = get_tmux_socket()
    prev_window = None

    if not is_command_allowed(command):
        approved, prev_window = confirm_popup(f"{command}", restore=False)
        if not approved:
            _restore_window(socket, prev_window)
            return f"Cancelled: {command}"
    # else: auto-allowed, no popup — no window switch happened

    # Restore user's window immediately — the command looks up its own
    # window/pane via session_id, so it no longer needs to be focused.
    _restore_window(socket, prev_window)

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
    mcp = get_mcp()
    server = mcp.Server(
        "clauthing-commands",
        instructions=(
            "Tools for interacting with the user's tmux setup.\n"
            "\n"
            "DESIGN: clauthing exposes a paired tmux window that the USER drives. "
            "claude can read its contents (read_tmux — no popup, pairing is the "
            "consent to read), focus it (focus_tmux), send a command into it "
            "(send_tmux — ALWAYS asks the user to confirm first), or trigger a "
            "colon command (kitty_command).\n"
            "\n"
            "Use :tmux-spawn (via kitty_command) to create + link a fresh window "
            "in the user's default tmux server. Use :tmux to link an existing one. "
            "Once linked: focus_tmux and read_tmux are auto-approved (no popup); "
            "send_tmux and kitty_command always pop up a confirmation."
        ),
    )

    read_tmux_tool = mcp.Tool(
        name="read_tmux",
        description=(
            "Read the contents of the linked tmux pane (the user's terminal). "
            "Shows what's currently visible on screen. User must confirm via popup."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    )

    focus_tmux_tool = mcp.Tool(
        name="focus_tmux",
        description=(
            "Switch the user's default tmux server to the linked window. "
            "Useful for drawing the user's attention to that window. "
            "Auto-approved (no popup) — only operates on the already-linked window."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    )

    kill_tmux_tool = mcp.Tool(
        name="kill_tmux",
        description=(
            "Kill the linked tmux window in the user's default tmux server and "
            "clear the link. Auto-approved (only operates on the already-linked "
            "window). After this you can :tmux-spawn a fresh window."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    )

    send_tmux_tool = mcp.Tool(
        name="send_tmux",
        description=(
            "Send a command to the linked tmux window (types the text, then "
            "presses Enter). The user is ALWAYS asked to confirm via popup before "
            "anything is typed — use this to run something in the user's paired "
            "terminal on their behalf."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command/text to type into the linked window.",
                },
            },
            "required": ["command"],
        },
    )

    kitty_command_tool = mcp.Tool(
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

    tools = [read_tmux_tool, focus_tmux_tool, kill_tmux_tool, send_tmux_tool]
    if enable_commands:
        tools.append(kitty_command_tool)

    @server.list_tools()
    async def list_tools():
        return tools

    @server.call_tool()
    async def call_tool(name, arguments):
        if name == "send_tmux":
            linked_window = read_linked_window_id()
            if not linked_window:
                return [mcp.TextContent(type="text", text="No tmux window linked.")]
            command = arguments.get("command", "")
            if not command:
                return [mcp.TextContent(type="text", text="Error: command is required")]
            # ALWAYS confirm before typing into the user's terminal.
            if not confirm_popup(f"Send to linked tmux window ({linked_window}):\n  {command}"):
                return [mcp.TextContent(type="text", text="User denied sending the command.")]
            try:
                subprocess.run(
                    ["tmux", "-L", "default", "send-keys", "-t", linked_window, "-l", command],
                    check=True, capture_output=True, text=True, timeout=5,
                )
                subprocess.run(
                    ["tmux", "-L", "default", "send-keys", "-t", linked_window, "Enter"],
                    capture_output=True, text=True, timeout=5,
                )
                return [mcp.TextContent(type="text", text=f"Sent to {linked_window}: {command}")]
            except subprocess.CalledProcessError as e:
                return [mcp.TextContent(type="text", text=f"Failed to send: {e.stderr or e}")]

        if name == "read_tmux":
            result = read_linked_tmux()
            return [mcp.TextContent(type="text", text=result)]

        if name == "focus_tmux":
            linked_window = read_linked_window_id()
            if not linked_window:
                return [mcp.TextContent(type="text", text="No tmux window linked.")]
            try:
                subprocess.run(
                    ["tmux", "-L", "default", "select-window", "-t", linked_window],
                    check=True, capture_output=True, text=True, timeout=5,
                )
                return [mcp.TextContent(type="text", text=f"Focused {linked_window}")]
            except subprocess.CalledProcessError as e:
                return [mcp.TextContent(
                    type="text",
                    text=f"Failed to focus {linked_window}: {e.stderr or e}"
                )]

        if name == "kill_tmux":
            linked_window = read_linked_window_id()
            if not linked_window:
                return [mcp.TextContent(type="text", text="No tmux window linked.")]
            try:
                subprocess.run(
                    ["tmux", "-L", "default", "kill-window", "-t", linked_window],
                    capture_output=True, text=True, timeout=5,
                )
            except Exception:
                pass  # window may already be gone
            # Always clear the link (keyed by clauthing_window)
            cw = _clauthing_window()
            if cw:
                from clauthing import linked_tmux
                linked_tmux.clear_linked_window(cw, _profile())
            return [mcp.TextContent(type="text", text=f"Killed and unlinked {linked_window}")]

        if name == "kitty_command" and enable_commands:
            command = arguments.get("command", "")
            if not command.startswith(':'):
                command = ':' + command
            result = run_command(command)
            return [mcp.TextContent(type="text", text=result)]

        return [mcp.TextContent(type="text", text=f"Unknown tool: {name}")]

    async with mcp.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(enable_commands=False):
    """Entry point for --command-mcp flag."""
    asyncio.run(run_command_mcp_server(enable_commands))


if __name__ == "__main__":
    main()
