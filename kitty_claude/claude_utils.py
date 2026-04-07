"""Utilities for interacting with Claude Code's data formats."""

import re


def encode_project_path(path):
    """Encode a path the same way Claude Code does for ~/.claude/projects/.

    Replaces all non-alphanumeric characters with hyphens.
    Matches Claude Code's: cwd.replace(/[^a-zA-Z0-9]/g, "-")
    """
    return re.sub(r'[^a-zA-Z0-9]', '-', path)