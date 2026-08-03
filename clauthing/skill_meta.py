#!/usr/bin/env python3
"""Read and edit the YAML-ish frontmatter of skill SKILL.md files.

A skill is a directory containing SKILL.md, whose leading `---`-delimited block
holds `description:` and (now) `tags:`. Kept stdlib-only — we only need a few
scalar/list fields, not a full YAML parser, and the rest of clauthing already
hand-parses this frontmatter.

`tags` is accepted in any of the forms people actually write:
    tags: alive, health          (inline, comma-separated)
    tags: [alive, health]        (inline list)
    tags:                        (block list)
      - alive
      - health
and always written back as the inline comma form.
"""
import re
from pathlib import Path


def split_frontmatter(text):
    """(frontmatter_lines, body) — the lines inside the leading --- block and
    everything after it. If there's no frontmatter, ([], text)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return [], text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], "\n".join(lines[i + 1:])
    return [], text                       # unterminated → treat as no frontmatter


def _parse_tags(value, block_lines):
    """Tags from an inline `value` (after `tags:`) and any following block
    `- item` lines."""
    tags = []
    v = value.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    if v:
        tags += [t.strip() for t in re.split(r"[,\s]+", v) if t.strip()]
    for bl in block_lines:
        m = re.match(r"\s*-\s+(.*\S)\s*$", bl)
        if m:
            tags.append(m.group(1).strip())
    # dedupe, preserve order
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def parse_meta(text):
    """Frontmatter as a dict; `tags` is always a list (possibly empty)."""
    fm, _body = split_frontmatter(text)
    meta = {"tags": []}
    i = 0
    while i < len(fm):
        line = fm[i]
        m = re.match(r"([A-Za-z0-9_-]+):(.*)$", line)
        if not m:
            i += 1
            continue
        key, val = m.group(1).strip(), m.group(2)
        if key == "tags":
            # gather following indented block-list lines, if any
            block = []
            j = i + 1
            while j < len(fm) and re.match(r"\s*-\s+", fm[j]):
                block.append(fm[j])
                j += 1
            meta["tags"] = _parse_tags(val, block)
            i = j
        else:
            meta[key] = val.strip()
            i += 1
    return meta


def set_tags_text(text, tags):
    """Return `text` with its frontmatter `tags:` set to `tags` (inline form),
    creating the frontmatter block or the tags line if absent. Order and other
    fields are preserved."""
    line = "tags: " + ", ".join(tags) if tags else "tags:"
    fm, body = split_frontmatter(text)
    if not fm and (not text.splitlines() or text.splitlines()[0].strip() != "---"):
        # no frontmatter at all — prepend one
        return f"---\n{line}\n---\n{text}" if text else f"---\n{line}\n---\n"

    out, i, replaced = [], 0, False
    while i < len(fm):
        if re.match(r"tags:", fm[i].strip()):
            out.append(line)
            i += 1
            while i < len(fm) and re.match(r"\s*-\s+", fm[i]):   # drop old block list
                i += 1
            replaced = True
        else:
            out.append(fm[i])
            i += 1
    if not replaced:
        out.append(line)
    return "---\n" + "\n".join(out) + "\n---\n" + body


# ── skill-directory helpers ──────────────────────────────────────────────────

def skill_md(skill_dir):
    return Path(skill_dir) / "SKILL.md"


def read_skill(skill_dir):
    """{'name', 'description', 'tags', 'symlink'} for one skill dir."""
    d = Path(skill_dir)
    meta = {}
    md = skill_md(d)
    if md.exists():
        try:
            meta = parse_meta(md.read_text())
        except Exception:
            meta = {"tags": []}
    return {
        "name": d.name,
        "description": meta.get("description", ""),
        "tags": meta.get("tags", []),
        "symlink": d.is_symlink(),
    }


def list_skills(skills_dir):
    """All skills under `skills_dir`, sorted by name."""
    base = Path(skills_dir)
    if not base.exists():
        return []
    return [read_skill(d) for d in sorted(base.iterdir()) if d.is_dir()]


def group_by_tag(skills, untagged="untagged"):
    """{tag: [skill, ...]} ordered by tag; skills with no tag go under
    `untagged`. A skill with several tags appears under each."""
    groups = {}
    for s in skills:
        for tag in (s["tags"] or [untagged]):
            groups.setdefault(tag, []).append(s)
    # untagged last, others alphabetical
    ordered = {}
    for tag in sorted(k for k in groups if k != untagged):
        ordered[tag] = groups[tag]
    if untagged in groups:
        ordered[untagged] = groups[untagged]
    return ordered


def apply_tag_ops(current, ops):
    """New tag list from `current` and a list of `ops`:

      --clear                 → []
      all plain (no +/-)      → replace: the ops become the tag set
      any +/- present         → edit `current`: `+x`/`x` add, `-x` remove

    Mixing is by design: `:skilltag s +a -b` adds a and removes b from what's
    there, while `:skilltag s a b` sets exactly {a, b}. Order preserved, deduped.
    """
    if ops == ["--clear"]:
        return []
    if any(o[:1] in "+-" for o in ops):
        result = list(current)
        for o in ops:
            if o.startswith("-"):
                result = [x for x in result if x != o[1:]]
            else:
                t = o[1:] if o.startswith("+") else o
                if t and t not in result:
                    result.append(t)
        return result
    seen, out = set(), []                 # all plain → replace
    for o in ops:
        if o and o not in seen:
            seen.add(o)
            out.append(o)
    return out


def set_skill_tags(skills_dir, name, tags):
    """Write `tags` onto skill `name`'s SKILL.md. Returns the new tag list.
    Raises FileNotFoundError if the skill/SKILL.md doesn't exist."""
    md = skill_md(Path(skills_dir) / name)
    if not md.exists():
        raise FileNotFoundError(md)
    md.write_text(set_tags_text(md.read_text(), tags))
    return tags
