#!/usr/bin/env python3
"""Curses confirmation pager: show text scrollably in a tmux popup and let the
user confirm or cancel — no external pager, so scroll and confirm live in one
prompt.

  ↑/k  ↓/j        scroll one line
  space / PgDn    page down        b / PgUp   page up
  Ctrl-D/Ctrl-U   half page        g / G      top / bottom
  Enter           confirm  (exit 0)
  q / Esc         cancel   (exit 1)

Invoked as:  python -m clauthing.confirm_pager <text-file>
Run inside a tmux display-popup by confirm_popup().
"""
import curses
import sys
from pathlib import Path


def _run(stdscr, lines):
    curses.curs_set(0)
    stdscr.keypad(True)
    scroll = 0
    while True:
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 1)
        maxscroll = max(0, len(lines) - body_h)
        scroll = max(0, min(scroll, maxscroll))

        stdscr.erase()
        for row, text in enumerate(lines[scroll:scroll + body_h]):
            try:
                stdscr.addstr(row, 0, text[:w - 1])
            except curses.error:
                pass
        more = " ↓more" if scroll < maxscroll else ""
        up = " ↑more" if scroll > 0 else ""
        bar = f"[Enter] confirm  [q] cancel  ↑/↓/space scroll{up}{more}"
        try:
            stdscr.addstr(h - 1, 0, bar[:w - 1].ljust(w - 1), curses.A_REVERSE)
        except curses.error:
            pass
        stdscr.refresh()

        c = stdscr.getch()
        if c in (ord("q"), 27):                 # q / Esc
            return False
        if c in (curses.KEY_ENTER, 10, 13):     # Enter
            return True
        elif c in (ord("j"), curses.KEY_DOWN):
            scroll += 1
        elif c in (ord("k"), curses.KEY_UP):
            scroll -= 1
        elif c in (ord(" "), curses.KEY_NPAGE):
            scroll += body_h - 1
        elif c in (ord("b"), curses.KEY_PPAGE):
            scroll -= body_h - 1
        elif c == 4:                            # Ctrl-D
            scroll += body_h // 2
        elif c == 21:                           # Ctrl-U
            scroll -= body_h // 2
        elif c == ord("g"):
            scroll = 0
        elif c == ord("G"):
            scroll = maxscroll


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m clauthing.confirm_pager <text-file>", file=sys.stderr)
        return 2
    try:
        text = Path(argv[0]).read_text()
    except Exception as e:
        print(f"could not read text: {e}", file=sys.stderr)
        return 2
    confirmed = curses.wrapper(_run, text.split("\n"))
    return 0 if confirmed else 1


if __name__ == "__main__":
    sys.exit(main())
