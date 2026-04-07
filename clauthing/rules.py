#!/usr/bin/env python3
"""Rule management for clauthing."""

from pathlib import Path


def save_rule(name, content, profile=None):
    """Save a rule to the rules directory.

    Args:
        name: Rule name (will be sanitized for filename)
        content: Rule content (markdown text)
        profile: Profile name (optional)
    """
    # Get config directory
    if profile:
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"

    rules_dir = config_dir / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize name for filename
    safe_name = "".join(c if c.isalnum() or c in ('-', '_') else '-' for c in name)
    rule_file = rules_dir / f"{safe_name}.md"

    # Write content
    rule_file.write_text(content)
    print(f"✓ Rule saved: {rule_file}")

    return rule_file


def list_rules(profile=None):
    """List all rules in the rules directory.

    Args:
        profile: Profile name (optional)

    Returns:
        List of rule names (without .md extension)
    """
    # Get config directory
    if profile:
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"

    rules_dir = config_dir / "rules"

    if not rules_dir.exists():
        return []

    rule_files = sorted(rules_dir.glob("*.md"))
    return [rule_file.stem for rule_file in rule_files]


def show_rule(name, profile=None):
    """Show the content of a specific rule.

    Args:
        name: Rule name
        profile: Profile name (optional)

    Returns:
        Rule content as string, or None if not found
    """
    # Get config directory
    if profile:
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"

    rules_dir = config_dir / "rules"

    # Sanitize name for filename
    safe_name = "".join(c if c.isalnum() or c in ('-', '_') else '-' for c in name)
    rule_file = rules_dir / f"{safe_name}.md"

    if not rule_file.exists():
        return None

    return rule_file.read_text()


def build_claude_md(profile=None):
    """Build CLAUDE.md from all rules in the rules directory.

    Args:
        profile: Profile name (optional)
    """
    # Get config directory
    if profile:
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"

    rules_dir = config_dir / "rules"
    claude_data_dir = config_dir / "claude-data"
    claude_md = claude_data_dir / "CLAUDE.md"

    # If no rules directory, don't create CLAUDE.md
    if not rules_dir.exists() or not any(rules_dir.iterdir()):
        return

    # Collect all rule files
    rule_files = sorted(rules_dir.glob("*.md"))

    if not rule_files:
        return

    # Build CLAUDE.md content
    content_parts = []
    for rule_file in rule_files:
        rule_content = rule_file.read_text()
        content_parts.append(f"# {rule_file.stem}\n\n{rule_content}")

    final_content = "\n\n".join(content_parts)

    # Write CLAUDE.md
    claude_data_dir.mkdir(parents=True, exist_ok=True)
    claude_md.write_text(final_content)

    print(f"✓ Built CLAUDE.md from {len(rule_files)} rule(s)")
