#!/usr/bin/env python3
"""Tests for build_transcript_pairs (the :history prompt/reply pairing)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from clauthing.colon_commands.session_commands import build_transcript_pairs


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _assistant(text):
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]}}


def test_basic_pairing():
    msgs = [_user("hi"), _assistant("hello"),
            _user("bye"), _assistant("see ya")]
    assert build_transcript_pairs(msgs) == [("hi", "hello"), ("bye", "see ya")]
    print("✓ test_basic_pairing")


def test_skips_warmup_and_joins_multiple_replies():
    msgs = [_user("Warmup"), _assistant("ready"),
            _user("do it"), _assistant("part1"), _assistant("part2")]
    pairs = build_transcript_pairs(msgs)
    # Warmup prompt dropped; its reply folds onto nothing (no preceding prompt).
    assert pairs == [("do it", "part1\npart2")], pairs
    print("✓ test_skips_warmup_and_joins_multiple_replies")


def test_trailing_prompt_with_no_reply():
    msgs = [_user("question?")]
    assert build_transcript_pairs(msgs) == [("question?", "")]
    print("✓ test_trailing_prompt_with_no_reply")


def test_user_content_as_blocks():
    msgs = [{"type": "user",
             "message": {"content": [{"type": "text", "text": "blocky"}]}},
            _assistant("ok")]
    assert build_transcript_pairs(msgs) == [("blocky", "ok")]
    print("✓ test_user_content_as_blocks")


if __name__ == "__main__":
    test_basic_pairing()
    test_skips_warmup_and_joins_multiple_replies()
    test_trailing_prompt_with_no_reply()
    test_user_content_as_blocks()
    print("All passed")
