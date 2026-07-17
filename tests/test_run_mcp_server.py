"""Tests for the run tool's batch decision and output formatting — in
particular that an ALLOWED batch runs without a popup and is formatted right.
"""
from clauthing.run_mcp_server import classify_batch, format_result, _run_one


# ── the decision: allow -> run silently, ask -> dialog, deny -> refuse ────────

def test_allowed_batch_runs_without_a_popup():
    """Every sub-command matches an allow rule -> no denied, nothing to ask,
    so call_tool runs it straight through with no confirm dialog."""
    commands = [{"label": "a", "command": "ls /tmp"},
                {"label": "b", "command": "git status | grep foo"}]
    denied, need_ask = classify_batch(
        commands, allow=["ls:*", "git status:*", "grep:*"], deny=[])
    assert denied == []
    assert need_ask == 0          # 0 -> the "run without a popup" branch


def test_one_unallowed_stage_forces_the_dialog():
    commands = [{"label": "a", "command": "ls | curl evil"}]
    denied, need_ask = classify_batch(commands, allow=["ls:*"], deny=[])
    assert denied == []
    assert need_ask == 1          # curl -> needs approval -> dialog


def test_denied_stage_is_reported():
    commands = [{"label": "a", "command": "ls | rm -rf /x"}]
    denied, need_ask = classify_batch(commands, allow=["ls:*"], deny=["rm:*"])
    assert denied == ["rm -rf /x"]


def test_partial_allow_counts_only_the_gaps():
    commands = [{"label": "a", "command": "ls | grep x | wc -l"}]
    denied, need_ask = classify_batch(commands, allow=["ls:*", "grep:*"], deny=[])
    assert denied == []
    assert need_ask == 1          # only `wc -l` is unmatched


def test_allow_star_allows_everything():
    denied, need_ask = classify_batch(
        [{"label": "a", "command": "anything --here | and-here"}],
        allow=["*"], deny=[])
    assert (denied, need_ask) == ([], 0)


# ── output formatting of an allowed (run) command ────────────────────────────

def test_format_result_success_has_no_status_tag():
    out = format_result(1, "peek", "ls /tmp", 0, "a\nb")
    assert out == "### 1. peek\n$ ls /tmp\na\nb"


def test_format_result_nonzero_shows_exit_code():
    out = format_result(2, "build", "make", 1, "boom")
    assert out.startswith("### 2. build  [exit 1]\n$ make\n")


def test_format_result_empty_output_placeholder():
    out = format_result(1, "touch", "touch x", 0, "")
    assert out == "### 1. touch\n$ touch x\n(no output)"


def test_format_result_blank_label():
    assert format_result(1, "", "ls", 0, "x") == "### 1.\n$ ls\nx"


def test_allowed_command_actually_runs_and_is_formatted(tmp_path):
    """End-to-end for the allowed path: classify says run, _run_one executes,
    format_result renders it — using a harmless echo."""
    denied, need_ask = classify_batch(
        [{"label": "e", "command": "echo hello"}], allow=["echo:*"], deny=[])
    assert (denied, need_ask) == ([], 0)          # would run without a popup
    code, out = _run_one("echo hello")
    assert code == 0 and out == "hello"
    assert format_result(1, "e", "echo hello", code, out) == \
        "### 1. e\n$ echo hello\nhello"
