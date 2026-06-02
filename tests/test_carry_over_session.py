#!/usr/bin/env python3
"""Tests for carry_over_session_state — the :cd clone provenance/state copy."""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from clauthing.colon_commands.nav_commands import carry_over_session_state
from clauthing.session import ensure_clauthing_window, get_clauthing_window


def _sessions_dir(state_home):
    d = Path(state_home) / "clauthing" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_records_cloned_from_and_copies_state():
    """New session gets cloned_from + carried-over keys from the old one."""
    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            sessions = _sessions_dir(tmp)
            old_id, new_id = "old-sess", "new-sess"
            (sessions / f"{old_id}.json").write_text(json.dumps({
                "name": "clauthing", "path": "/old",
                "dir_stack": ["/a", "/b"], "mcpServers": {"x": 1},
            }))
            (sessions / f"{new_id}.json").write_text(json.dumps({
                "name": "clauthing", "path": "/new",
            }))

            carry_over_session_state(old_id, new_id)

            new_meta = json.loads((sessions / f"{new_id}.json").read_text())
            assert new_meta["cloned_from"] == old_id, "should record provenance"
            assert new_meta["dir_stack"] == ["/a", "/b"], "should carry dir_stack"
            assert new_meta["mcpServers"] == {"x": 1}, "should carry mcpServers"
            assert new_meta["path"] == "/new", "should not clobber new path"
        finally:
            if old_env is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_env
    print("✓ test_records_cloned_from_and_copies_state")


def test_records_cloned_from_when_old_meta_missing():
    """Provenance is recorded even if the old session has no metadata file."""
    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            sessions = _sessions_dir(tmp)
            new_id = "new-sess"
            (sessions / f"{new_id}.json").write_text(json.dumps({
                "name": "clauthing", "path": "/new",
            }))

            carry_over_session_state("ghost-old-id", new_id)

            new_meta = json.loads((sessions / f"{new_id}.json").read_text())
            assert new_meta["cloned_from"] == "ghost-old-id", \
                "should record provenance even without old metadata"
        finally:
            if old_env is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_env
    print("✓ test_records_cloned_from_when_old_meta_missing")


def test_ensure_clauthing_window_mints_and_is_stable():
    """ensure_clauthing_window mints once, persists, and returns the same id."""
    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            sessions = _sessions_dir(tmp)
            sid = "sess-a"
            (sessions / f"{sid}.json").write_text(json.dumps({"name": "w", "path": "/p"}))

            cw1 = ensure_clauthing_window(sid)
            assert cw1, "should mint a clauthing_window"
            # Persisted into metadata, and idempotent.
            assert get_clauthing_window(sid) == cw1, "should persist into metadata"
            assert ensure_clauthing_window(sid) == cw1, "should be stable on re-call"
            # Did not clobber existing fields.
            meta = json.loads((sessions / f"{sid}.json").read_text())
            assert meta["name"] == "w" and meta["path"] == "/p", "must not clobber metadata"
        finally:
            if old_env is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_env
    print("✓ test_ensure_clauthing_window_mints_and_is_stable")


def test_clauthing_window_carries_across_cd_clone():
    """A :cd clone keeps the same clauthing_window (stable window identity)."""
    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = tmp
        try:
            sessions = _sessions_dir(tmp)
            old_id, new_id = "old-sess", "new-sess"
            (sessions / f"{old_id}.json").write_text(json.dumps({"name": "w", "path": "/old"}))
            cw = ensure_clauthing_window(old_id)
            # new session starts fresh (as save_session_metadata would leave it)
            (sessions / f"{new_id}.json").write_text(json.dumps({"name": "w", "path": "/new"}))

            carry_over_session_state(old_id, new_id)

            assert get_clauthing_window(new_id) == cw, \
                "clone should inherit the same clauthing_window"
        finally:
            if old_env is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old_env
    print("✓ test_clauthing_window_carries_across_cd_clone")


if __name__ == "__main__":
    test_records_cloned_from_and_copies_state()
    test_records_cloned_from_when_old_meta_missing()
    test_ensure_clauthing_window_mints_and_is_stable()
    test_clauthing_window_carries_across_cd_clone()
    print("All passed")
