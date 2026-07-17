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


def _prompt_reason(stdscr):
    """Bottom-line prompt for a decline reason. Returns the text (may be "")."""
    curses.curs_set(1)
    buf = []
    try:
        while True:
            h, w = stdscr.getmaxyx()
            prompt = "reason (Enter to send, Esc to skip): "
            stdscr.move(h - 1, 0)
            stdscr.clrtoeol()
            try:
                stdscr.addstr(h - 1, 0, (prompt + "".join(buf))[:w - 1])
            except curses.error:
                pass
            stdscr.refresh()
            c = stdscr.getch()
            if c in (10, 13, curses.KEY_ENTER):
                return "".join(buf).strip()
            if c == 27:
                return ""
            if c in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
            elif 32 <= c < 127:
                buf.append(chr(c))
    finally:
        curses.curs_set(0)


def _run(stdscr, lines, reason_box=None):
    curses.curs_set(0)
    stdscr.keypad(True)
    # Inherit the terminal's colours (start_color, called by curses.wrapper,
    # otherwise imposes a white-on-black default that paints the background).
    try:
        curses.start_color()
        curses.use_default_colors()
        stdscr.bkgd(" ", curses.color_pair(0))
    except Exception:
        pass
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
        bar = f"[Enter] confirm  [q] cancel+reason  [Q] cancel  scroll{up}{more}"
        try:
            stdscr.addstr(h - 1, 0, bar[:w - 1], curses.A_DIM)
        except curses.error:
            pass
        stdscr.refresh()

        c = stdscr.getch()
        if c == ord("q"):                       # cancel WITH a reason
            reason = _prompt_reason(stdscr)
            if reason and reason_box is not None:
                reason_box.append(reason)
            return False
        if c in (ord("Q"), 27):                 # Q / Esc — just cancel
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
        print("usage: python -m clauthing.confirm_pager <text-file> [reason-file]",
              file=sys.stderr)
        return 2
    try:
        text = Path(argv[0]).read_text()
    except Exception as e:
        print(f"could not read text: {e}", file=sys.stderr)
        return 2
    reason_box = []
    confirmed = curses.wrapper(_run, text.split("\n"), reason_box)
    # A cancel-with-reason (q) writes the reason to the reason-file for the
    # parent (confirm_popup) to read back.
    if not confirmed and reason_box and len(argv) > 1:
        try:
            Path(argv[1]).write_text(reason_box[0])
        except Exception:
            pass
    return 0 if confirmed else 1


if __name__ == "__main__":
    sys.exit(main())
