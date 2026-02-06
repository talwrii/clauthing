#!/usr/bin/env python3
"""
MCP server that lets Claude manage kc-skills.

Exposes tools to create, update, read, and list skill files in
~/.config/kitty-claude/kc-skills/<name>.md.
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


def validate_skill_name(name: str) -> str | None:
    """Validate skill name. Returns error message or None if valid."""
    if not name:
        return "Error: skill name is required"
    if not all(c.isalnum() or c in '-_' for c in name):
        return "Error: skill name can only contain letters, numbers, dash, underscore"
    return None


async def run_skills_mcp_server():
    """Run the skills MCP server."""
    server = Server("kitty-claude-skills")

    create_skill_tool = Tool(
        name="create_skill",
        description=(
            "Create a new kc-skill. The skill will be available as ::name in kitty-claude. "
            "Content is plain markdown that gets injected into the prompt when invoked. "
            "Will NOT overwrite existing skills - use update_skill for that."
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

    update_skill_tool = Tool(
        name="update_skill",
        description=(
            "Update an existing kc-skill. Overwrites the skill content. "
            "Use read_skill first to get current content if you need to modify it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name to update"
                },
                "content": {
                    "type": "string",
                    "description": "New markdown content for the skill"
                },
            },
            "required": ["name", "content"],
        },
    )

    read_skill_tool = Tool(
        name="read_skill",
        description="Read the content of an existing kc-skill.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name to read"
                },
            },
            "required": ["name"],
        },
    )

    list_skills_tool = Tool(
        name="list_skills",
        description="List all kc-skills with their first line as description.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    )

    @server.list_tools()
    async def list_tools():
        return [create_skill_tool, update_skill_tool, read_skill_tool, list_skills_tool]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        skills_dir = get_kc_skills_dir()

        if name == "create_skill":
            skill_name = arguments.get("name", "").strip()
            content = arguments.get("content", "")

            if err := validate_skill_name(skill_name):
                return [TextContent(type="text", text=err)]

            skills_dir.mkdir(parents=True, exist_ok=True)
            skill_file = skills_dir / f"{skill_name}.md"

            if skill_file.exists():
                return [TextContent(type="text", text=f"Error: skill '{skill_name}' already exists. Use update_skill to modify.")]

            skill_file.write_text(content)
            return [TextContent(type="text", text=f"Created skill '{skill_name}'. Use ::{skill_name} to invoke.")]

        elif name == "update_skill":
            skill_name = arguments.get("name", "").strip()
            content = arguments.get("content", "")

            if err := validate_skill_name(skill_name):
                return [TextContent(type="text", text=err)]

            skill_file = skills_dir / f"{skill_name}.md"

            if not skill_file.exists():
                return [TextContent(type="text", text=f"Error: skill '{skill_name}' does not exist. Use create_skill first.")]

            skill_file.write_text(content)
            return [TextContent(type="text", text=f"Updated skill '{skill_name}'.")]

        elif name == "read_skill":
            skill_name = arguments.get("name", "").strip()

            if err := validate_skill_name(skill_name):
                return [TextContent(type="text", text=err)]

            skill_file = skills_dir / f"{skill_name}.md"

            if not skill_file.exists():
                return [TextContent(type="text", text=f"Error: skill '{skill_name}' does not exist.")]

            content = skill_file.read_text()
            return [TextContent(type="text", text=content)]

        elif name == "list_skills":
            if not skills_dir.exists():
                return [TextContent(type="text", text="No kc-skills directory found.")]

            skills = []
            for f in sorted(skills_dir.glob("*.md")):
                first_line = f.read_text().split('\n')[0][:60] if f.read_text() else "(empty)"
                skills.append(f"{f.stem}: {first_line}")

            if not skills:
                return [TextContent(type="text", text="No kc-skills found.")]

            return [TextContent(type="text", text="\n".join(skills))]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """Entry point for --skills-mcp flag."""
    asyncio.run(run_skills_mcp_server())


if __name__ == "__main__":
    main()
