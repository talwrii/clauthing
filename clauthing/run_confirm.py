#!/usr/bin/env python3
"""Interactive confirm dialog for the run MCP (curses, in a tmux popup).

Shows the batch with each sub-command COLOURED by Bash-permission status
(green = already allowed, yellow = needs approval, red = denied). Scroll and
select a sub-command, then add an allow rule for it (prefilled with the exact
command, editable; you type the closing ')' yourself):
  'a' → a Bash(...) rule       (Claude's own prefix language: foo:*)
  'A' → a BetterBash(...) rule (our language: _ one arg, ... more args,
                                */** path globs)
The rule is written and the colours update live. Enter runs the batch (exit 0);
q / Esc cancels (exit 1).

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


def classify(segs, allow, deny):
    """Set each segment's status (allow/ask/deny) from the rules — what the
    colour is derived from. Mutates and returns `segs`."""
    for s in segs:
        s["status"] = bash_perms.seg_status(s["text"], allow, deny)
    return segs


def row_status(row, segs):
    """The status a row is drawn with (→ its colour): the row's own segment, or
    the parent segment for an ssh continuation line; None for labels/blanks."""
    si = row["seg"] if row["seg"] is not None else row["detail"]
    return None if si is None else segs[si]["status"]


# status -> style name (see clauthing.curses_markup)
_STATUS_STYLE = {"allow": "green", "ask": "yellow bold", "deny": "red"}


def document(rows, segs, sel_ri=-1):
    """Build the styled document for the confirm body — plain data, no curses,
    so the colouring can be printed and tested. `sel_ri` is the selected row
    index (gets a `>` cursor + bold). Each line is a single styled span."""
    from clauthing.curses_markup import span
    doc = []
    for ri, row in enumerate(rows):
        style = _STATUS_STYLE.get(row_status(row, segs))
        text = row["text"]
        if row["seg"] is not None and ri == sel_ri:
            text = "> " + text[2:]                       # cursor in left margin
            style = f"{style} bold" if style else "bold"
        doc.append([span(text, style)])
    return doc


def _paren_depth(s):
    """Net unquoted paren depth of `s` (opens minus closes), floored at -inf so
    a stray close shows negative. Quotes are ignored — good enough to detect the
    user closing the rule's own bracket."""
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return depth


def _swap_head(buf, heads):
    """If `buf` starts with one of `heads`, swap it for the other and return the
    signed change in length (to keep the cursor over the same inner char). No-op
    (returns 0) if it starts with neither."""
    s = "".join(buf)
    for a, b in (heads, heads[::-1]):
        if s.startswith(a):
            buf[:len(a)] = list(b)
            return len(b) - len(a)
    return 0


def _edit_line(stdscr, prompt, initial, submit_on_balanced=False, heads=None):
    """Bottom-line editor prefilled with `initial`. Returns text, or None (Esc).

    Scrolls horizontally so a pattern longer than the popup stays editable
    (the prefill is the full command, which is routinely wider than the box).

    With `submit_on_balanced`, the prefill opens a bracket (e.g. `Bash(ls`) and
    the editor returns the moment the user types the `)` that balances it — so
    they close the rule themselves rather than us appending it.

    With `heads=(a, b)` (e.g. `("Bash(", "BetterBash(")`), Tab flips the leading
    head between the two, and the prompt shows the current mode + the Tab hint —
    so the BetterBash option is discoverable while adding a rule.

      Tab switch rule kind    ←/→ move    ^A/Home start    ^E/End end
      Backspace / Del delete    ^U clear line    ^W delete word back
      Enter accept    Esc cancel
    """
    curses.curs_set(1)
    buf = list(initial)
    pos = len(buf)
    try:
        while True:
            h, w = stdscr.getmaxyx()
            pr = prompt
            if heads:
                s = "".join(buf)
                cur = heads[0] if s.startswith(heads[0]) else (
                    heads[1] if s.startswith(heads[1]) else "")
                other = heads[1] if cur == heads[0] else heads[0]
                mode = cur.rstrip("(") or "?"
                pr = f"{prompt}[{mode}] Tab→{other.rstrip('(')} Esc✗ "
            avail = max(1, w - 1 - len(pr))
            start = pos - avail if pos > avail else 0    # keep cursor visible
            shown = "".join(buf[start:start + avail])
            stdscr.move(h - 1, 0)
            stdscr.clrtoeol()
            try:
                stdscr.addstr(h - 1, 0, (pr + shown)[:w - 1])
                stdscr.move(h - 1, min(len(pr) + (pos - start), w - 1))
            except curses.error:
                pass
            stdscr.refresh()

            c = stdscr.getch()
            if c in (10, 13, curses.KEY_ENTER):
                return "".join(buf)
            if c == 27:
                return None
            if c == 9 and heads:                         # Tab: switch rule kind
                delta = _swap_head(buf, heads)
                pos = max(0, min(len(buf), pos + delta))
                continue
            if c == curses.KEY_LEFT:
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
                if submit_on_balanced and c == ord(")") and "(" in buf \
                        and _paren_depth(buf) == 0:
                    return "".join(buf)
    finally:
        curses.curs_set(0)


def _run(stdscr, data):
    from clauthing import curses_markup as cm
    curses.curs_set(0)
    stdscr.keypad(True)
    cm.init_styles()

    config_dir = data.get("config_dir")
    settings_file = data.get("settings_file")
    cwds = data.get("cwds") or []
    rows, segs = _build_rows(data.get("commands", []))

    def reclassify():
        allow, deny = bash_perms.load_bash_permissions(config_dir, cwds)
        classify(segs, allow, deny)
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
        doc = document(rows, segs, sel_rows[sel] if sel_rows else -1)
        cm.render(stdscr, doc, top=scroll, height=body_h)
        legend = "green=allowed  yellow=needs-approval  red=denied"
        help_ = "↑/↓ select · a add-rule (Tab: Bash/BetterBash) · Enter run · q cancel"
        try:
            stdscr.addstr(h - 2, 0, (msg or legend)[:w - 1], curses.A_DIM)
            stdscr.addstr(h - 1, 0, help_[:w - 1], curses.A_DIM)
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
            head = "BetterBash(" if c == ord("A") else "Bash("
            # Prefill the open bracket + command; the user types the closing ')'
            # themselves (submit_on_balanced returns when it balances). Tab flips
            # between Bash and BetterBash so both are discoverable here.
            rule = _edit_line(stdscr, "add allow rule ",
                              head + segs[si]["text"], submit_on_balanced=True,
                              heads=("Bash(", "BetterBash("))
            if rule and rule.strip():
                rule = rule.strip()
                try:
                    bash_perms.add_allow_rule(settings_file, rule)
                    reclassify()
                    msg = f"added {rule}"
                except Exception as e:
                    msg = f"failed to add rule: {e}"
            else:
                msg = "rule not added (cancelled)"


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
