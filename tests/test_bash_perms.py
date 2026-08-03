"""Tests for bash_perms: command splitting, ssh expansion, and the Bash(...)
permission rules the run tool honours — including git-repo-root resolution and
the add-rule -> live-recolour flow in the run_confirm dialog.
"""
import json
import subprocess

import pytest

from clauthing import bash_perms
from clauthing.run_confirm import _build_rows


# ── splitting (display only — never executed) ────────────────────────────────

def test_split_on_pipe():
    assert bash_perms.split_bash("ls /tmp | grep foo") == [
        ("", "ls /tmp"), ("|", "grep foo")]


def test_split_on_and_and():
    assert bash_perms.split_bash("cd /x && ls") == [("", "cd /x"), ("&&", "ls")]


def test_split_respects_quotes():
    """Operators inside quotes are part of the command, not separators."""
    assert bash_perms.split_bash("echo 'a && b || c' | cat") == [
        ("", "echo 'a && b || c'"), ("|", "cat")]


def test_split_respects_subshell():
    out = bash_perms.split_bash('cd /x && "$(which python)" -c \'print(1)\'')
    assert out[0] == ("", "cd /x")
    assert out[1][0] == "&&"
    assert "$(which python)" in out[1][1]


def test_split_single_command():
    assert bash_perms.split_bash("ls") == [("", "ls")]


# ── ssh expansion ────────────────────────────────────────────────────────────

def test_ssh_remote_extracts_remote_command():
    prefix, quote, remote, suffix = bash_perms.ssh_remote("ssh user@router 'a && b'")
    assert prefix == "ssh user@router"
    assert quote == "'"
    assert remote == "a && b"
    assert suffix == ""


def test_ssh_remote_none_for_non_ssh():
    assert bash_perms.ssh_remote("ls -la") is None


# ── rule matching ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seg,pattern,expected", [
    ("git status -s", "git:*", True),
    ("git", "git:*", True),
    ("gitfoo", "git:*", False),      # :* enforces a word boundary
    ("ls", "ls", True),
    ("ls -la", "ls", False),         # exact rule: args must not match
    ("anything at all", "*", True),
])
def test_rule_matches(seg, pattern, expected):
    assert bash_perms.rule_matches(seg, pattern) is expected


def test_seg_status_deny_beats_allow():
    assert bash_perms.seg_status("rm -rf /", ["rm:*"], ["rm:*"]) == "deny"


def test_seg_status_allow_and_ask():
    assert bash_perms.seg_status("git log", ["git:*"], []) == "allow"
    assert bash_perms.seg_status("curl x", ["git:*"], []) == "ask"


# ── reading rules out of settings files ──────────────────────────────────────

def test_parse_bash_rule():
    assert bash_perms.parse_bash_rule("Bash(git status:*)") == "git status:*"
    assert bash_perms.parse_bash_rule("Read") is None          # non-Bash ignored


def test_bare_bash_rule_means_all_commands():
    # `Bash` (whole tool) is how Claude Code allows/denies every bash command.
    assert bash_perms.parse_bash_rule("Bash") == "*"
    assert bash_perms.parse_bash_rule("Bash()") == "*"


def test_whole_tool_allow_applies_to_any_command(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"permissions": {"allow": ["Bash"]}}))
    allow, deny = bash_perms.load_bash_permissions(None, files=[f])
    assert allow == [("bash", "*")]
    assert bash_perms.seg_status("anything --at all", allow, deny) == "allow"
    assert bash_perms.seg_status("ssh user@router 'x'", allow, deny) == "allow"


def test_whole_tool_deny_blocks_any_command(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"permissions": {"deny": ["Bash"]}}))
    allow, deny = bash_perms.load_bash_permissions(None, files=[f])
    assert bash_perms.seg_status("ls", allow, deny) == "deny"


def test_load_reads_allow_and_deny(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"permissions": {
        "allow": ["Bash(ls:*)", "Read"], "deny": ["Bash(rm:*)"]}}))
    allow, deny = bash_perms.load_bash_permissions(None, files=[f])
    assert allow == [("bash", "ls:*")]   # the Read rule is not a Bash rule
    assert deny == [("bash", "rm:*")]


def test_load_ignores_missing_and_malformed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    allow, deny = bash_perms.load_bash_permissions(
        None, files=[bad, tmp_path / "missing.json"])
    assert (allow, deny) == ([], [])


# ── git-repo-root resolution (the thing Claude actually does) ────────────────

def _git_repo(path):
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    return path


def test_git_repo_root_walks_up_to_the_ancestor_with_dot_git(tmp_path):
    repo = _git_repo(tmp_path)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert bash_perms.git_repo_root(sub) == repo


def test_git_repo_root_falls_back_outside_a_repo(tmp_path):
    assert bash_perms.git_repo_root(tmp_path) == tmp_path


def test_load_finds_rules_at_repo_root_from_a_subdir(tmp_path):
    """A rule at <repo-root>/.claude must be found from a subdirectory —
    this is what :permissions got wrong by using the literal cwd."""
    repo = _git_repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))
    allow, _ = bash_perms.load_bash_permissions(None, [str(sub)])
    assert ("bash", "ls:*") in allow


def test_repo_settings_local_points_at_repo_root(tmp_path):
    repo = _git_repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()
    assert bash_perms.repo_settings_local(sub) == repo / ".claude" / "settings.local.json"


# ── add rule -> the live recolour in run_confirm ─────────────────────────────

def test_add_rule_flips_status_which_drives_the_recolour(tmp_path):
    """Pressing 'a' writes the rule; reclassify() re-reads and the segment goes
    ask -> allow. That status flip is what repaints the line green."""
    repo = _git_repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()
    target = bash_perms.repo_settings_local(sub)   # exactly where run_confirm writes

    def status():                                   # exactly what reclassify() does
        allow, deny = bash_perms.load_bash_permissions(None, [str(sub)])
        return bash_perms.seg_status("ls /tmp", allow, deny)

    assert status() == "ask"
    bash_perms.add_allow_rule(target, "Bash(ls:*)")
    assert status() == "allow"


def test_a_broad_rule_recolours_several_segments(tmp_path):
    repo = _git_repo(tmp_path)
    target = bash_perms.repo_settings_local(repo)
    bash_perms.add_allow_rule(target, "Bash(git:*)")
    allow, deny = bash_perms.load_bash_permissions(None, [str(repo)])
    assert bash_perms.seg_status("git status", allow, deny) == "allow"
    assert bash_perms.seg_status("git log -1", allow, deny) == "allow"
    assert bash_perms.seg_status("curl x", allow, deny) == "ask"


# ── BetterBash pattern language ──────────────────────────────────────────────

def test_parse_rule_distinguishes_bash_and_betterbash():
    assert bash_perms.parse_rule("Bash(ls:*)") == ("bash", "ls:*")
    assert bash_perms.parse_rule("Bash") == ("bash", "*")
    assert bash_perms.parse_rule("BetterBash(git ...)") == ("better", "git ...")
    assert bash_perms.parse_rule("Read(x)") is None


def test_better_match_exact():
    assert bash_perms.better_match("git status", "git status")
    assert not bash_perms.better_match("git status", "git status --short")
    assert not bash_perms.better_match("git status", "git")


def test_better_match_ellipsis_is_zero_or_more_args():
    assert bash_perms.better_match("git ...", "git")
    assert bash_perms.better_match("git ...", "git status")
    assert bash_perms.better_match("git ...", "git log -1 --oneline")


def test_better_match_underscore_is_exactly_one_arg():
    assert bash_perms.better_match("git _", "git status")
    assert not bash_perms.better_match("git _", "git")
    assert not bash_perms.better_match("git _", "git log -1")


def test_better_match_single_star_stays_in_one_path_segment():
    assert bash_perms.better_match("ls ~/mine/*", "ls ~/mine/clauthing")
    assert not bash_perms.better_match("ls ~/mine/*", "ls ~/mine/a/b")


def test_better_match_double_star_crosses_path_segments():
    assert bash_perms.better_match("cat ~/mine/**", "cat ~/mine/a/b/c.txt")
    assert bash_perms.better_match("cat ~/mine/**", "cat ~/mine/x")


def test_better_match_underscore_then_ellipsis():
    assert bash_perms.better_match("git _ ...", "git commit -m hi")
    assert not bash_perms.better_match("git _ ...", "git")


def test_seg_status_uses_betterbash_rules():
    allow = [("better", "git ...")]
    assert bash_perms.seg_status("git push --force", allow, []) == "allow"
    assert bash_perms.seg_status("rm -rf /", allow, []) == "ask"


def test_betterbash_deny_beats_allow():
    allow = [("better", "git ...")]
    deny = [("better", "git push ...")]
    assert bash_perms.seg_status("git status", allow, deny) == "allow"
    assert bash_perms.seg_status("git push origin", allow, deny) == "deny"


def test_add_allow_rule_is_idempotent(tmp_path):
    f = tmp_path / "settings.local.json"
    bash_perms.add_allow_rule(f, "Bash(ls:*)")
    bash_perms.add_allow_rule(f, "Bash(ls:*)")
    assert json.loads(f.read_text())["permissions"]["allow"] == ["Bash(ls:*)"]


def test_add_allow_rule_preserves_other_settings(tmp_path):
    f = tmp_path / "settings.local.json"
    f.write_text(json.dumps({"permissions": {"allow": ["Bash(git:*)"]},
                             "hooks": {"PreToolUse": []}}))
    bash_perms.add_allow_rule(f, "Bash(ls:*)")
    data = json.loads(f.read_text())
    assert data["permissions"]["allow"] == ["Bash(git:*)", "Bash(ls:*)"]
    assert data["hooks"] == {"PreToolUse": []}     # untouched


# ── run_confirm row building ─────────────────────────────────────────────────

def test_build_rows_makes_each_subcommand_selectable():
    rows, segs = _build_rows([{"label": "peek", "command": "ls /tmp | grep foo"}])
    assert [s["text"] for s in segs] == ["ls /tmp", "grep foo"]
    assert len([r for r in rows if r["seg"] is not None]) == 2


def test_build_rows_expands_ssh_as_non_selectable_detail():
    rows, segs = _build_rows([{"label": "r", "command": "ssh h 'a && b'"}])
    # the whole ssh command is the one selectable/permissioned unit …
    assert [s["text"] for s in segs] == ["ssh h 'a && b'"]
    # … and its remote command is shown as detail rows attached to it
    assert any(r["detail"] is not None for r in rows)
