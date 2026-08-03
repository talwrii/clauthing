#!/usr/bin/env python3
"""Fuzzy search across ALL clauthing message inboxes, in a curses TUI.

Aggregates every window's inbox into one searchable history and runs the shared
fuzzy picker (clauthing.fuzzy_tui) over it. Enter emits the selected message.

Invoked:  python -m clauthing.msg_search <messages-dir> [out-file]
"""
import json
import sys
import time
from pathlib import Path

from clauthing.fuzzy_tui import fuzzy_score, rank, pick  # noqa: F401 (re-exported)


def load_messages(msgs_dir):
    """Every message across every inbox, newest first, each tagged with the
    'window' (inbox) it came from."""
    out = []
    d = Path(msgs_dir)
    if d.exists():
        for inbox in sorted(d.glob("*.jsonl")):
            try:
                lines = inbox.read_text().splitlines()
            except Exception:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                m = dict(m)
                m.setdefault("window", inbox.stem)
                out.append(m)
    out.sort(key=lambda m: m.get("ts", 0), reverse=True)
    return out


def _text(m):
    return f"{m.get('from', '')} {m.get('window', '')} {m.get('message', '')}"


def _label(m, width):
    ts = m.get("ts", 0)
    t = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "     "
    body = " ".join((m.get("message", "") or "").split())
    return f"{t}  {m.get('from', '?')} » {m.get('window', '?')}: {body}"[:width]


def search(messages, query):
    """Ranked (score, message) matches — used by tests and callers."""
    return rank(messages, query, _text)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m clauthing.msg_search <messages-dir> [out-file]",
              file=sys.stderr)
        return 2
    messages = load_messages(argv[0])
    if not messages:
        print("No messages")
        return 0
    m = pick(messages, _text, _label, "search messages")
    if m is None:
        return 1
    t = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("ts", 0)))
    text = f"[{t}] {m.get('from', '?')}: {m.get('message', '')}"
    if len(argv) > 1:
        try:
            Path(argv[1]).write_text(text)
        except Exception:
            pass
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
