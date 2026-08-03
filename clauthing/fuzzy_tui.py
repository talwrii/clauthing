"""Reusable fuzzy picker — a scorer, a ranker, and a curses TUI.

Shared by :msg-search and the clauthing-convos CLI. The scorer ranks CONTIGUOUS
(substring) matches above scattered subsequence matches ("best = contiguous").
"""
import curses


def fuzzy_score(query, text):
    """Return (score, positions) if `query` fuzzy-matches `text`, else None.

    Higher score = better. A contiguous substring match always beats a
    scattered subsequence match; among substrings an earlier one wins, among
    scattered matches a tighter span wins.
    """
    if not query:
        return (0.0, [])
    q, t = query.lower(), text.lower()
    idx = t.find(q)
    if idx != -1:                                   # contiguous substring — best
        return (1000.0 - idx - 0.001 * len(t), list(range(idx, idx + len(q))))
    pos, ti = [], 0                                 # scattered subsequence
    for ch in q:
        j = t.find(ch, ti)
        if j == -1:
            return None
        pos.append(j)
        ti = j + 1
    span = pos[-1] - pos[0] + 1
    gaps = span - len(q)
    return (500.0 - gaps - 0.001 * pos[0], pos)


def rank(items, query, text_of):
    """Ranked (score, item) matches for `query`, best first. Ties keep input
    order — feed items newest-first for recency tie-breaking."""
    if not query:
        return [(0.0, it) for it in items]
    scored = []
    for i, it in enumerate(items):
        r = fuzzy_score(query, text_of(it))
        if r is not None:
            scored.append((r[0], -i, it))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [(sc, it) for sc, _, it in scored]


def _run(stdscr, items, text_of, label_of, header, out_box):
    curses.curs_set(1)
    stdscr.keypad(True)
    try:
        curses.start_color()
        curses.use_default_colors()
        stdscr.bkgd(" ", curses.color_pair(0))
    except Exception:
        pass
    query, sel, top = "", 0, 0
    while True:
        results = rank(items, query, text_of)
        sel = max(0, min(sel, max(0, len(results) - 1)))
        h, w = stdscr.getmaxyx()
        body_h = max(1, h - 2)
        if sel < top:
            top = sel
        elif sel >= top + body_h:
            top = sel - body_h + 1
        top = max(0, min(top, max(0, len(results) - body_h)))

        stdscr.erase()
        for i in range(top, min(len(results), top + body_h)):
            marker = "> " if i == sel else "  "
            attr = curses.A_BOLD if i == sel else curses.A_NORMAL
            try:
                stdscr.addstr(i - top, 0, marker + label_of(results[i][1], w - 3), attr)
            except curses.error:
                pass
        count = f"{len(results)} match" + ("" if len(results) == 1 else "es")
        try:
            stdscr.addstr(h - 2, 0, count[:w - 1], curses.A_DIM)
            stdscr.addstr(h - 1, 0, (header + ": " + query)[:w - 1])
            stdscr.move(h - 1, min(len(header) + 2 + len(query), w - 1))
        except curses.error:
            pass
        stdscr.refresh()

        c = stdscr.getch()
        if c == 27:                                 # Esc
            return False
        if c in (10, 13, curses.KEY_ENTER):
            if results:
                out_box.append(results[sel][1])
                return True
            return False
        elif c in (curses.KEY_DOWN, 14):            # ↓ / Ctrl-N
            sel += 1
        elif c in (curses.KEY_UP, 16):              # ↑ / Ctrl-P
            sel -= 1
        elif c == curses.KEY_NPAGE:
            sel += body_h - 1
        elif c == curses.KEY_PPAGE:
            sel -= body_h - 1
        elif c in (curses.KEY_BACKSPACE, 127, 8):
            query, sel = query[:-1], 0
        elif c == 21:                               # Ctrl-U clear
            query, sel = "", 0
        elif 32 <= c < 127:
            query, sel = query + chr(c), 0


def pick(items, text_of, label_of, header="search"):
    """Run the fuzzy picker over `items`; return the selected item or None.

    text_of(item)  -> the string searched. label_of(item, width) -> the row.
    """
    box = []
    if curses.wrapper(_run, items, text_of, label_of, header, box):
        return box[0]
    return None
