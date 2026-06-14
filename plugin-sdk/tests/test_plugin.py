#!/usr/bin/env python3
"""Tests for the clauthing_plugin SDK (Plugin + --manifest + dispatch)."""

import contextlib
import io
import json
import sys
from pathlib import Path

# Import the SDK from this subdir without needing it installed.
sys.path.insert(0, str(Path(__file__).parent.parent))

from clauthing_plugin import Plugin


def _make():
    p = Plugin("jugglarm", "snooze windows")
    calls = []

    @p.command("juggle", "close a window, reopen after a delay",
               permissions=["window:close", "window:open"])
    def juggle(args):
        calls.append(args)
        return 0

    return p, calls


def test_manifest_shape():
    p, _ = _make()
    m = p.manifest()
    assert m["name"] == "jugglarm"
    assert len(m["commands"]) == 1
    cmd = m["commands"][0]
    assert cmd["name"] == "juggle"
    assert cmd["permissions"] == ["window:close", "window:open"]
    print("✓ test_manifest_shape")


def test_manifest_flag():
    p, _ = _make()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = p.run(["--manifest"])
    out = json.loads(buf.getvalue())
    assert rc == 0 and out["commands"][0]["name"] == "juggle"
    print("✓ test_manifest_flag")


def test_single_command_takes_all_args():
    p, calls = _make()
    assert p.run(["cgm-login", "1h"]) == 0
    assert calls == [["cgm-login", "1h"]], calls
    print("✓ test_single_command_takes_all_args")


def test_explicit_subcommand():
    p, calls = _make()
    assert p.run(["juggle", "cgm-login", "1h"]) == 0
    assert calls == [["cgm-login", "1h"]], calls
    print("✓ test_explicit_subcommand")


def test_unknown_multicommand():
    p = Plugin("multi")
    p.command("a")(lambda args: 0)
    p.command("b")(lambda args: 0)
    assert p.run(["nope"]) == 2
    print("✓ test_unknown_multicommand")


if __name__ == "__main__":
    test_manifest_shape()
    test_manifest_flag()
    test_single_command_takes_all_args()
    test_explicit_subcommand()
    test_unknown_multicommand()
    print("All passed")
