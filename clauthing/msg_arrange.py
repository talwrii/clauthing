#!/usr/bin/env python3
"""Curses UI to rearrange a window's message inbox.

:msg reads the oldest-unread message by (priority, ts), so this reorders the
list and, on save, writes `priority = position` — making :msg hand them back in
the order you arranged.

Shows the unread queue by default; `a` toggles the already-read ("done")
messages into view, dimmed below the queue, so old ones are still reachable.
Only unread messages are reorderable — reordering done ones is meaningless
since :msg skips them.

  j / ↓   k / ↑      move the cursor
  J        K          move the selected message DOWN / UP (unread only)
  a                   toggle showing done messages
  s / Enter           save the new order
  q / Esc             cancel (no change)

Invoked:  python -m clauthing.msg_arrange <inbox.jsonl>
Exit 0 = saved, 1 = cancelled.
"""
import curses
import json
import sys
import time
from pathlib import Path


def _load(path):
    msgs = []
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    # Start in :msg processing order.
    msgs.sort(key=lambda m: (m.get("priority", 9999), m.get("ts", 0)))
    return msgs


def _label(m, width):
    ts = m.get("ts", 0)
    t = time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"
    body = " ".join((m.get("message", "") or "").split())
    return f"[{t}] {m.get('from', '?')}: {body}"[:width]


def _run(stdscr, msgs, done, path):
    curses.curs_set(0)
    stdscr.keypad(True)
    # Inherit the terminal's own colours. curses.wrapper calls start_color(),
    # which makes pair 0 white-on-black and fills the window with it;
    # use_default_colors() maps the default pair back to the terminal's own
    # fg/bg so the popup doesn't render with an imposed background.
    try:
        curses.start_color()
        curses.use_default_colors()
        stdscr.bkgd(" ", curses.color_pair(0))
    except Exception:
        pass
    show_all = not msgs        # nothing unread? then start on the full history
    sel, top, status = 0, 0, ""
    while True:
        # Unread first, then (optionally) the done ones — so index < len(msgs)
        # always means "an unread message", which is what's reorderable.
        visible = msgs + done if show_all else msgs
        sel = max(0, min(sel, max(0, len(visible) - 1)))
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 2)
        if sel < top:
            top = sel
        elif sel >= top + body_h:
            top = sel - body_h + 1
        top = max(0, min(top, max(0, len(visible) - body_h)))

        stdscr.erase()
        for i in range(top, min(len(visible), top + body_h)):
            marker = "> " if i == sel else "  "
            if i == sel:
                attr = curses.A_BOLD
            elif i >= len(msgs):          # a done message
                attr = curses.A_DIM
            else:
                attr = curses.A_NORMAL
            try:
                stdscr.addstr(i - top, 0,
                              marker + f"{i + 1:2}. " + _label(visible[i], w - 8), attr)
            except curses.error:
                pass
        help_ = status or "j/k cursor · J/K move · a all · s save · q cancel"
        count = f"{len(msgs)} unread" + (f" · {len(done)} done" if show_all else "")
        try:
            stdscr.addstr(h - 2, 0, help_[:w - 1], curses.A_DIM)
            stdscr.addstr(h - 1, 0, count[:w - 1], curses.A_DIM)
        except curses.error:
            pass
        stdscr.refresh()
        status = ""

        c = stdscr.getch()
        if c in (ord("q"), 27):
            return False
        if c in (ord("s"), 10, 13, curses.KEY_ENTER):
            for i, m in enumerate(msgs):
                m["priority"] = i
            # Write back the reordered unread PLUS the read ones untouched, so
            # saving never drops messages we chose not to display.
            Path(path).write_text(
                "".join(json.dumps(m) + "\n" for m in msgs + done))
            return True
        elif c in (ord("a"), ord("A")):
            show_all = not show_all
            status = "showing all" if show_all else "showing unread only"
        elif c in (ord("j"), curses.KEY_DOWN):
            sel = min(sel + 1, len(visible) - 1)
        elif c in (ord("k"), curses.KEY_UP):
            sel = max(sel - 1, 0)
        elif c == ord("J") and sel < len(msgs) - 1:      # reorder unread only
            msgs[sel], msgs[sel + 1] = msgs[sel + 1], msgs[sel]
            sel += 1
            status = "moved down"
        elif c == ord("K") and 0 < sel < len(msgs):
            msgs[sel], msgs[sel - 1] = msgs[sel - 1], msgs[sel]
            sel -= 1
            status = "moved up"


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m clauthing.msg_arrange <inbox.jsonl>", file=sys.stderr)
        return 2
    all_msgs = _load(argv[0])
    pending = [m for m in all_msgs if not m.get("read")]   # the queue
    done = [m for m in all_msgs if m.get("read")]          # hidden until 'a'
    if not all_msgs:
        print("No messages")
        return 0
    return 0 if curses.wrapper(_run, pending, done, argv[0]) else 1


if __name__ == "__main__":
    sys.exit(main())
