#!/usr/bin/env python3
"""Interactive confirm dialog for the run MCP (curses, in a tmux popup).

Shows the batch with each sub-command COLOURED by Bash-permission status
(green = already allowed, yellow = needs approval, red = denied). Scroll and
select a sub-command; press 'a' to add a Bash(...) allow rule for it (prefilled
with the exact command, editable) — the rule is written and the colours update
live. Enter runs the batch (exit 0); q / Esc cancels (exit 1).

Input JSON (argv[1]):
  {"config_dir", "settings_file", "cwds": [...],
   "commands": [{"label", "command"}]}
"""
import curses
import json
import sys
from pathlib import Path

from clauthing import bash_perms

BASE = "     "
INNER = BASE + "    "


def _build_rows(commands):
    """Flatten commands into display rows + selectable segments.

    rows: list of {'text', 'seg', 'detail'} where 'seg' is the index of a
    selectable sub-command (its head line) or None, and 'detail' is the parent
    segment index for ssh continuation lines (coloured, not selectable).
    segs: list of {'text'} (the sub-command matched against rules).
    """
    rows, segs = [], []
    for i, c in enumerate(commands, 1):
        rows.append({"text": f"{i}. {c.get('label', '')}".rstrip(),
                     "seg": None, "detail": None})
        for op, seg in bash_perms.split_bash(c.get("command", "")):
            si = len(segs)
            segs.append({"text": seg, "status": "ask"})
            lead = BASE + (f"{op} " if op else "")
            ssh = bash_perms.ssh_remote(seg)
            if not ssh:
                rows.append({"text": lead + seg, "seg": si, "detail": None})
            else:
                prefix, quote, remote, suffix = ssh
                rows.append({"text": f"{lead}{prefix} {quote}", "seg": si, "detail": None})
                for rop, rseg in bash_perms.split_bash(remote):
                    rows.append({"text": INNER + (f"{rop} {rseg}" if rop else rseg),
                                 "seg": None, "detail": si})
                rows.append({"text": BASE + quote + suffix, "seg": None, "detail": si})
        rows.append({"text": "", "seg": None, "detail": None})
    return rows, segs


def _edit_line(stdscr, prompt, initial):
    """Bottom-line editor prefilled with `initial`. Returns text, or None (Esc).

    Scrolls horizontally so a pattern longer than the popup stays editable
    (the prefill is the full command, which is routinely wider than the box).

      ←/→ move    ^A/Home start    ^E/End end
      Backspace / Del delete    ^U clear line    ^W delete word back
      Enter accept    Esc cancel
    """
    curses.curs_set(1)
    buf = list(initial)
    pos = len(buf)
    try:
        while True:
            h, w = stdscr.getmaxyx()
            avail = max(1, w - 1 - len(prompt))
            start = pos - avail if pos > avail else 0    # keep cursor visible
            shown = "".join(buf[start:start + avail])
            stdscr.move(h - 1, 0)
            stdscr.clrtoeol()
            try:
                stdscr.addstr(h - 1, 0, (prompt + shown)[:w - 1])
                stdscr.move(h - 1, min(len(prompt) + (pos - start), w - 1))
            except curses.error:
                pass
            stdscr.refresh()

            c = stdscr.getch()
            if c in (10, 13, curses.KEY_ENTER):
                return "".join(buf)
            if c == 27:
                return None
            elif c == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif c == curses.KEY_RIGHT:
                pos = min(len(buf), pos + 1)
            elif c in (curses.KEY_HOME, 1):              # ^A
                pos = 0
            elif c in (curses.KEY_END, 5):               # ^E
                pos = len(buf)
            elif c in (curses.KEY_BACKSPACE, 127, 8):
                if pos > 0:
                    del buf[pos - 1]
                    pos -= 1
            elif c == curses.KEY_DC:                     # Delete
                if pos < len(buf):
                    del buf[pos]
            elif c == 21:                                # ^U clear
                buf, pos = [], 0
            elif c == 23:                                # ^W delete word back
                while pos > 0 and buf[pos - 1] == " ":
                    del buf[pos - 1]
                    pos -= 1
                while pos > 0 and buf[pos - 1] != " ":
                    del buf[pos - 1]
                    pos -= 1
            elif 32 <= c < 127:
                buf.insert(pos, chr(c))
                pos += 1
    finally:
        curses.curs_set(0)


def _run(stdscr, data):
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    colour = {"deny": curses.color_pair(1),
              "allow": curses.color_pair(2),
              "ask": curses.color_pair(3) | curses.A_BOLD}

    config_dir = data.get("config_dir")
    settings_file = data.get("settings_file")
    cwds = data.get("cwds") or []
    rows, segs = _build_rows(data.get("commands", []))

    def reclassify():
        allow, deny = bash_perms.load_bash_permissions(config_dir, cwds)
        for s in segs:
            s["status"] = bash_perms.seg_status(s["text"], allow, deny)
    reclassify()

    sel_rows = [ri for ri, r in enumerate(rows) if r["seg"] is not None]
    sel = 0
    scroll = 0
    msg = ""

    while True:
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 2)
        if sel_rows:
            srow = sel_rows[sel]
            if srow < scroll:
                scroll = srow
            elif srow >= scroll + body_h:
                scroll = srow - body_h + 1
        scroll = max(0, min(scroll, max(0, len(rows) - body_h)))

        stdscr.erase()
        for ri in range(scroll, min(len(rows), scroll + body_h)):
            row = rows[ri]
            si = row["seg"] if row["seg"] is not None else row["detail"]
            attr = colour.get(segs[si]["status"], curses.A_NORMAL) if si is not None else curses.A_NORMAL
            if row["seg"] is not None and sel_rows and ri == sel_rows[sel]:
                attr |= curses.A_REVERSE
            try:
                stdscr.addstr(ri - scroll, 0, row["text"][:w - 1], attr)
            except curses.error:
                pass
        legend = "green=allowed  yellow=needs-approval  red=denied"
        help_ = "↑/↓ select   a add-rule   Enter run   q cancel"
        try:
            stdscr.addstr(h - 2, 0, (msg or legend)[:w - 1].ljust(w - 1), curses.A_DIM)
            stdscr.addstr(h - 1, 0, help_[:w - 1].ljust(w - 1), curses.A_REVERSE)
        except curses.error:
            pass
        stdscr.refresh()
        msg = ""

        c = stdscr.getch()
        if c in (ord("q"), 27):
            return False
        if c in (curses.KEY_ENTER, 10, 13):
            return True
        elif c in (ord("j"), curses.KEY_DOWN):
            if sel_rows:
                sel = min(sel + 1, len(sel_rows) - 1)
        elif c in (ord("k"), curses.KEY_UP):
            if sel_rows:
                sel = max(sel - 1, 0)
        elif c in (ord(" "), curses.KEY_NPAGE):
            scroll += body_h - 1
        elif c in (ord("b"), curses.KEY_PPAGE):
            scroll -= body_h - 1
        elif c in (ord("a"), ord("A")) and sel_rows:
            si = rows[sel_rows[sel]]["seg"]
            inner = _edit_line(stdscr, "add allow rule → Bash(", segs[si]["text"])
            if inner and inner.strip():
                rule = f"Bash({inner.strip()})"
                try:
                    bash_perms.add_allow_rule(settings_file, rule)
                    reclassify()
                    msg = f"added {rule}"
                except Exception as e:
                    msg = f"failed to add rule: {e}"


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m clauthing.run_confirm <json>", file=sys.stderr)
        return 2
    try:
        data = json.loads(Path(argv[0]).read_text())
    except Exception as e:
        print(f"could not read: {e}", file=sys.stderr)
        return 2
    return 0 if curses.wrapper(_run, data) else 1


if __name__ == "__main__":
    sys.exit(main())
