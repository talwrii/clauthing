#!/usr/bin/env python3
"""Session file utilities for kitty-claude.

This module handles reading and analyzing Claude Code session files (.jsonl).
"""

import json
from pathlib import Path


def session_has_messages(session_file):
    """Check if a session file has any real user messages.

    Claude Code sends an automatic "Warmup" message on startup to prime the cache.
    This doesn't count as a real conversation - Claude won't resume from it.
    
    Args:
        session_file: Path to the JSONL session file

    Returns:
        True if the session has at least one real user message (not "Warmup")
    """
    try:
        with open(session_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get('type') == 'user':
                        message = entry.get('message', {})
                        content = message.get('content', '')
                        # Ignore automatic Warmup message
                        if content != 'Warmup':
                            return True
                except json.JSONDecodeError:
                    continue
        return False
    except Exception:
        return False


def get_session_messages(session_file):
    """Get all messages from a session file.
    
    Args:
        session_file: Path to the JSONL session file
        
    Returns:
        List of message entries (dicts with 'type', 'message', etc.)
    """
    messages = []
    try:
        with open(session_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get('type') in ('user', 'assistant'):
                        messages.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return messages


def get_last_assistant_message(session_file):
    """Get the last assistant message from a session file.
    
    Args:
        session_file: Path to the JSONL session file
        
    Returns:
        The text content of the last assistant message, or None
    """
    last_message = None
    try:
        with open(session_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get('type') == 'assistant':
                        message = entry.get('message', {})
                        content = message.get('content', [])
                        # Extract text from content blocks
                        text_parts = [
                            block.get('text', '')
                            for block in content
                            if isinstance(block, dict) and block.get('type') == 'text'
                        ]
                        if text_parts:
                            last_message = '\n'.join(text_parts)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return last_message