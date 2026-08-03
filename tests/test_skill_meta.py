"""Skill frontmatter parsing + tag editing.

Tags live in each SKILL.md's leading --- block. These pin the parse/edit round
trip and the tag grouping that :skills / :skilltag rely on.
"""
from clauthing import skill_meta as sm


def _skill(tmp_path, name, text):
    d = tmp_path / name
    d.mkdir()
    (d / "SKILL.md").write_text(text)
    return d


# ── parsing ──────────────────────────────────────────────────────────────────

def test_parse_description_and_no_tags():
    meta = sm.parse_meta("---\ndescription: does a thing\n---\nbody\n")
    assert meta["description"] == "does a thing"
    assert meta["tags"] == []


def test_parse_inline_comma_tags():
    assert sm.parse_meta("---\ntags: alive, health\n---\n")["tags"] == ["alive", "health"]


def test_parse_inline_list_tags():
    assert sm.parse_meta("---\ntags: [alive, health]\n---\n")["tags"] == ["alive", "health"]


def test_parse_block_list_tags():
    text = "---\ndescription: x\ntags:\n  - alive\n  - health\n---\nbody"
    meta = sm.parse_meta(text)
    assert meta["tags"] == ["alive", "health"]
    assert meta["description"] == "x"


def test_parse_dedupes_tags():
    assert sm.parse_meta("---\ntags: a, a, b\n---\n")["tags"] == ["a", "b"]


def test_parse_no_frontmatter():
    assert sm.parse_meta("just a body, no ---\n")["tags"] == []


def test_unterminated_frontmatter_is_not_frontmatter():
    assert sm.parse_meta("---\ntags: a\nno closing fence\n")["tags"] == []


# ── editing preserves everything else ────────────────────────────────────────

def test_set_tags_roundtrip():
    text = "---\ndescription: keep me\ntags: old\n---\nthe body\n"
    out = sm.set_tags_text(text, ["new", "shiny"])
    meta = sm.parse_meta(out)
    assert meta["tags"] == ["new", "shiny"]
    assert meta["description"] == "keep me"          # untouched
    assert "the body" in out


def test_set_tags_adds_line_when_absent():
    text = "---\ndescription: x\n---\nbody\n"
    out = sm.set_tags_text(text, ["a"])
    assert sm.parse_meta(out)["tags"] == ["a"]
    assert sm.parse_meta(out)["description"] == "x"


def test_set_tags_replaces_block_list():
    text = "---\ntags:\n  - a\n  - b\ndescription: x\n---\nbody"
    out = sm.set_tags_text(text, ["c"])
    assert sm.parse_meta(out)["tags"] == ["c"]
    assert sm.parse_meta(out)["description"] == "x"   # field after block list survives


def test_set_tags_creates_frontmatter_when_none():
    out = sm.set_tags_text("bare body\n", ["a", "b"])
    assert out.startswith("---\n")
    assert sm.parse_meta(out)["tags"] == ["a", "b"]
    assert "bare body" in out


def test_set_empty_tags_clears():
    out = sm.set_tags_text("---\ntags: a, b\n---\nx", [])
    assert sm.parse_meta(out)["tags"] == []


# ── directory-level helpers ──────────────────────────────────────────────────

def test_read_skill(tmp_path):
    d = _skill(tmp_path, "ideas", "---\ndescription: tend ideas\ntags: alive\n---\nx")
    s = sm.read_skill(d)
    assert s["name"] == "ideas"
    assert s["description"] == "tend ideas"
    assert s["tags"] == ["alive"]
    assert s["symlink"] is False


def test_list_and_group_by_tag(tmp_path):
    _skill(tmp_path, "b_health", "---\ntags: alive\n---\n")
    _skill(tmp_path, "a_daemon", "---\ntags: infra, alive\n---\n")
    _skill(tmp_path, "c_loose", "---\ndescription: no tags\n---\n")
    skills = sm.list_skills(tmp_path)
    assert [s["name"] for s in skills] == ["a_daemon", "b_health", "c_loose"]
    groups = sm.group_by_tag(skills)
    assert list(groups.keys()) == ["alive", "infra", "untagged"]   # untagged last
    assert [s["name"] for s in groups["alive"]] == ["a_daemon", "b_health"]
    assert [s["name"] for s in groups["untagged"]] == ["c_loose"]


def test_set_skill_tags_writes_file(tmp_path):
    _skill(tmp_path, "ideas", "---\ndescription: x\n---\nbody\n")
    sm.set_skill_tags(tmp_path, "ideas", ["alive", "daily"])
    assert sm.read_skill(tmp_path / "ideas")["tags"] == ["alive", "daily"]


def test_set_skill_tags_missing_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        sm.set_skill_tags(tmp_path, "nope", ["a"])


# ── add / remove / clear ops ─────────────────────────────────────────────────

def test_apply_ops_all_plain_replaces():
    assert sm.apply_tag_ops(["old"], ["a", "b"]) == ["a", "b"]


def test_apply_ops_remove_keeps_the_rest():
    assert sm.apply_tag_ops(["alive", "system"], ["-system"]) == ["alive"]


def test_apply_ops_add_without_replacing():
    assert sm.apply_tag_ops(["alive"], ["+system"]) == ["alive", "system"]


def test_apply_ops_add_and_remove_together():
    assert sm.apply_tag_ops(["a", "b"], ["+c", "-a"]) == ["b", "c"]


def test_apply_ops_clear():
    assert sm.apply_tag_ops(["a", "b"], ["--clear"]) == []


def test_apply_ops_remove_absent_is_noop():
    assert sm.apply_tag_ops(["a"], ["-zzz"]) == ["a"]


def test_apply_ops_add_is_idempotent():
    assert sm.apply_tag_ops(["a"], ["+a"]) == ["a"]


def test_apply_ops_replace_dedupes():
    assert sm.apply_tag_ops([], ["a", "a", "b"]) == ["a", "b"]
