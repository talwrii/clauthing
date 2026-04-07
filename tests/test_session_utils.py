#!/usr/bin/env python3
"""Tests for session_utils module."""

import tempfile
import json
import sys
from pathlib import Path

# Add parent dir to path so we can import clauthing
sys.path.insert(0, str(Path(__file__).parent.parent))

from clauthing.session_utils import session_has_messages, get_session_messages, get_last_assistant_message


def test_warmup_only_returns_false():
    """Session with only Warmup message should return False."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        # Warmup user message
        f.write(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "Warmup"}
        }) + '\n')
        # Warmup response
        f.write(json.dumps({
            "type": "assistant", 
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Ready"}]}
        }) + '\n')
        f.flush()
        
        assert session_has_messages(f.name) == False, "Warmup-only session should return False"
        Path(f.name).unlink()
    print("✓ test_warmup_only_returns_false")


def test_real_message_returns_true():
    """Session with a real user message should return True."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        # Warmup
        f.write(json.dumps({
            "type": "user",
            "message": {"content": "Warmup"}
        }) + '\n')
        f.write(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Ready"}]}
        }) + '\n')
        # Real message
        f.write(json.dumps({
            "type": "user",
            "message": {"content": "Hello, can you help me?"}
        }) + '\n')
        f.flush()
        
        assert session_has_messages(f.name) == True, "Session with real message should return True"
        Path(f.name).unlink()
    print("✓ test_real_message_returns_true")


def test_empty_file_returns_false():
    """Empty session file should return False."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        f.flush()
        assert session_has_messages(f.name) == False, "Empty session should return False"
        Path(f.name).unlink()
    print("✓ test_empty_file_returns_false")


def test_nonexistent_file_returns_false():
    """Nonexistent file should return False (not raise)."""
    assert session_has_messages("/tmp/this-file-does-not-exist-12345.jsonl") == False
    print("✓ test_nonexistent_file_returns_false")


def test_colon_command_only_returns_false():
    """Session with only :cd command (after Warmup) should return False."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        # Warmup
        f.write(json.dumps({
            "type": "user",
            "message": {"content": "Warmup"}
        }) + '\n')
        f.write(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Ready"}]}
        }) + '\n')
        f.flush()
        
        # Note: :cd is intercepted before being written, so this tests
        # the exact scenario where user runs :cd immediately after startup
        assert session_has_messages(f.name) == False, "Warmup-only (pre-:cd) should return False"
        Path(f.name).unlink()
    print("✓ test_colon_command_only_returns_false")


def test_get_last_assistant_message():
    """Should extract last assistant message text."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        f.write(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "First response"}]}
        }) + '\n')
        f.write(json.dumps({
            "type": "user",
            "message": {"content": "Follow up"}
        }) + '\n')
        f.write(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Second response"}]}
        }) + '\n')
        f.flush()
        
        result = get_last_assistant_message(f.name)
        assert result == "Second response", f"Expected 'Second response', got '{result}'"
        Path(f.name).unlink()
    print("✓ test_get_last_assistant_message")


if __name__ == "__main__":
    test_warmup_only_returns_false()
    test_real_message_returns_true()
    test_empty_file_returns_false()
    test_nonexistent_file_returns_false()
    test_colon_command_only_returns_false()
    test_get_last_assistant_message()
    print("\nAll tests passed!")