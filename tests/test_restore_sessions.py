"""Which sessions a restore should reopen.

open-sessions.json only loses an entry on a CLEAN claude exit, so killing the
tmux server (kitty restart / reboot) leaves every session in it forever. The
real list had grown to 47 entries, which made restore open duplicate windows
("two windows called state") while other windows never came back.
"""
import json

import pytest

from clauthing import session


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(session.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _session(sid, name, activity_ts=None, home=None):
    """Register a session: metadata (name) + optional transcript (activity)."""
    meta = session.get_state_dir() / "sessions"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / f"{sid}.json").write_text(json.dumps({"name": name}))
    if activity_ts is not None:
        d = session.session_transcripts_dir() / sid / "projects" / "-proj"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{sid}.jsonl"
        f.write_text('{"type":"user"}\n')
        import os
        os.utime(f, (activity_ts, activity_ts))


def _open(*sids):
    f = session.get_open_sessions_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"sessions": list(sids)}))


def test_one_window_per_name():
    """Three 'state' sessions must not become three 'state' windows."""
    for i, sid in enumerate(["s1", "s2", "s3"]):
        _session(sid, "state", activity_ts=1000 + i)
    _open("s1", "s2", "s3")
    assert session.restorable_sessions() == ["s3"]      # most recently active


def test_distinct_names_all_survive():
    _session("a", "god", activity_ts=1000)
    _session("b", "state", activity_ts=1001)
    _open("a", "b")
    assert sorted(session.restorable_sessions()) == ["a", "b"]


def test_most_recently_active_wins_not_last_added():
    """Insertion order lies: a live session added long ago outranks a husk that
    a failed restore appended later."""
    _session("old-but-live", "clauthing", activity_ts=9000)
    _session("new-but-dead", "clauthing", activity_ts=100)
    _open("old-but-live", "new-but-dead")
    assert session.restorable_sessions() == ["old-but-live"]


def test_a_name_is_never_dropped_even_with_no_history():
    """god's sessions are all empty husks — the window must still come back.
    Losing the window entirely is worse than losing its history."""
    _session("husk", "god")                              # no transcript
    _open("husk")
    assert session.restorable_sessions() == ["husk"]


def test_session_with_history_beats_husk_of_the_same_name():
    _session("husk", "god")
    _session("real", "god", activity_ts=500)
    _open("husk", "real")
    assert session.restorable_sessions() == ["real"]


def test_uuid_named_sessions_are_skipped():
    """A 'name' that is just a uuid means the window was never named."""
    _session("x", "ef85aa00-57e0-4ffb-a4c9-73e91e99e90", activity_ts=1000)
    _open("x")
    assert session.restorable_sessions() == []


def test_ordering_is_most_recently_active_first():
    _session("a", "one", activity_ts=100)
    _session("b", "two", activity_ts=900)
    _open("a", "b")
    assert session.restorable_sessions() == ["b", "a"]


def test_prune_rewrites_the_file_and_reports_drops():
    _session("s1", "state", activity_ts=1)
    _session("s2", "state", activity_ts=2)
    _open("s1", "s2")
    keep, dropped = session.prune_open_sessions()
    assert keep == ["s2"] and dropped == ["s1"]
    assert json.loads(session.get_open_sessions_file().read_text())["sessions"] == ["s2"]


def test_prune_is_idempotent():
    _session("s1", "state", activity_ts=1)
    _open("s1")
    session.prune_open_sessions()
    keep, dropped = session.prune_open_sessions()
    assert keep == ["s1"] and dropped == []


def test_empty_list_is_safe():
    _open()
    assert session.restorable_sessions() == []
    assert session.prune_open_sessions() == ([], [])
