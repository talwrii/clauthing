"""A tiny styled-document layer for curses screens.

The idea: build what you want to show as plain data, then let one dumb renderer
paint it. That keeps colour/style decisions out of the curses loop, so they can
be printed, snapshotted, and unit-tested without a terminal.

  document  = list of lines
  line      = list of spans
  span      = {"style": <name or None>, "text": <str>}

`style` is a name, or a space/`+`-joined combo:
  colours   red green yellow cyan magenta blue
  attrs     bold dim reverse underline
e.g. {"style": "green", "text": "ls"} or {"style": "yellow bold", "text": "!"}.
"""
import curses

_COLORS = {"red": 1, "green": 2, "yellow": 3, "cyan": 4, "magenta": 5, "blue": 6}
_ATTRS = {"bold": "A_BOLD", "dim": "A_DIM", "reverse": "A_REVERSE",
          "underline": "A_UNDERLINE"}


def span(text, style=None):
    return {"style": style, "text": str(text)}


def line(*parts):
    """A line from spans and/or bare strings (bare strings are unstyled)."""
    return [p if isinstance(p, dict) else span(p) for p in parts]


def init_styles():
    """Call once inside curses (after curses.wrapper starts). Maps colour names
    to pairs over the terminal's own background. Safe if colour is unsupported."""
    try:
        curses.start_color()
        curses.use_default_colors()
        for name, idx in _COLORS.items():
            curses.init_pair(idx, getattr(curses, f"COLOR_{name.upper()}"), -1)
    except Exception:
        pass


def attr(style):
    """Resolve a style name/combo to a curses attribute."""
    if not style:
        return curses.A_NORMAL
    a = curses.A_NORMAL
    for tok in str(style).replace("+", " ").split():
        if tok in _COLORS:
            try:
                a |= curses.color_pair(_COLORS[tok])
            except Exception:
                pass
        elif tok in _ATTRS:
            a |= getattr(curses, _ATTRS[tok])
    return a


def render(stdscr, doc, top=0, height=None, y0=0):
    """Draw `doc` — the window [top, top+height) of its lines — onto the screen
    starting at row `y0`. Clips to the screen width; never raises on overflow."""
    h, w = stdscr.getmaxyx()
    height = (h - y0) if height is None else height
    for i, ln in enumerate(doc[top:top + height]):
        y, x = y0 + i, 0
        for sp in ln:
            if x >= w - 1:
                break
            text = str(sp.get("text", ""))[:max(0, w - 1 - x)]
            if not text:
                continue
            try:
                stdscr.addstr(y, x, text, attr(sp.get("style")))
            except curses.error:
                pass
            x += len(text)
