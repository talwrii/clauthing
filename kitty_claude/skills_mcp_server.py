#!/usr/bin/env python3
"""
MCP server that lets Claude create kc-skills.

Exposes a create_skill tool that writes new skill files to
~/.config/kitty-claude/kc-skills/<name>.md. Create-only — refuses to
overwrite existing skills.
"""

import asyncio
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


def get_kc_skills_dir() -> Path:
    """Get the kc-skills directory."""
    profile = os.environ.get('KITTY_CLAUDE_PROFILE')
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    return config_dir / "kc-skills"


async def run_skills_mcp_server():
    """Run the skills MCP server."""
    server = Server("kitty-claude-skills")

    create_skill_tool = Tool(
        name="create_skill",
        description=(
            "Create a new kc-skill. The skill will be available as ::name in kitty-claude. "
            "Content is plain markdown that gets injected into the prompt when invoked. "
            "Will NOT overwrite existing skills."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name (alphanumeric, dash, underscore only)"
                },
                "content": {
                    "type": "string",
                    "description": "Markdown content for the skill"
                },
            },
            "required": ["name", "content"],
        },
    )

    @server.list_tools()
    async def list_tools():
        return [create_skill_tool]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name == "create_skill":
            skill_name = arguments.get("name", "").strip()
            content = arguments.get("content", "")

            if not skill_name:
                return [TextContent(type="text", text="Error: skill name is required")]

            if not all(c.isalnum() or c in '-_' for c in skill_name):
                return [TextContent(type="text", text="Error: skill name can only contain letters, numbers, dash, underscore")]

            skills_dir = get_kc_skills_dir()
            skills_dir.mkdir(parents=True, exist_ok=True)

            skill_file = skills_dir / f"{skill_name}.md"
            if skill_file.exists():
                return [TextContent(type="text", text=f"Error: skill '{skill_name}' already exists. Will not overwrite.")]

            skill_file.write_text(content)
            return [TextContent(type="text", text=f"Created skill '{skill_name}' at {skill_file}. Use ::skill-name to invoke.")]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """Entry point for --skills-mcp flag."""
    asyncio.run(run_skills_mcp_server())


if __name__ == "__main__":
    main()
