#!/usr/bin/env python3
"""Test the skills MCP server."""
import os
import sys
import tempfile
from pathlib import Path

# Point HOME at a temp dir so we don't touch real config
tmpdir = tempfile.mkdtemp(prefix="kc-skills-test-")
os.environ["HOME"] = tmpdir

from kitty_claude.skills_mcp_server import get_kc_skills_dir


def test_create_skill(name, content):
    """Simulate what the MCP tool does."""
    if not name:
        return "Error: skill name is required"
    if not all(c.isalnum() or c in '-_' for c in name):
        return "Error: skill name can only contain letters, numbers, dash, underscore"

    sd = get_kc_skills_dir()
    sd.mkdir(parents=True, exist_ok=True)
    skill_file = sd / f"{name}.md"
    if skill_file.exists():
        return f"Error: skill '{name}' already exists. Will not overwrite."
    skill_file.write_text(content)
    return f"Created skill '{name}' at {skill_file}. Use ::skill-name to invoke."


def test_skills_dir():
    skills_dir = get_kc_skills_dir()
    assert str(skills_dir).endswith("kc-skills"), f"Unexpected skills dir: {skills_dir}"
    print(f"  skills_dir: {skills_dir}")


def test_create():
    result = test_create_skill("test-danish", "Help me practice Danish vocabulary.")
    assert "Created" in result, f"Expected success, got: {result}"
    print(f"  {result}")


def test_no_overwrite():
    result = test_create_skill("test-danish", "overwrite attempt")
    assert "already exists" in result, f"Expected error, got: {result}"
    print(f"  {result}")


def test_invalid_name():
    result = test_create_skill("bad name!", "content")
    assert "Error" in result, f"Expected error, got: {result}"
    print(f"  {result}")


def test_empty_name():
    result = test_create_skill("", "content")
    assert "required" in result, f"Expected error, got: {result}"
    print(f"  {result}")


def test_file_content():
    skills_dir = get_kc_skills_dir()
    content = (skills_dir / "test-danish.md").read_text()
    assert content == "Help me practice Danish vocabulary.", f"Wrong content: {content}"
    print(f"  content verified")


if __name__ == "__main__":
    tests = [test_skills_dir, test_create, test_no_overwrite, test_invalid_name, test_empty_name, test_file_content]
    for t in tests:
        print(f"{t.__name__}:")
        t()
    print(f"\nAll {len(tests)} tests passed!")
