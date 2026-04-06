"""MCP server management colon commands.

Commands: :mcp, :mcp-shell, :mcp-approve, :mcps, :mcp-remove, :skills-mcp
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from kitty_claude.colon_command import command, send_tmux_message, get_state_dir
from kitty_claude.logging import log, run


@command(':mcps')
def cmd_mcps(ctx):
    if not ctx.session_id:
        return ctx.stop("No session ID.")

    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{ctx.session_id}.json"
    metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    servers = metadata.get("mcpServers", {})

    if servers:
        lines = "MCP servers in this session:\n\n"
        for name, config in servers.items():
            cmd = config.get("command", "?")
            args = " ".join(config.get("args", []))
            lines += f"  {name}: {cmd} {args}\n"
    else:
        lines = "No MCP servers in this session."
    return ctx.stop(lines)


@command(':mcp-remove')
def cmd_mcp_remove(ctx):
    server_name = ctx.args.strip()
    if not ctx.session_id or not server_name:
        return ctx.stop("Usage: :mcp-remove <server-name>")

    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{ctx.session_id}.json"
    metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    mcp_servers = metadata.get("mcpServers", {})

    if server_name not in mcp_servers:
        available = ", ".join(mcp_servers.keys()) if mcp_servers else "none"
        return ctx.stop(f"MCP server '{server_name}' not in session metadata.\nAvailable: {available}")

    del mcp_servers[server_name]
    metadata["mcpServers"] = mcp_servers
    metadata_file.write_text(json.dumps(metadata, indent=2))
    ctx.message(f"✓ Removed '{server_name}' - use :reload")
    return ctx.stop(f"✓ MCP server '{server_name}' removed.\n\nUse :reload to apply.")


@command(':mcp-approve')
def cmd_mcp_approve(ctx):
    parts = ctx.args.strip().split()
    if not parts:
        return ctx.stop("Usage: :mcp-approve <cmd> [args...]")
    if not ctx.session_id:
        return ctx.stop("❌ No session ID available")

    command_name = parts[0]
    extra_args = parts[1:]

    command_path = shutil.which(command_name)
    if not command_path:
        return ctx.stop(f"❌ Command '{command_name}' not found in PATH")

    server_name = command_name.rsplit("/", 1)[-1]
    original_def = {"command": command_path}
    if extra_args:
        original_def["args"] = extra_args

    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
    proxy_def = {
        "command": kitty_claude_path,
        "args": ["--proxy-mcp", json.dumps(original_def)],
    }

    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{ctx.session_id}.json"
    metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    metadata.setdefault("mcpServers", {})[server_name] = proxy_def
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    metadata_file.write_text(json.dumps(metadata, indent=2))

    ctx.message(f"✓ Added '{server_name}' with approval proxy - use :reload")
    return ctx.stop(f"✓ MCP server '{server_name}' added with approval proxy.\n\nUse :reload to start Claude with the new MCP server.")


@command(':mcp-shell')
def cmd_mcp_shell(ctx):
    command_name = ctx.args.strip()
    if not command_name:
        return ctx.stop("❌ Usage: :mcp-shell <command>")
    if not ctx.session_id:
        return ctx.stop("❌ No session ID found")

    try:
        command_path = shutil.which(command_name)
        if not command_path:
            return ctx.stop(f"❌ Command '{command_name}' not found in PATH")

        help_result = subprocess.run(
            [command_path, "--help"],
            capture_output=True, text=True, timeout=5
        )
        help_lines = (help_result.stdout or help_result.stderr or "").strip().split('\n')
        description = help_lines[0] if help_lines and help_lines[0] else f"Execute {command_name}"
        if len(description) > 100:
            description = description[:97] + "..."

        server_name = f"shell-{command_name}"
        kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
        server_entry = {
            "type": "stdio",
            "command": kitty_claude_path,
            "args": ["--mcp-exec", command_name, description, "--pos-arg", "input Input data"]
        }

        state_dir = get_state_dir()
        metadata_file = state_dir / "sessions" / f"{ctx.session_id}.json"
        metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
        metadata.setdefault("mcpServers", {})[server_name] = server_entry
        metadata_file.parent.mkdir(parents=True, exist_ok=True)
        metadata_file.write_text(json.dumps(metadata, indent=2))

        ctx.message(f"✓ MCP server '{server_name}' added - use :reload")
        return ctx.stop(f"✓ MCP server '{server_name}' added\n\nUse :reload to start Claude with the new MCP server.")
    except subprocess.TimeoutExpired:
        return ctx.stop(f"❌ Command '{command_name}' help timed out")
    except Exception as e:
        return ctx.stop(f"❌ Error: {str(e)}")


@command(':mcp')
def cmd_mcp(ctx):
    parts = ctx.args.strip().split()
    if not parts:
        return ctx.stop("❌ Usage: :mcp <command> [args...]")
    if not ctx.session_id:
        return ctx.stop("❌ No session ID found")

    command_name = parts[0]
    extra_args = parts[1:]

    try:
        command_path = shutil.which(command_name)
        if not command_path:
            return ctx.stop(f"❌ Command '{command_name}' not found in PATH")

        server_name = command_name.rsplit("/", 1)[-1]
        server_entry = {"type": "stdio", "command": command_path}
        if extra_args:
            server_entry["args"] = extra_args

        state_dir = get_state_dir()
        metadata_file = state_dir / "sessions" / f"{ctx.session_id}.json"
        metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
        metadata.setdefault("mcpServers", {})[server_name] = server_entry
        metadata_file.parent.mkdir(parents=True, exist_ok=True)
        metadata_file.write_text(json.dumps(metadata, indent=2))

        ctx.message(f"✓ MCP server '{server_name}' added - use :reload")
        return ctx.stop(f"✓ MCP server '{server_name}' added\n\nUse :reload to start Claude with the new MCP server.")
    except Exception as e:
        return ctx.stop(f"❌ Error: {str(e)}")


@command(':skills-mcp')
def cmd_skills_mcp(ctx):
    if not ctx.session_id:
        return ctx.stop("❌ No session ID available")

    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{ctx.session_id}.json"
    metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    metadata.setdefault("mcpServers", {})["kitty-claude-skills"] = {
        "command": kitty_claude_path,
        "args": ["--skills-mcp"],
    }
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    metadata_file.write_text(json.dumps(metadata, indent=2))

    ctx.message("✓ Skills MCP added - use :reload to apply")
    return ctx.stop("✓ Skills MCP server added.\n\nUse :reload to start Claude with the skills MCP server.")