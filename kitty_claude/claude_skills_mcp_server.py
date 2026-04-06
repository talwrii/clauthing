#!/usr/bin/env python3
"""
MCP server for managing Claude Code skills (Anthropic's official CLI).

Claude Code skills are slash commands (like /commit, /review-pr) that inject
prompts when invoked. They're stored in:
~/.config/kitty-claude/claude-data/skills/<name>/SKILL.md

Content format: Markdown with optional YAML frontmatter (description:, etc.)

NOTE: This is separate from kc-skills (kitty-claude skills invoked with ::name).
WARNING: Skills can execute arbitrary code - this MCP should NOT be auto-approved.
"""

import asyncio
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


def get_claude_skills_dir() -> Path:
    """Get the Claude skills directory (global, shared across sessions)."""
    profile = os.environ.get('KITTY_CLAUDE_PROFILE')
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    return config_dir / "claude-data" / "skills"


def validate_skill_name(name: str) -> str | None:
    """Validate skill name. Returns error message or None if valid."""
    if not name:
        return "Error: skill name is required"
    if not all(c.isalnum() or c in '-_' for c in name):
        return "Error: skill name can only contain letters, numbers, dash, underscore"
    return None


async def run_claude_skills_mcp_server():
    """Run the Claude skills MCP server."""
    server = Server("claude-skills")

    create_skill_tool = Tool(
        name="create_claude_skill",
        description=(
            "Create a Claude Code skill (Anthropic's official CLI). "
            "Skills are slash commands (e.g., /commit, /review-pr) that inject prompts. "
            "Stored in ~/.config/kitty-claude/claude-data/skills/<name>/SKILL.md. "
            "Content is markdown with optional YAML frontmatter (description:). "
            "Will NOT overwrite - use update_claude_skill for existing skills."
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
                    "description": "Markdown content for the skill (with optional YAML frontmatter)"
                },
            },
            "required": ["name", "content"],
        },
    )

    update_skill_tool = Tool(
        name="update_claude_skill",
        description=(
            "Update an existing Claude Code skill (slash command). "
            "Overwrites the entire SKILL.md file. "
            "PREFER patch_claude_skill for partial edits - it's more readable."
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
        name="read_claude_skill",
        description="Read the SKILL.md content of a Claude Code skill (slash command).",
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
        name="list_claude_skills",
        description="List all Claude Code skills (slash commands) with their descriptions.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    )

    patch_skill_tool = Tool(
        name="patch_claude_skill",
        description=(
            "Apply a unified diff patch to a Claude Code skill (slash command). "
            "Preferred over update_claude_skill for partial edits. "
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
        name="delete_claude_skill",
        description="Delete a Claude Code skill (removes the entire skill directory).",
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
        skills_dir = get_claude_skills_dir()

        if name == "create_claude_skill":
            skill_name = arguments.get("name", "").strip()
            content = arguments.get("content", "")

            if err := validate_skill_name(skill_name):
                return [TextContent(type="text", text=err)]

            skill_dir = skills_dir / skill_name
            skill_file = skill_dir / "SKILL.md"

            if skill_file.exists():
                return [TextContent(type="text", text=f"Error: skill '{skill_name}' already exists. Use update_claude_skill to modify.")]

            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_file.write_text(content)
            return [TextContent(type="text", text=f"Created skill '{skill_name}'. Use /{skill_name} to invoke. Reload to pick up changes.")]

        elif name == "update_claude_skill":
            skill_name = arguments.get("name", "").strip()
            content = arguments.get("content", "")

            if err := validate_skill_name(skill_name):
                return [TextContent(type="text", text=err)]

            skill_file = skills_dir / skill_name / "SKILL.md"

            if not skill_file.exists():
                return [TextContent(type="text", text=f"Error: skill '{skill_name}' does not exist. Use create_claude_skill first.")]

            skill_file.write_text(content)
            return [TextContent(type="text", text=f"Updated skill '{skill_name}'. Reload to pick up changes.")]

        elif name == "read_claude_skill":
            skill_name = arguments.get("name", "").strip()

            if err := validate_skill_name(skill_name):
                return [TextContent(type="text", text=err)]

            skill_file = skills_dir / skill_name / "SKILL.md"

            if not skill_file.exists():
                return [TextContent(type="text", text=f"Error: skill '{skill_name}' does not exist.")]

            content = skill_file.read_text()
            return [TextContent(type="text", text=content)]

        elif name == "list_claude_skills":
            if not skills_dir.exists():
                return [TextContent(type="text", text="No Claude skills directory found.")]

            skills = []
            for skill_dir in sorted(skills_dir.iterdir()):
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        content = skill_file.read_text()
                        # Try to extract description from frontmatter
                        desc = "(no description)"
                        for line in content.split('\n'):
                            if line.startswith('description:'):
                                desc = line[12:].strip()
                                break
                        skills.append(f"/{skill_dir.name}: {desc}")

            if not skills:
                return [TextContent(type="text", text="No Claude skills found.")]

            return [TextContent(type="text", text="\n".join(skills))]

        elif name == "patch_claude_skill":
            import subprocess
            skill_name = arguments.get("name", "").strip()
            patch_content = arguments.get("patch", "")

            if err := validate_skill_name(skill_name):
                return [TextContent(type="text", text=err)]

            skill_file = skills_dir / skill_name / "SKILL.md"

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
                return [TextContent(type="text", text=f"Patched skill '{skill_name}'. Reload to pick up changes.\n{result.stdout}")]
            except FileNotFoundError:
                return [TextContent(type="text", text="Error: 'patch' command not found. Install with: sudo apt install patch")]
            except Exception as e:
                return [TextContent(type="text", text=f"Error applying patch: {e}")]

        elif name == "delete_claude_skill":
            import shutil
            skill_name = arguments.get("name", "").strip()

            if err := validate_skill_name(skill_name):
                return [TextContent(type="text", text=err)]

            skill_dir = skills_dir / skill_name

            if not skill_dir.exists():
                return [TextContent(type="text", text=f"Error: skill '{skill_name}' does not exist.")]

            shutil.rmtree(skill_dir)
            return [TextContent(type="text", text=f"Deleted skill '{skill_name}'. Reload to pick up changes.")]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    """Entry point for --claude-skills-mcp flag."""
    asyncio.run(run_claude_skills_mcp_server())


if __name__ == "__main__":
    main()
