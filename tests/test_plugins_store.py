#!/usr/bin/env python3
"""Tests for the plugin directory (clauthing.plugins_store) + permission pinning."""

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from clauthing import plugins_store as ps


def _cleanup(profile):
    shutil.rmtree(ps.plugins_dir(profile).parent, ignore_errors=True)


def test_save_load_resolve_and_pin():
    profile = f"ps-{os.getpid()}"
    try:
        assert ps.load_plugin("jugglarm", profile) is None, "starts absent"

        tok = ps.issue_token()
        ps.save_plugin("jugglarm", ["windows", "window:open", "window:close"], tok, profile)

        entry = ps.load_plugin("jugglarm", profile)
        assert entry["name"] == "jugglarm"
        assert entry["permissions"] == ["window:close", "window:open", "windows"]  # sorted
        assert entry["token"] == tok

        # resolve by token
        assert ps.resolve_token(tok, profile)["name"] == "jugglarm"
        assert ps.resolve_token("nope", profile) is None

        # pinning: order-insensitive match; mismatch on different set
        assert ps.permissions_match(entry, ["windows", "window:open", "window:close"])
        assert not ps.permissions_match(entry, ["windows"])
        assert not ps.permissions_match(entry, ["windows", "window:open", "window:close", "extra"])
    finally:
        _cleanup(profile)
    print("✓ test_save_load_resolve_and_pin")


def test_tokens_are_distinct():
    assert ps.issue_token() != ps.issue_token()
    print("✓ test_tokens_are_distinct")


if __name__ == "__main__":
    test_save_load_resolve_and_pin()
    test_tokens_are_distinct()
    print("All passed")
