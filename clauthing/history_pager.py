#!/usr/bin/env python3
"""Curses pager for browsing a conversation as prompt/reply pairs.

The message boundary is the unit of navigation: j/k move between *pairs*, never
mushing two messages together. Within a long pair, Ctrl-D/Ctrl-U scroll. ?
searches backward through pairs (n repeats), / searches forward.

Invoked as:  python -m clauthing.history_pager <pairs.json>
where pairs.json is a JSON list of [prompt, reply] string pairs (oldest first).
Run inside a tmux display-popup by the :history colon command.
"""
import curses
import json
import sys
import textwrap
from pathlib import Path


def _wrap(text, width):
    lines = []
    for raw in (text or "").split("\n"):
        if raw == "":
            lines.append("")
        else:
            lines.extend(textwrap.wrap(raw, width) or [""])
    return lines


def _pair_lines(pair, width):
    """Rendered (kind, text) lines for one pair."""
    u, a = pair
    out = [("hdr", "❯ YOU"), ("blank", "")]
    out += [("user", l) for l in _wrap(u, width)]
    out += [("blank", ""), ("hdr", "● CLAUDE"), ("blank", "")]
    out += [("claude", l) for l in _wrap(a or "(no text reply)", width)]
    return out


def _run(stdscr, pairs):
    curses.curs_set(0)
    stdscr.keypad(True)
    cur = len(pairs) - 1   # start at the most recent pair
    scroll = 0
    search = ""
    direction = -1         # ? sets backward; / sets forward
    status = ""

    def prompt(prefix):
        h, w = stdscr.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        stdscr.addstr(h - 1, 0, " " * (w - 1))
        stdscr.addstr(h - 1, 0, prefix)
        stdscr.refresh()
        try:
            s = stdscr.getstr(h - 1, len(prefix)).decode("utf-8", "replace")
        except Exception:
            s = ""
        curses.noecho()
        curses.curs_set(0)
        return s

    def matches(idx):
        u, a = pairs[idx]
        return search.lower() in (u + "\n" + a).lower()

    def find(start, step):
        idx = start
        n = len(pairs)
        for _ in range(n):
            if 0 <= idx < n and matches(idx):
                return idx
            idx += step
            if idx < 0:
                idx = n - 1
            elif idx >= n:
                idx = 0
        return None

    while True:
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 1)
        lines = _pair_lines(pairs[cur], w - 1)
        maxscroll = max(0, len(lines) - body_h)
        scroll = max(0, min(scroll, maxscroll))

        stdscr.erase()
        for row, (kind, text) in enumerate(lines[scroll:scroll + body_h]):
            attr = curses.A_BOLD if kind == "hdr" else curses.A_NORMAL
            try:
                stdscr.addstr(row, 0, text[:w - 1], attr)
            except curses.error:
                pass
        more = " ↓more" if scroll < maxscroll else ""
        bar = status or (f"[{cur + 1}/{len(pairs)}]{more}  "
                         "j/k pair  ^D/^U scroll  ? search back  n next  q quit")
        try:
            stdscr.addstr(h - 1, 0, bar[:w - 1].ljust(w - 1), curses.A_REVERSE)
        except curses.error:
            pass
        stdscr.refresh()
        status = ""

        c = stdscr.getch()
        if c in (ord("q"), 27):                         # q / ESC
            break
        elif c in (ord("j"), curses.KEY_DOWN):
            if cur < len(pairs) - 1:
                cur += 1
                scroll = 0
        elif c in (ord("k"), curses.KEY_UP):
            if cur > 0:
                cur -= 1
                scroll = 0
        elif c == 4:                                    # Ctrl-D
            scroll += body_h // 2
        elif c == 21:                                   # Ctrl-U
            scroll -= body_h // 2
        elif c in (ord(" "), curses.KEY_NPAGE):
            scroll += body_h - 1
        elif c == curses.KEY_PPAGE:
            scroll -= body_h - 1
        elif c == ord("g"):
            scroll = 0
        elif c == ord("G"):
            cur = len(pairs) - 1
            scroll = 0
        elif c in (ord("?"), ord("/")):
            direction = -1 if c == ord("?") else 1
            s = prompt("?" if direction < 0 else "/")
            if s:
                search = s
            if search:
                m = find(cur + direction, direction)
                if m is not None:
                    cur, scroll = m, 0
                else:
                    status = f"not found: {search}"
        elif c in (ord("n"), ord("N")):
            if search:
                step = direction if c == ord("n") else -direction
                m = find(cur + step, step)
                if m is not None:
                    cur, scroll = m, 0
                else:
                    status = f"not found: {search}"


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m clauthing.history_pager <pairs.json>", file=sys.stderr)
        return 2
    try:
        pairs = json.loads(Path(argv[0]).read_text())
    except Exception as e:
        print(f"could not read pairs: {e}", file=sys.stderr)
        return 1
    if not pairs:
        print("No messages to show")
        return 0
    # Normalise to (str, str) tuples.
    pairs = [(p[0], p[1] if len(p) > 1 else "") for p in pairs]
    curses.wrapper(_run, pairs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
