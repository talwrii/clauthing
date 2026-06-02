#!/usr/bin/env python3
"""Tests for the linked_tmux module's storage (keyed by clauthing_window).

The tmux operations target the user's real `default` server, so they're not
exercised here — this covers the per-window-state-backed link storage, which is
the bit the refactor changed.
"""

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from clauthing import linked_tmux
from clauthing.session import get_window_state_dir


def _cleanup(profile):
    # window-state lives under other-profiles/<profile>/ — remove the whole profile dir.
    shutil.rmtree(get_window_state_dir(profile).parent, ignore_errors=True)


def test_linked_window_roundtrip():
    profile = f"lt-{os.getpid()}"
    cw = "window-abc"
    try:
        assert linked_tmux.get_linked_window(cw, profile) is None, "starts unlinked"
        linked_tmux.set_linked_window(cw, profile, "@7")
        assert linked_tmux.get_linked_window(cw, profile) == "@7", "stores the link"
        # Distinct window id is independent.
        assert linked_tmux.get_linked_window("other-window", profile) is None
        assert linked_tmux.clear_linked_window(cw, profile) is True, "clears once"
        assert linked_tmux.clear_linked_window(cw, profile) is False, "no-op when absent"
        assert linked_tmux.get_linked_window(cw, profile) is None, "stays unlinked"
    finally:
        _cleanup(profile)
    print("✓ test_linked_window_roundtrip")


def test_no_clauthing_window_is_safe():
    # None window id must never raise or write.
    assert linked_tmux.get_linked_window(None, "lt-none") is None
    assert linked_tmux.get_linked_list(None, "lt-none") == []
    print("✓ test_no_clauthing_window_is_safe")


if __name__ == "__main__":
    test_linked_window_roundtrip()
    test_no_clauthing_window_is_safe()
    print("All passed")
