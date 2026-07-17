"""Tests for run_confirm's colouring decision — which status (and therefore
colour: green=allow, yellow=ask, red=deny) each rendered line gets.

The curses render can't run headless, so we test the two pure functions it
drives every line through: classify() (segment -> status) and row_status()
(row -> the status it's painted with).
"""
from clauthing.run_confirm import _build_rows, classify, row_status, document
from clauthing.curses_markup import span, line


def _painted(commands, allow=(), deny=()):
    """Return [(line_text, status)] exactly as the render loop would colour it.
    status is None for non-coloured lines (labels/blanks)."""
    rows, segs = _build_rows(commands)
    classify(segs, list(allow), list(deny))
    return [(r["text"].strip(), row_status(r, segs)) for r in rows]


def _status_of(painted, needle):
    """Status of the first coloured line containing `needle`."""
    for text, st in painted:
        if st is not None and needle in text:
            return st
    raise AssertionError(f"no coloured line contains {needle!r}")


# ── each pipe stage is coloured independently ────────────────────────────────

def test_pipe_stages_coloured_by_their_own_rule():
    painted = _painted([{"label": "x", "command": "ls /tmp | rm -rf /x"}],
                       allow=["ls:*"], deny=["rm:*"])
    assert _status_of(painted, "ls /tmp") == "allow"     # green
    assert _status_of(painted, "rm -rf /x") == "deny"    # red


def test_unmatched_stage_is_ask():
    painted = _painted([{"label": "x", "command": "ls | curl evil.example"}],
                       allow=["ls:*"])
    assert _status_of(painted, "ls") == "allow"
    assert _status_of(painted, "curl evil.example") == "ask"   # yellow


# ── statuses are per-command, not smeared across the batch ───────────────────

def test_per_command_and_per_stage():
    painted = _painted(
        [{"label": "a", "command": "git status"},
         {"label": "b", "command": "git push | nc host 1"}],
        allow=["git status:*", "git push:*"])
    assert _status_of(painted, "git status") == "allow"
    assert _status_of(painted, "git push") == "allow"
    assert _status_of(painted, "nc host 1") == "ask"     # nc not allowed


# ── labels and blank separators are never coloured ───────────────────────────

def test_labels_and_blanks_have_no_colour():
    painted = _painted([{"label": "peek", "command": "ls"}], allow=["ls:*"])
    label = [st for text, st in painted if text == "1. peek"]
    blanks = [st for text, st in painted if text == ""]
    assert label == [None]
    assert blanks and all(st is None for st in blanks)


# ── ssh: head line AND every expanded remote line share the ssh status ───────

def test_ssh_lines_all_take_the_ssh_segments_status_ask():
    painted = _painted([{"label": "r", "command": "ssh user@router 'rm -rf /'"}])
    coloured = [(t, s) for t, s in painted if s is not None]
    # head ("ssh user@router '"), the remote "rm -rf /", and closing "'"
    assert len(coloured) >= 3
    assert all(s == "ask" for _, s in coloured)          # one unit, no rule


def test_ssh_becomes_allow_when_ssh_is_allowed():
    painted = _painted([{"label": "r", "command": "ssh user@router 'rm -rf /'"}],
                       allow=["ssh:*"])
    coloured = [s for _, s in painted if s is not None]
    assert coloured and all(s == "allow" for s in coloured)


def test_ssh_remote_operators_do_not_recolour_stages():
    # && inside the ssh quotes is part of the one ssh unit — the remote lines
    # must not be independently classified against rules.
    painted = _painted(
        [{"label": "r", "command": "ssh h 'a && rm -rf /'"}],
        deny=["rm:*"])   # a deny that would fire IF the remote were split out
    coloured = [s for _, s in painted if s is not None]
    assert all(s == "ask" for s in coloured)   # NOT deny — rm is inside ssh


# ── the add-rule -> recolour flow (classify is what recolours) ───────────────

def test_reclassify_flips_colours_when_a_rule_is_added():
    rows, segs = _build_rows([{"label": "x", "command": "ls | grep foo"}])
    classify(segs, [], [])
    assert row_status(next(r for r in rows if "ls" in r["text"]), segs) == "ask"
    # user adds Bash(ls:*): reclassify runs again
    classify(segs, ["ls:*"], [])
    assert row_status(next(r for r in rows if "grep foo" in r["text"]), segs) == "ask"
    assert row_status(next(r for r in rows if r["text"].strip() == "ls"), segs) == "allow"


def test_deny_overrides_allow_in_colour():
    painted = _painted([{"label": "x", "command": "rm -rf /x"}],
                       allow=["rm:*"], deny=["rm:*"])
    assert _status_of(painted, "rm -rf /x") == "deny"


# ── the styled document (the markup layer) — colours as inspectable data ──────

def _styled(commands, allow=(), deny=(), sel=-1):
    rows, segs = _build_rows(commands)
    classify(segs, list(allow), list(deny))
    doc = document(rows, segs, sel)
    return [(ln[0]["text"].strip(), ln[0]["style"]) for ln in doc]


def _style_of(styled, needle):
    return next(s for t, s in styled if s and needle in t)


def test_span_and_line_are_plain_data():
    assert span("hi", "green") == {"style": "green", "text": "hi"}
    assert line("a", span("b", "red")) == [
        {"style": None, "text": "a"}, {"style": "red", "text": "b"}]


def test_document_gives_each_status_a_distinct_colour():
    styled = _styled([{"label": "x", "command": "ls | rm -rf /y | curl z"}],
                     allow=["ls:*"], deny=["rm:*"])
    assert _style_of(styled, "ls") == "green"
    assert _style_of(styled, "rm -rf /y") == "red"
    assert _style_of(styled, "curl z") == "yellow bold"
    # three statuses -> three genuinely different styles
    assert len({_style_of(styled, "ls"),
                _style_of(styled, "rm -rf /y"),
                _style_of(styled, "curl z")}) == 3


def test_document_labels_and_blanks_have_no_style():
    styled = _styled([{"label": "peek", "command": "ls"}], allow=["ls:*"])
    assert ("1. peek", None) in styled
    assert ("", None) in styled


def test_document_selected_row_gets_cursor_and_bold():
    rows, segs = _build_rows([{"label": "x", "command": "ls | grep foo"}])
    classify(segs, ["ls:*"], [])
    sel = next(ri for ri, r in enumerate(rows) if r["seg"] is not None)  # the ls line
    sp = document(rows, segs, sel)[sel][0]
    assert sp["text"].startswith("> ")
    assert "bold" in sp["style"] and "green" in sp["style"]


def test_document_recolours_green_after_rule_added():
    rows, segs = _build_rows([{"label": "x", "command": "nc host 1"}])
    classify(segs, [], [])
    nc_line = lambda: document(rows, segs)[1][0]["style"]   # rows: label, seg, blank
    assert nc_line() == "yellow bold"
    classify(segs, ["nc:*"], [])                            # user adds Bash(nc:*)
    assert nc_line() == "green"


def test_add_rule_recolours_document_end_to_end(tmp_path):
    """The full press-`a` path: add_allow_rule writes to the repo-root settings
    file, load_bash_permissions re-reads it, and the document recolours the
    matching line green while leaving others yellow. This is what the dialog
    does on add-rule; it only works because write-target == re-read file."""
    import subprocess
    from clauthing import bash_perms
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    sub = tmp_path / "sub"
    sub.mkdir()
    rows, segs = _build_rows([{"label": "x", "command": "grep foo | wc -l"}])

    def recolour():                                   # == reclassify() in _run
        allow, deny = bash_perms.load_bash_permissions(None, [str(sub)])
        classify(segs, allow, deny)

    def styles():
        return {ln[0]["text"].strip(): ln[0]["style"] for ln in document(rows, segs)}

    recolour()
    assert styles()["grep foo"] == "yellow bold"

    bash_perms.add_allow_rule(str(bash_perms.repo_settings_local(sub)), "Bash(grep:*)")
    recolour()
    s = styles()
    assert s["grep foo"] == "green"        # the added rule recoloured it
    assert s["| wc -l"] == "yellow bold"   # unaffected stage stays yellow
