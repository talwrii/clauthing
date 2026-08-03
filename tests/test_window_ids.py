"""The durable window-name -> clauthing_window map.

The live id rides on the tmux window option @clauthing_window, which dies with
the tmux server (kitty restart / reboot). Before this map, that minted a fresh
id for every window on restart and stranded every inbox (inboxes are keyed by
clauthing_window). These tests pin the survive-a-restart behaviour.
"""
import json

import pytest

from clauthing import session


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    """Point XDG_STATE_HOME at a tmp dir so we never touch the real map."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "clauthing"


def test_recall_is_none_when_nothing_remembered():
    assert session.recall_window_id("clauthing", "god") is None


def test_remember_then_recall_round_trips():
    session.remember_window_id("clauthing", "god", "uuid-god")
    assert session.recall_window_id("clauthing", "god") == "uuid-god"


def test_ids_are_scoped_per_socket():
    session.remember_window_id("clauthing", "god", "uuid-a")
    session.remember_window_id("other", "god", "uuid-b")
    assert session.recall_window_id("clauthing", "god") == "uuid-a"
    assert session.recall_window_id("other", "god") == "uuid-b"


def test_survives_a_fresh_process_reading_the_file(state_dir):
    """The whole point: the id is on disk, not in tmux — a later process (after
    the tmux server died) reads the same value back."""
    session.remember_window_id("clauthing", "clauthing", "uuid-1")
    on_disk = json.loads((state_dir / "window-ids.json").read_text())
    assert on_disk["clauthing"]["clauthing"] == "uuid-1"


def test_remember_updates_an_existing_name():
    session.remember_window_id("clauthing", "god", "uuid-old")
    session.remember_window_id("clauthing", "god", "uuid-new")
    assert session.recall_window_id("clauthing", "god") == "uuid-new"


def test_remember_keeps_other_windows_intact():
    session.remember_window_id("clauthing", "god", "uuid-god")
    session.remember_window_id("clauthing", "state", "uuid-state")
    assert session.recall_window_id("clauthing", "god") == "uuid-god"
    assert session.recall_window_id("clauthing", "state") == "uuid-state"


@pytest.mark.parametrize("socket,name,cw", [
    (None, "god", "u"), ("clauthing", None, "u"), ("clauthing", "god", None),
    ("", "god", "u"), ("clauthing", "", "u"),
])
def test_remember_ignores_incomplete_input(socket, name, cw, state_dir):
    session.remember_window_id(socket, name, cw)
    assert not (state_dir / "window-ids.json").exists()


def test_recall_tolerates_a_corrupt_file(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "window-ids.json").write_text("{not json")
    assert session.recall_window_id("clauthing", "god") is None


# ── name-keyed inboxes (the thing that makes messages survive a restart) ─────

def test_inbox_slug_makes_names_filesystem_safe():
    assert session.inbox_slug("work alive") == "work-alive"
    assert session.inbox_slug("god") == "god"
    assert session.inbox_slug("a/b") == "a-b"
    assert session.inbox_slug("  pain  ") == "pain"
    assert session.inbox_slug("") == "unnamed"
    assert session.inbox_slug(None) == "unnamed"


def test_inbox_path_is_keyed_by_name_not_id():
    p = session.inbox_path("god")
    assert p.name == "god.jsonl"


def test_same_name_resolves_to_the_same_inbox():
    """Two incarnations of the 'god' window share an inbox — that is the point:
    you address the name, and a restart (new id) still finds the messages."""
    assert session.inbox_path("god") == session.inbox_path("god")


def test_inbox_lives_under_state_dir_not_the_runtime_dir(state_dir):
    """Messages are user data, so they belong in the state dir. The runtime dir
    is /tmp (or /run) and is wiped on reboot — inboxes must not live there."""
    from clauthing.events import get_runtime_dir
    p = session.inbox_path("god")
    assert str(p).startswith(str(state_dir))
    assert not str(p).startswith(str(get_runtime_dir()))


def test_rename_inbox_moves_messages_to_the_new_name():
    session.inbox_path("old").write_text('{"message": "hi"}\n')
    session.rename_inbox("old", "new")
    assert not session.inbox_path("old").exists()
    assert '"hi"' in session.inbox_path("new").read_text()


def test_rename_inbox_merges_when_target_exists():
    session.inbox_path("old").write_text('{"m": 1}\n')
    session.inbox_path("new").write_text('{"m": 2}\n')
    session.rename_inbox("old", "new")
    body = session.inbox_path("new").read_text()
    assert '{"m": 1}' in body and '{"m": 2}' in body
    assert not session.inbox_path("old").exists()


def test_rename_inbox_is_a_noop_without_a_source():
    session.rename_inbox("nothing-here", "new")       # must not raise
    assert not session.inbox_path("new").exists()
