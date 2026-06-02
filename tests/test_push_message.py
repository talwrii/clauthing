#!/usr/bin/env python3
"""Tests for push_back_last_read (the :push command's core)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from clauthing.colon_commands.session_commands import push_back_last_read


def test_pushes_most_recently_read():
    msgs = [
        {"message": "a", "ts": 1, "read": True},
        {"message": "b", "ts": 2, "read": True},   # newest read → pushed back
        {"message": "c", "ts": 3, "read": False},
    ]
    pushed = push_back_last_read(msgs)
    assert pushed["message"] == "b", pushed
    assert msgs[1]["read"] is False, "the pushed message is now unread"
    assert msgs[0]["read"] is True, "older read message untouched"
    print("✓ test_pushes_most_recently_read")


def test_nothing_read_returns_none():
    msgs = [{"message": "a", "ts": 1, "read": False}]
    assert push_back_last_read(msgs) is None
    assert msgs[0]["read"] is False
    print("✓ test_nothing_read_returns_none")


def test_empty_returns_none():
    assert push_back_last_read([]) is None
    print("✓ test_empty_returns_none")


if __name__ == "__main__":
    test_pushes_most_recently_read()
    test_nothing_read_returns_none()
    test_empty_returns_none()
    print("All passed")
