#!/usr/bin/env python3
"""
mcp-exec: Wrap CLI tools as MCP servers.

Single tool mode:
  mcp-exec convert "Resize images" \
    --pos-arg "input Input file" \
    --flag "-resize= Resize dimensions"

Multi-tool mode (subcommands):
  mcp-exec book-by-para \
    --tool "start Start reading a book" --pos-arg "file Path to book" \
    --tool "next Get next paragraph" \
    --tool "status Show reading status"
"""

import asyncio
import subprocess
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


def parse_spaced(val: str) -> tuple[str, str]:
    """Parse 'name description' format (space-separated)."""
    parts = val.split(None, 1)
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], parts[1]


def parse_flag(val: str) -> tuple[str, str, str, bool]:
    """Parse flag format. Returns (flag, param_name, description, takes_value)."""
    parts = val.split(None, 1)
    flag_part = parts[0]
    desc = parts[1] if len(parts) > 1 else flag_part
    
    takes_value = flag_part.endswith("=")
    if takes_value:
        flag_part = flag_part[:-1]
    
    param_name = flag_part.lstrip("-")
    return flag_part, param_name, desc, takes_value


def parse_args(argv):
    """Custom parser to handle --tool grouping."""
    if len(argv) < 2:
        print("Usage: mcp-exec <command> <description> [options]")
        print("       mcp-exec <command> [--extra-args 'args'] --tool 'name desc' [--pos-arg ...] [--tool ...]")
        sys.exit(1)
    
    command = argv[1]
    rest = argv[2:]
    
    # Extract --extra-args first (these get appended to every command)
    extra_args = []
    filtered_rest = []
    i = 0
    while i < len(rest):
        if rest[i] == "--extra-args" and i + 1 < len(rest):
            # Split the extra args string into individual args
            extra_args.extend(rest[i + 1].split())
            i += 2
        else:
            filtered_rest.append(rest[i])
            i += 1
    rest = filtered_rest
    
    # Check if multi-tool mode (has --tool)
    if "--tool" in rest:
        cmd, tools = parse_multi_tool(command, rest)
    else:
        cmd, tools = parse_single_tool(command, rest)
    
    return cmd, tools, extra_args


def parse_single_tool(command, rest):
    """Parse single-tool mode: mcp-exec cmd 'desc' --pos-arg ... --flag ..."""
    if not rest:
        print("Error: Missing description")
        sys.exit(1)
    
    description = rest[0]
    rest = rest[1:]
    
    pos_args = []
    flags = []
    required_flags = []
    
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--pos-arg" and i + 1 < len(rest):
            name, desc = parse_spaced(rest[i + 1])
            pos_args.append({"name": name, "description": desc})
            i += 2
        elif arg == "--flag" and i + 1 < len(rest):
            f, n, d, tv = parse_flag(rest[i + 1])
            flags.append({"flag": f, "name": n, "description": d, "takes_value": tv})
            i += 2
        elif arg == "--required-flag" and i + 1 < len(rest):
            f, n, d, tv = parse_flag(rest[i + 1])
            required_flags.append({"flag": f, "name": n, "description": d, "takes_value": tv})
            i += 2
        else:
            i += 1
    
    tools = [{
        "name": command,
        "subcommand": None,
        "description": description,
        "pos_args": pos_args,
        "flags": flags,
        "required_flags": required_flags,
    }]
    
    return command, tools


def parse_multi_tool(command, rest):
    """Parse multi-tool mode: mcp-exec cmd --tool 'name desc' --pos-arg ..."""
    tools = []
    current_tool = None
    
    i = 0
    while i < len(rest):
        arg = rest[i]
        
        if arg == "--tool" and i + 1 < len(rest):
            # Save previous tool
            if current_tool:
                tools.append(current_tool)
            
            # Start new tool
            name, desc = parse_spaced(rest[i + 1])
            current_tool = {
                "name": name,
                "subcommand": name,
                "description": desc,
                "pos_args": [],
                "flags": [],
                "required_flags": [],
            }
            i += 2
        elif arg == "--pos-arg" and i + 1 < len(rest):
            if current_tool is None:
                print("Error: --pos-arg before any --tool")
                sys.exit(1)
            name, desc = parse_spaced(rest[i + 1])
            current_tool["pos_args"].append({"name": name, "description": desc})
            i += 2
        elif arg == "--flag" and i + 1 < len(rest):
            if current_tool is None:
                print("Error: --flag before any --tool")
                sys.exit(1)
            f, n, d, tv = parse_flag(rest[i + 1])
            current_tool["flags"].append({"flag": f, "name": n, "description": d, "takes_value": tv})
            i += 2
        elif arg == "--required-flag" and i + 1 < len(rest):
            if current_tool is None:
                print("Error: --required-flag before any --tool")
                sys.exit(1)
            f, n, d, tv = parse_flag(rest[i + 1])
            current_tool["required_flags"].append({"flag": f, "name": n, "description": d, "takes_value": tv})
            i += 2
        else:
            i += 1
    
    # Don't forget last tool
    if current_tool:
        tools.append(current_tool)
    
    if not tools:
        print("Error: No tools defined")
        sys.exit(1)
    
    return command, tools


def build_command(base_command: str, tool: dict, params: dict, extra_args: list = None) -> list[str]:
    """Build the command list from params."""
    cmd = [base_command]
    
    # Add subcommand if present
    if tool.get("subcommand"):
        cmd.append(tool["subcommand"])
    
    # Add positional args in order
    for arg in tool["pos_args"]:
        cmd.append(str(params[arg["name"]]))
    
    # Add required flags
    for flag in tool["required_flags"]:
        val = params[flag["name"]]
        cmd.append(flag["flag"])
        if flag["takes_value"]:
            cmd.append(str(val))
    
    # Add optional flags if present
    for flag in tool["flags"]:
        name = flag["name"]
        if name in params and params[name]:
            cmd.append(flag["flag"])
            if flag["takes_value"]:
                cmd.append(str(params[name]))
    
    # Add extra args at the end
    if extra_args:
        cmd.extend(extra_args)
    
    return cmd


def build_mcp_tool(tool: dict) -> Tool:
    """Build MCP Tool from tool definition."""
    properties = {}
    required = []
    
    for arg in tool["pos_args"]:
        properties[arg["name"]] = {
            "type": "string",
            "description": arg["description"],
        }
        required.append(arg["name"])
    
    for flag in tool["required_flags"]:
        if flag["takes_value"]:
            properties[flag["name"]] = {
                "type": "string",
                "description": flag["description"],
            }
        else:
            properties[flag["name"]] = {
                "type": "boolean",
                "description": flag["description"],
            }
        required.append(flag["name"])
    
    for flag in tool["flags"]:
        if flag["takes_value"]:
            properties[flag["name"]] = {
                "type": "string",
                "description": flag["description"],
            }
        else:
            properties[flag["name"]] = {
                "type": "boolean",
                "description": flag["description"],
            }
    
    return Tool(
        name=tool["name"],
        description=tool["description"],
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": required,
        },
    )


async def run_server(base_command: str, tools: list, extra_args: list = None):
    """Run the MCP server."""
    server = Server("mcp-exec")
    
    mcp_tools = [build_mcp_tool(t) for t in tools]
    tool_map = {t["name"]: t for t in tools}
    
    @server.list_tools()
    async def list_tools():
        return mcp_tools
    
    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name not in tool_map:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        
        tool = tool_map[name]
        cmd = build_command(base_command, tool, arguments, extra_args)
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"
            if result.returncode != 0:
                output += f"\nExit code: {result.returncode}"
            return [TextContent(type="text", text=output or "(no output)")]
        except subprocess.TimeoutExpired:
            return [TextContent(type="text", text="Command timed out")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]
    
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    base_command, tools, global_args = parse_args(sys.argv)
    asyncio.run(run_server(base_command, tools, global_args))


if __name__ == "__main__":
    main()