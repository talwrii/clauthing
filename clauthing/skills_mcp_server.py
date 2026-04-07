#!/usr/bin/env python3
"""
MCP server for managing cl-skills (clauthing skills).

cl-skills are invoked with ::name in clauthing prompts. They're simple
markdown files that get injected into the conversation. Stored in:
~/.config/clauthing/cl-skills/<name>.md

NOTE: This is separate from Claude Code skills (slash commands like /commit).
"""

import asyncio
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


def get_kc_skills_dir() -> Path:
    """Get the cl-skills directory."""
    profile = os.environ.get('CLAUTHING_PROFILE')
    if profile:
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"
    return config_dir / "cl-skills"


def validate_skill_name(name: str) -> str | None:
    """Validate skill name. Returns error message or None if valid."""
    if not name:
        return "Error: skill name is required"
    if not all(c.isalnum() or c in '-_' for c in name):
        return "Error: skill name can only contain letters, numbers, dash, underscore"
    return None


async def run_skills_mcp_server():
    """Run the skills MCP server."""
    server = Server("clauthing-skills")

    create_skill_tool = Tool(
        name="create_skill",
        description=(
            "Create a cl-skill (clauthing skill, invoked with ::name). "
            "Content is plain markdown injected into the prompt. "
            "Stored in ~/.config/clauthing/cl-skills/<name>.md. "
            "Will NOT overwrite - use update_skill for existing skills."
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
            "Update an existing cl-skill (clauthing skill). "
            "Overwrites the entire file. "
            "PREFER patch_skill for partial edits - it's more readable."
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
        description="Read the content of a cl-skill (clauthing skill, ::name).",
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
        description="List all cl-skills (clauthing skills, ::name) with descriptions.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    )

    patch_skill_tool = Tool(
        name="patch_skill",
        description=(
            "Apply a unified diff patch to a cl-skill (clauthing skill). "
            "Preferred over update_skill for partial edits. "
            "Patch format: unified diff (like 'diff -u' output)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name to patch"
                },
                "patch": {
                    "type": "string",
                    "description": "Unified diff to apply"
                },
            },
            "required": ["name", "patch"],
        },
    )

    delete_skill_tool = Tool(
        name="delete_skill",
        description="Delete a cl-skill (clauthing skill file).",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name to delete"
                },
            },
            "required": ["name"],
        },
    )

    @server.list_tools()
    async def list_tools():
        return [create_skill_tool, update_skill_tool, read_skill_tool, list_skills_tool, patch_skill_tool, delete_skill_tool]

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
                return [TextContent(type="text", text="No cl-skills directory found.")]

            skills = []
            for f in sorted(skills_dir.glob("*.md")):
                first_line = f.read_text().split('\n')[0][:60] if f.read_text() else "(empty)"
                skills.append(f"{f.stem}: {first_line}")

            if not skills:
                return [TextContent(type="text", text="No cl-skills found.")]

            return [TextContent(type="text", text="\n".join(skills))]

        elif name == "patch_skill":
            import subprocess
            skill_name = arguments.get("name", "").strip()
            patch_content = arguments.get("patch", "")

            if err := validate_skill_name(skill_name):
                return [TextContent(type="text", text=err)]

            skill_file = skills_dir / f"{skill_name}.md"

            if not skill_file.exists():
                return [TextContent(type="text", text=f"Error: skill '{skill_name}' does not exist.")]

            # Apply patch using subprocess
            try:
                result = subprocess.run(
                    ["patch", "-u", str(skill_file)],
                    input=patch_content,
                    capture_output=True,
                    text=True
                )
                if result.returncode != 0:
                    return [TextContent(type="text", text=f"Patch failed:\n{result.stderr}\n{result.stdout}")]
                return [TextContent(type="text", text=f"Patched skill '{skill_name}'.\n{result.stdout}")]
            except FileNotFoundError:
                return [TextContent(type="text", text="Error: 'patch' command not found. Install with: sudo apt install patch")]
            except Exception as e:
                return [TextContent(type="text", text=f"Error applying patch: {e}")]

        elif name == "delete_skill":
            skill_name = arguments.get("name", "").strip()

            if err := validate_skill_name(skill_name):
                return [TextContent(type="text", text=err)]

            skill_file = skills_dir / f"{skill_name}.md"

            if not skill_file.exists():
                return [TextContent(type="text", text=f"Error: skill '{skill_name}' does not exist.")]

            skill_file.unlink()
            return [TextContent(type="text", text=f"Deleted skill '{skill_name}'.")]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """Entry point for --skills-mcp flag."""
    asyncio.run(run_skills_mcp_server())


if __name__ == "__main__":
    main()
