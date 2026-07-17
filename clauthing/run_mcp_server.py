#!/usr/bin/env python3
"""MCP server: run a batch of labelled shell commands behind ONE tmux popup
that lists each command on its own line.

Motivation: instead of cramming several inspection commands into a single
bash call with `echo "=== label ==="` separators, Claude passes a list of
{label, command}. The user reviews each command on its own labelled line in an
interactive confirmation popup (clauthing.run_confirm) and approves the batch,
and gets structured per-command output back. The popup IS the permission gate.

The run tool honours the same Bash(...) permission rules Claude Code uses
(bash_perms): sub-commands already allowed run without a popup, denied ones are
refused, and anything new is shown in the popup where you can approve it and/or
add a rule for it.
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from clauthing import bash_perms
from clauthing.mcp_lazy import get_mcp


def _settings_write_file(cwds):
    """Where new allow rules are written: <repo-root>/.claude/settings.local.json
    for the commands' cwd — the same file Claude writes "don't ask again" Bash
    rules to (and that :permissions / load_bash_permissions read), so a rule
    added in the dialog is honoured by Claude's own Bash tool too."""
    for d in cwds:
        if d:
            return str(bash_perms.repo_settings_local(d))
    return None


def _launch_run_confirm(commands, config_dir, cwds, width="90%", height="80%"):
    """Show the interactive confirm dialog (clauthing.run_confirm) in a tmux
    popup. Returns True iff the user confirmed (Enter)."""
    import tempfile
    from clauthing.command_mcp_server import (
        get_tmux_socket, focus_mcp_origin, _restore_window)
    socket = get_tmux_socket()
    payload = {
        "config_dir": config_dir,
        "settings_file": _settings_write_file(cwds),
        "cwds": list(cwds),
        "commands": [{"label": (c.get("label") or "").strip(),
                      "command": c.get("command", "")} for c in commands],
    }
    f = Path(tempfile.mktemp(prefix="cl-runconfirm-", suffix=".json"))
    f.write_text(json.dumps(payload))
    prev = focus_mcp_origin(socket)
    try:
        r = subprocess.run(
            ["tmux", "-L", socket, "display-popup", "-E", "-w", width, "-h", height,
             sys.executable, "-m", "clauthing.run_confirm", str(f)],
            capture_output=True, text=True, timeout=600,
        )
        return r.returncode == 0
    except Exception:
        return False
    finally:
        f.unlink(missing_ok=True)
        _restore_window(socket, prev)


def classify_batch(commands, allow, deny):
    """Classify every sub-command of a batch against the rules.

    Returns (denied, need_ask): `denied` is the list of sub-commands matching a
    deny rule; `need_ask` is how many match no rule. An empty `denied` and
    `need_ask == 0` means the whole batch is already allowed — it can run with
    no popup.
    """
    denied, need_ask = [], 0
    for c in commands:
        for _op, seg in bash_perms.split_bash(c.get("command", "")):
            st = bash_perms.seg_status(seg, allow, deny)
            if st == "deny":
                denied.append(seg)
            elif st == "ask":
                need_ask += 1
    return denied, need_ask


def format_result(i, label, command, code, out):
    """Format one command's result block for the tool's text output."""
    head = f"### {i}. {label}".rstrip()
    status = "" if code == 0 else f"  [exit {code}]"
    return f"{head}{status}\n$ {command}\n{out or '(no output)'}"


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

        # Honour the same Bash(...) permission rules Claude Code uses: classify
        # every sub-command. Denied → refuse; all allowed → run without a popup;
        # any needing approval → interactive confirm dialog.
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        cwds = {c.get("cwd") for c in commands if c.get("cwd")} or {os.getcwd()}
        allow, deny = bash_perms.load_bash_permissions(config_dir, cwds)
        denied, need_ask = classify_batch(commands, allow, deny)
        if denied:
            return [mcp.TextContent(
                type="text",
                text="Denied by Bash permissions (deny rule):\n  " +
                     "\n  ".join(denied))]
        if need_ask:
            if not _launch_run_confirm(commands, config_dir, cwds):
                return [mcp.TextContent(type="text", text="User denied the commands.")]
        # else: every sub-command is already allowed — run without a popup.
        chunks = []
        for i, c in enumerate(commands, 1):
            cmd = c.get("command", "")
            code, out = _run_one(cmd, cwd=c.get("cwd"))
            chunks.append(format_result(i, (c.get("label") or "").strip(), cmd, code, out))
        return [mcp.TextContent(type="text", text="\n\n".join(chunks))]

    async with mcp.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


def main():
    """Entry point for --run-mcp-server flag."""
    asyncio.run(run_batch_mcp_server())


if __name__ == "__main__":
    main()
