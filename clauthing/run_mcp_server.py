#!/usr/bin/env python3
"""MCP server: run a batch of labelled shell commands behind ONE tmux popup
that lists each command on its own line.

Motivation: instead of cramming several inspection commands into a single
bash call with `echo "=== label ==="` separators, Claude passes a list of
{label, command}. The user sees each command on its own labelled line in a
confirmation popup, approves the batch once, and gets structured per-command
output back. The popup IS the permission gate.
"""
import asyncio
import subprocess

from clauthing.mcp_lazy import get_mcp
# Reuse the existing popup/socket helpers so behaviour matches the command MCP.
from clauthing.command_mcp_server import confirm_popup


def _split_bash(cmd):
    """Split a shell command into (operator, segment) pairs on TOP-LEVEL
    &&, ||, ;, | — respecting single/double/backtick quotes and $(...)/${...}/
    (...) nesting so we never split inside them. Display-only, never executed.
    The first pair's operator is ''. Empty segments are dropped.
    """
    pairs, seg, op = [], [], ""
    quote = None      # active quote char: ' " or `
    depth = 0         # nesting depth of (...) / {...}
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote:
            seg.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            seg.append(c); seg.append(cmd[i + 1]); i += 2; continue
        if c in ("'", '"', "`"):
            quote = c; seg.append(c); i += 1; continue
        if c in ("(", "{"):
            depth += 1; seg.append(c); i += 1; continue
        if c in (")", "}"):
            depth = max(0, depth - 1); seg.append(c); i += 1; continue
        if depth == 0:
            two = cmd[i:i + 2]
            if two in ("&&", "||"):
                pairs.append((op, "".join(seg).strip())); seg = []; op = two; i += 2; continue
            if c == ";":
                pairs.append((op, "".join(seg).strip())); seg = []; op = ";"; i += 1; continue
            if c == "|":  # lone pipe (|| handled above)
                pairs.append((op, "".join(seg).strip())); seg = []; op = "|"; i += 1; continue
            # a lone & (background) is left inline — rare and worth seeing whole
        seg.append(c); i += 1
    pairs.append((op, "".join(seg).strip()))
    return [(o, s) for o, s in pairs if s]


def _format_popup(commands):
    """Build the popup body. Each command is labelled and its compound parts
    are split onto their own lines (standard A). Every sub-command line shares
    the same indent; the connecting operator leads the line, aligned with the
    command text above it."""
    lines = ["claude wants to run these commands:", ""]
    for i, c in enumerate(commands, 1):
        label = (c.get("label") or "").strip()
        lines.append(f"{i}. {label}".rstrip())
        for op, seg in _split_bash(c.get("command", "")):
            lines.append("     " + (f"{op} {seg}" if op else seg))
        lines.append("")
    return "\n".join(lines).rstrip()


def _run_one(command, cwd=None, timeout=60):
    try:
        r = subprocess.run(["bash", "-c", command], capture_output=True,
                           text=True, timeout=timeout, cwd=cwd or None)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).rstrip("\n")
    except subprocess.TimeoutExpired:
        return None, f"(timed out after {timeout}s)"
    except Exception as e:
        return None, f"(failed to run: {e})"


async def run_batch_mcp_server():
    mcp = get_mcp()
    server = mcp.Server(
        "clauthing-run",
        instructions=(
            "PREFER THIS over the built-in Bash/shell tool for running shell "
            "commands. Instead of one Bash call (especially one with `echo "
            "\"=== label ===\"` separators cramming several commands together), "
            "call `run` with a list of {label, command}: the user reviews each "
            "command on its own labelled line in ONE confirmation popup and "
            "approves the batch, and you get structured per-command output back. "
            "Reach for the built-in Bash tool only when this doesn't fit (e.g. "
            "an interactive command, or a long-running/background process)."
        ),
    )

    run_tool = mcp.Tool(
        name="run",
        description=(
            "PREFERRED way to run shell commands — use this instead of the "
            "built-in Bash tool. Pass a list of labelled commands; the user "
            "sees every command on its own line in a single confirmation popup, "
            "approves the batch once, and you get structured per-command output "
            "back. Especially replace any Bash call that would use `echo` "
            "headers to label multiple commands. Commands run in order via "
            "`bash -c`. Fall back to the built-in Bash tool only for "
            "interactive or long-running/background commands."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "commands": {
                    "type": "array",
                    "description": "Commands to run, in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "Short label shown on its own line in the popup.",
                            },
                            "command": {
                                "type": "string",
                                "description": "Shell command (run via bash -c).",
                            },
                            "cwd": {
                                "type": "string",
                                "description": "Optional working directory for this command.",
                            },
                        },
                        "required": ["command"],
                    },
                },
            },
            "required": ["commands"],
        },
    )

    @server.list_tools()
    async def list_tools():
        return [run_tool]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name != "run":
            return [mcp.TextContent(type="text", text=f"Unknown tool: {name}")]
        commands = arguments.get("commands") or []
        if not commands:
            return [mcp.TextContent(type="text", text="Error: no commands given")]
        # ONE popup listing every command on its own line — the permission gate.
        if not confirm_popup(_format_popup(commands), width="90%", height="75%"):
            return [mcp.TextContent(type="text", text="User denied the commands.")]
        chunks = []
        for i, c in enumerate(commands, 1):
            label = (c.get("label") or "").strip()
            cmd = c.get("command", "")
            code, out = _run_one(cmd, cwd=c.get("cwd"))
            head = f"### {i}. {label}".rstrip()
            status = "" if code == 0 else f"  [exit {code}]"
            chunks.append(f"{head}{status}\n$ {cmd}\n{out or '(no output)'}")
        return [mcp.TextContent(type="text", text="\n\n".join(chunks))]

    async with mcp.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


def main():
    """Entry point for --run-mcp-server flag."""
    asyncio.run(run_batch_mcp_server())


if __name__ == "__main__":
    main()
