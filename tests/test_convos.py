"""Tests for clauthing-convos: transcript aggregation + text extraction.
(Ranking is covered by test_msg_search / fuzzy_tui.)
"""
import json

from clauthing.convos import _entry_text, load_conversations
from clauthing.fuzzy_tui import rank


def test_entry_text_from_string_content():
    assert _entry_text({"message": {"content": "hello there"}}) == "hello there"


def test_entry_text_from_block_list():
    e = {"message": {"content": [
        {"type": "text", "text": "part one"},
        {"type": "tool_use", "name": "Bash", "input": {}},
        {"type": "text", "text": "part two"},
    ]}}
    assert _entry_text(e) == "part one\npart two"


def test_entry_text_empty_for_tool_only_turn():
    e = {"message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}}
    assert _entry_text(e) == ""


def _write_transcript(base, session, encoded_cwd, entries):
    d = base / session / "projects" / encoded_cwd
    d.mkdir(parents=True)
    (d / f"{session}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n")


def test_load_conversations_aggregates_user_and_assistant(tmp_path):
    _write_transcript(tmp_path, "sess1", "-home-bruger-x", [
        {"type": "user", "message": {"content": "deploy the app"},
         "cwd": "/home/bruger/x", "timestamp": "2026-07-16T10:00:00Z"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done deploying"}]},
         "cwd": "/home/bruger/x", "timestamp": "2026-07-16T10:00:05Z"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {}}]},
         "cwd": "/home/bruger/x", "timestamp": "2026-07-16T10:00:03Z"},
    ])
    convos = load_conversations(tmp_path)
    # the tool-only assistant turn is dropped; the two text turns remain
    assert [c["text"] for c in convos] == ["done deploying", "deploy the app"]  # newest first
    assert {c["role"] for c in convos} == {"user", "assistant"}
    assert all(c["session"] == "sess1" for c in convos)


def test_load_conversations_spans_multiple_sessions_newest_first(tmp_path):
    _write_transcript(tmp_path, "old", "-a", [
        {"type": "user", "message": {"content": "old msg"},
         "timestamp": "2026-01-01T00:00:00Z"}])
    _write_transcript(tmp_path, "new", "-b", [
        {"type": "user", "message": {"content": "new msg"},
         "timestamp": "2026-07-01T00:00:00Z"}])
    convos = load_conversations(tmp_path)
    assert [c["text"] for c in convos] == ["new msg", "old msg"]


def test_load_conversations_falls_back_to_encoded_cwd(tmp_path):
    _write_transcript(tmp_path, "s", "-home-bruger-proj", [
        {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-07-16T00:00:00Z"}])
    # no explicit cwd in the entry -> use the encoded project dir name
    assert load_conversations(tmp_path)[0]["cwd"] == "-home-bruger-proj"


def test_load_conversations_empty(tmp_path):
    assert load_conversations(tmp_path) == []


def test_load_conversations_dedups_cloned_turns(tmp_path):
    """A :cd clone copies the transcript into another session dir keeping each
    turn's timestamp — the same (ts, role, text) turn must appear only once."""
    turn = {"type": "user", "message": {"content": "gah!"},
            "cwd": "/home/bruger/x", "timestamp": "2026-07-16T18:45:00.000Z"}
    _write_transcript(tmp_path, "orig", "-home-bruger-x", [turn])
    _write_transcript(tmp_path, "clone1", "-home-bruger-x", [turn])   # :cd clone
    _write_transcript(tmp_path, "clone2", "-home-bruger-y", [turn])   # another clone
    convos = load_conversations(tmp_path)
    assert [c["text"] for c in convos] == ["gah!"]        # collapsed to one


def test_dedup_keeps_repeats_with_different_timestamps(tmp_path):
    """The user typing "gah!" twice at different times is NOT a duplicate."""
    _write_transcript(tmp_path, "s", "-x", [
        {"type": "user", "message": {"content": "gah!"}, "timestamp": "2026-07-16T18:45:00.000Z"},
        {"type": "user", "message": {"content": "gah!"}, "timestamp": "2026-07-16T18:46:00.000Z"},
    ])
    assert len(load_conversations(tmp_path)) == 2


def test_search_over_conversations_ranks_contiguous_first(tmp_path):
    _write_transcript(tmp_path, "s", "-x", [
        {"type": "user", "message": {"content": "d-e-p-l-o-y notes"},
         "timestamp": "2026-07-16T10:00:00Z"},
        {"type": "user", "message": {"content": "please deploy now"},
         "timestamp": "2026-07-16T10:00:01Z"},
    ])
    from clauthing.convos import _text
    convos = load_conversations(tmp_path)
    results = rank(convos, "deploy", _text)
    assert results[0][1]["text"] == "please deploy now"   # contiguous wins
    assert len(results) == 2
