"""The transcript index: hash chain, incremental update, extends relation.

The whole design rests on three properties, so they're pinned here:
  1. the canonical form ignores per-copy metadata (else clones look unrelated),
  2. the chain is incremental (extending never re-reads the prefix),
  3. B extends A iff A's head hash appears in B's chain.
"""
import json

import pytest

from clauthing import sessions_db as sdb


@pytest.fixture
def conn(tmp_path):
    return sdb.connect(tmp_path / "t.db")


def _turn(role, text, ts="2026-07-01T00:00:00Z", **extra):
    e = {"type": role, "timestamp": ts, "message": {"role": role, "content": text}}
    e.update(extra)
    return e


def _write(path, turns):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(t) for t in turns) + "\n")
    return path


# ── canonicalisation: per-copy metadata must not affect the hash ─────────────

def test_canonical_ignores_session_scoped_metadata():
    """Two entries differing ONLY in per-copy metadata are the same turn.

    Note this is defensive, not load-bearing today: a real `:cd` copies the
    transcript verbatim, so these fields already match between copies (verified
    on real data — raw-line hashing groups conversations identically). It
    matters only if an id is ever rewritten in place.
    """
    a = _turn("user", "hi", sessionId="aaa", cwd="/one", uuid="u1", parentUuid=None)
    b = _turn("user", "hi", sessionId="bbb", cwd="/two", uuid="u2", version="9.9")
    assert sdb.canonical_message(a) == sdb.canonical_message(b)


def test_canonical_distinguishes_real_differences():
    assert sdb.canonical_message(_turn("user", "hi")) != \
           sdb.canonical_message(_turn("user", "bye"))
    assert sdb.canonical_message(_turn("user", "hi")) != \
           sdb.canonical_message(_turn("assistant", "hi"))
    assert sdb.canonical_message(_turn("user", "hi", ts="A")) != \
           sdb.canonical_message(_turn("user", "hi", ts="B"))


def test_non_turns_are_skipped():
    assert sdb.canonical_message({"type": "summary"}) is None
    assert sdb.canonical_message({"type": "system"}) is None
    assert sdb.canonical_message("nonsense") is None


def test_structured_content_is_canonicalised():
    e = {"type": "assistant", "timestamp": "t", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "a"},
                     {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}}]}}
    c = sdb.canonical_message(e)
    assert b"Bash" in c and b"a" in c


# ── the chain ────────────────────────────────────────────────────────────────

def test_chain_is_order_dependent():
    a, b = b"one", b"two"
    assert sdb.chain(sdb.chain("", a), b) != sdb.chain(sdb.chain("", b), a)


def test_chain_is_incremental():
    """Extending must only need the previous head, never the whole prefix —
    this is what makes indexing cheap."""
    msgs = [b"m1", b"m2", b"m3"]
    h = ""
    for m in msgs:
        h = sdb.chain(h, m)
    resumed = sdb.chain(sdb.chain(sdb.chain("", msgs[0]), msgs[1]), msgs[2])
    assert h == resumed


# ── extends ──────────────────────────────────────────────────────────────────

def test_extends_is_detected(conn, tmp_path):
    base = [_turn("user", "one"), _turn("assistant", "two")]
    parent = _write(tmp_path / "p" / "projects" / "x" / "parent.jsonl", base)
    child = _write(tmp_path / "c" / "projects" / "x" / "child.jsonl",
                   base + [_turn("user", "three")])
    sdb.index_file(conn, parent, "p", "x")
    sdb.index_file(conn, child, "c", "x")
    conn.commit()
    pairs = sdb.extends_pairs(conn)
    assert [(2, 3)] == [(plen, clen) for _c, _p, plen, clen, _n in pairs]
    # and the parent head really is the one identifying the parent file
    parent_head = conn.execute(
        "SELECT head_hash FROM file WHERE path=?", (str(parent),)).fetchone()[0]
    assert pairs[0][1] == parent_head


def test_many_copies_yield_one_relationship_not_a_cross_product(conn, tmp_path):
    """Real data has 156 copies of one conversation; a file-level join emitted
    parent_copies x child_copies rows (1341) for 3 real relationships."""
    base = [_turn("user", "one")]
    for i in range(4):                                    # 4 copies of parent
        _write(tmp_path / f"p{i}" / "projects" / "x" / f"p{i}.jsonl", base)
    for i in range(3):                                    # 3 copies of child
        _write(tmp_path / f"c{i}" / "projects" / "x" / f"c{i}.jsonl",
               base + [_turn("user", "two")])
    sdb.update_index(conn, base=tmp_path)
    assert len(sdb.extends_pairs(conn)) == 1
    assert sdb.extends_pairs(conn)[0][4] == 3             # 3 child files


def test_identical_copies_are_dups_not_extends(conn, tmp_path):
    turns = [_turn("user", "same")]
    a = _write(tmp_path / "a" / "projects" / "x" / "a.jsonl", turns)
    b = _write(tmp_path / "b" / "projects" / "x" / "b.jsonl", turns)
    sdb.index_file(conn, a, "a", "x")
    sdb.index_file(conn, b, "b", "x")
    conn.commit()
    assert sdb.extends_pairs(conn) == []
    assert len(sdb.duplicates(conn)) == 1


def test_divergent_conversations_do_not_extend(conn, tmp_path):
    a = _write(tmp_path / "a" / "projects" / "x" / "a.jsonl",
               [_turn("user", "one"), _turn("user", "LEFT")])
    b = _write(tmp_path / "b" / "projects" / "x" / "b.jsonl",
               [_turn("user", "one"), _turn("user", "RIGHT"), _turn("user", "more")])
    sdb.index_file(conn, a, "a", "x")
    sdb.index_file(conn, b, "b", "x")
    conn.commit()
    assert sdb.extends_pairs(conn) == []


def test_latest_for_points_at_the_longest_continuation(conn, tmp_path):
    base = [_turn("user", "one")]
    p = _write(tmp_path / "p" / "projects" / "x" / "p.jsonl", base)
    _write(tmp_path / "c1" / "projects" / "x" / "c1.jsonl", base + [_turn("user", "2")])
    c2 = _write(tmp_path / "c2" / "projects" / "x" / "c2.jsonl",
                base + [_turn("user", "2"), _turn("user", "3")])
    for f, s in [(p, "p"), (tmp_path / "c1" / "projects" / "x" / "c1.jsonl", "c1"), (c2, "c2")]:
        sdb.index_file(conn, f, s, "x")
    conn.commit()
    assert sdb.latest_for(conn, p)["path"] == str(c2)


def test_tips_excludes_extended_files(conn, tmp_path):
    base = [_turn("user", "one")]
    p = _write(tmp_path / "p" / "projects" / "x" / "p.jsonl", base)
    c = _write(tmp_path / "c" / "projects" / "x" / "c.jsonl", base + [_turn("user", "2")])
    sdb.index_file(conn, p, "p", "x")
    sdb.index_file(conn, c, "c", "x")
    conn.commit()
    assert [r["path"] for r in sdb.tips(conn)] == [str(c)]


# ── incremental indexing ─────────────────────────────────────────────────────

def test_reindex_is_a_noop_when_unchanged(conn, tmp_path):
    f = _write(tmp_path / "a" / "projects" / "x" / "a.jsonl", [_turn("user", "one")])
    assert sdb.index_file(conn, f, "a", "x") == (1, "rebuilt")
    assert sdb.index_file(conn, f, "a", "x") == (0, "unchanged")


def test_append_only_hashes_just_the_new_messages(conn, tmp_path):
    """The point of the chain: appending 1 message costs 1 hash, not N."""
    p = tmp_path / "a" / "projects" / "x" / "a.jsonl"
    _write(p, [_turn("user", "one"), _turn("user", "two")])
    sdb.index_file(conn, p, "a", "x")
    with open(p, "a") as fh:
        fh.write(json.dumps(_turn("user", "three")) + "\n")
    added, status = sdb.index_file(conn, p, "a", "x")
    assert (added, status) == (1, "appended")
    assert conn.execute("SELECT n_messages FROM file").fetchone()[0] == 3


def test_appending_matches_a_full_rebuild(conn, tmp_path):
    """Incremental and from-scratch must agree, or the index silently rots."""
    p = tmp_path / "a" / "projects" / "x" / "a.jsonl"
    _write(p, [_turn("user", "one")])
    sdb.index_file(conn, p, "a", "x")
    with open(p, "a") as fh:
        fh.write(json.dumps(_turn("user", "two")) + "\n")
    sdb.index_file(conn, p, "a", "x")
    incremental = conn.execute("SELECT head_hash FROM file").fetchone()[0]
    sdb.index_file(conn, p, "a", "x", force=True)
    assert conn.execute("SELECT head_hash FROM file").fetchone()[0] == incremental


def test_a_shrunk_file_is_rebuilt_not_chained(conn, tmp_path):
    """Append-only is an assumption. If it's violated we must not chain onto a
    stale head and record a hash for a conversation that never existed."""
    p = tmp_path / "a" / "projects" / "x" / "a.jsonl"
    _write(p, [_turn("user", "one"), _turn("user", "two"), _turn("user", "three")])
    sdb.index_file(conn, p, "a", "x")
    _write(p, [_turn("user", "one")])                     # truncated
    added, status = sdb.index_file(conn, p, "a", "x")
    assert status == "rebuilt"
    assert conn.execute("SELECT n_messages FROM file").fetchone()[0] == 1
    sdb.index_file(conn, p, "a", "x", force=True)
    assert conn.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 1


def test_update_index_walks_the_tree(conn, tmp_path):
    _write(tmp_path / "s1" / "projects" / "-proj" / "s1.jsonl", [_turn("user", "a")])
    _write(tmp_path / "s2" / "projects" / "-proj" / "s2.jsonl", [_turn("user", "b")])
    stats = sdb.update_index(conn, base=tmp_path)
    assert stats["files"] == 2 and stats["messages"] == 2


def test_malformed_lines_are_skipped(conn, tmp_path):
    p = tmp_path / "a" / "projects" / "x" / "a.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text('{"broken\n' + json.dumps(_turn("user", "ok")) + "\n\n")
    added, _ = sdb.index_file(conn, p, "a", "x")
    assert added == 1


# ── shared ancestors / forks ─────────────────────────────────────────────────

def test_fork_reports_the_divergence_point(conn, tmp_path):
    shared = [_turn("user", "one"), _turn("assistant", "two")]
    a = _write(tmp_path / "a" / "projects" / "x" / "a.jsonl",
               shared + [_turn("user", "LEFT")])
    b = _write(tmp_path / "b" / "projects" / "x" / "b.jsonl",
               shared + [_turn("user", "RIGHT"), _turn("user", "more")])
    sdb.index_file(conn, a, "a", "x")
    sdb.index_file(conn, b, "b", "x")
    conn.commit()
    got = sdb.forks(conn)
    assert len(got) == 1
    _a, _b, depth, na, nb = got[0]
    assert depth == 2                      # they share exactly the 2 leading msgs
    assert sorted((na, nb)) == [3, 4]


def test_a_pure_extension_is_not_a_fork(conn, tmp_path):
    """One conversation simply continuing another shares an ancestor, but
    neither side diverged — that's an extends, not a fork."""
    base = [_turn("user", "one")]
    a = _write(tmp_path / "a" / "projects" / "x" / "a.jsonl", base)
    b = _write(tmp_path / "b" / "projects" / "x" / "b.jsonl", base + [_turn("user", "2")])
    sdb.index_file(conn, a, "a", "x")
    sdb.index_file(conn, b, "b", "x")
    conn.commit()
    assert sdb.forks(conn) == []
    assert len(sdb.shared_ancestors(conn)) == 1        # still a shared ancestor


def test_unrelated_conversations_share_nothing(conn, tmp_path):
    a = _write(tmp_path / "a" / "projects" / "x" / "a.jsonl", [_turn("user", "alpha")])
    b = _write(tmp_path / "b" / "projects" / "x" / "b.jsonl", [_turn("user", "beta")])
    sdb.index_file(conn, a, "a", "x")
    sdb.index_file(conn, b, "b", "x")
    conn.commit()
    assert sdb.shared_ancestors(conn) == []


def test_copies_do_not_inflate_fork_results(conn, tmp_path):
    """156 copies per side must still yield ONE fork, not 156*156."""
    shared = [_turn("user", "one")]
    for i in range(3):
        _write(tmp_path / f"a{i}" / "projects" / "x" / f"a{i}.jsonl",
               shared + [_turn("user", "LEFT")])
        _write(tmp_path / f"b{i}" / "projects" / "x" / f"b{i}.jsonl",
               shared + [_turn("user", "RIGHT")])
    sdb.update_index(conn, base=tmp_path)
    assert len(sdb.forks(conn)) == 1


def test_min_depth_filters_shallow_pairs(conn, tmp_path):
    shared = [_turn("user", "one")]
    _write(tmp_path / "a" / "projects" / "x" / "a.jsonl", shared + [_turn("user", "L")])
    _write(tmp_path / "b" / "projects" / "x" / "b.jsonl", shared + [_turn("user", "R")])
    sdb.update_index(conn, base=tmp_path)
    assert len(sdb.forks(conn, min_depth=1)) == 1
    assert sdb.forks(conn, min_depth=5) == []


def test_tips_are_conversation_level_not_per_copy(conn, tmp_path):
    turns = [_turn("user", "one")]
    for i in range(4):
        _write(tmp_path / f"c{i}" / "projects" / "x" / f"c{i}.jsonl", turns)
    sdb.update_index(conn, base=tmp_path)
    rows = sdb.tips(conn)
    assert len(rows) == 1 and rows[0]["n_files"] == 4


def test_conversation_name_falls_back_to_project(conn, tmp_path):
    """Never show a bare uuid: an unnamed session borrows the project dir."""
    f = _write(tmp_path / "s" / "projects" / "-home-bruger-mine-clauthing" / "s.jsonl",
               [_turn("user", "one")])
    sdb.index_file(conn, f, "s", "-home-bruger-mine-clauthing")
    conn.commit()
    row = sdb.tips(conn)[0]
    assert sdb.conversation_name(row) == "clauthing"


# ── "is this window on a stale version?" ─────────────────────────────────────

def test_newer_than_finds_a_continuation(conn, tmp_path):
    base = [_turn("user", "one")]
    p = _write(tmp_path / "p" / "projects" / "x" / "p.jsonl", base)
    _write(tmp_path / "c" / "projects" / "x" / "c.jsonl", base + [_turn("user", "2")])
    sdb.update_index(conn, base=tmp_path)
    head = conn.execute("SELECT head_hash,n_messages FROM file WHERE path=?",
                        (str(p),)).fetchone()
    newer = sdb.newer_than(conn, head["head_hash"], head["n_messages"])
    assert newer["n_messages"] == 2


def test_newer_than_is_none_at_the_tip(conn, tmp_path):
    c = _write(tmp_path / "c" / "projects" / "x" / "c.jsonl", [_turn("user", "one")])
    sdb.index_file(conn, c, "c", "x")
    conn.commit()
    row = conn.execute("SELECT head_hash,n_messages FROM file").fetchone()
    assert sdb.newer_than(conn, row["head_hash"], row["n_messages"]) is None


def test_tip_named_recovers_an_orphaned_conversation(conn, tmp_path):
    """A restore that opens a fresh empty session orphans the real
    conversation; the window name is the only link back to it."""
    _write(tmp_path / "s" / "projects" / "-home-bruger-mine-state" / "s.jsonl",
           [_turn("user", "a"), _turn("user", "b")])
    sdb.update_index(conn, base=tmp_path)
    found = sdb.tip_named(conn, "state")
    assert found is not None and found["n_messages"] == 2
    assert sdb.tip_named(conn, "nonexistent") is None


def test_tip_named_prefers_the_richest_conversation(conn, tmp_path):
    _write(tmp_path / "a" / "projects" / "-x-pain" / "a.jsonl", [_turn("user", "1")])
    _write(tmp_path / "b" / "projects" / "-x-pain" / "b.jsonl",
           [_turn("user", "1"), _turn("user", "2"), _turn("user", "3")])
    sdb.update_index(conn, base=tmp_path)
    assert sdb.tip_named(conn, "pain")["n_messages"] == 3


def test_index_session_from_disk_picks_up_an_unindexed_session(conn, tmp_path):
    """A live window's transcript may be newer than the index — checking it
    must not report a false 'nothing newer'."""
    _write(tmp_path / "s" / "projects" / "-x" / "sess-1.jsonl", [_turn("user", "hi")])
    assert sdb.files_for_session(conn, "sess-1") == []
    assert sdb.index_session_from_disk(conn, "sess-1", base=tmp_path) == 1
    assert len(sdb.files_for_session(conn, "sess-1")) == 1


# ── symlinked projects trees must not inflate the index ──────────────────────

def test_symlinked_projects_are_deduped_by_inode(conn, tmp_path):
    """Every session-config dir's projects/ is a SYMLINK to one shared tree, so
    a naive walk sees each real transcript ~156 times. Dedup by inode: one real
    file, however many symlinks point at it, is indexed once."""
    # the real shared tree
    shared = tmp_path / "s-real" / "projects" / "-proj"
    shared.mkdir(parents=True)
    (shared / "sess.jsonl").write_text(json.dumps(_turn("user", "hi")) + "\n")
    # three other config dirs whose projects/ symlink at the shared tree
    for i in range(3):
        d = tmp_path / f"s-link{i}"
        d.mkdir()
        (d / "projects").symlink_to(tmp_path / "s-real" / "projects")

    files = sdb.transcript_files(base=tmp_path)
    assert len(files) == 1                      # not 4

    stats = sdb.update_index(conn, base=tmp_path)
    assert stats["files"] == 1
    assert conn.execute("SELECT COUNT(*) FROM file").fetchone()[0] == 1


def test_distinct_real_copies_are_kept(conn, tmp_path):
    """A real `:cd` copy is a DIFFERENT file (new session id) and must survive
    dedup — only symlink aliases of the same inode collapse."""
    base = [_turn("user", "one")]
    _write(tmp_path / "orig" / "projects" / "-p" / "a.jsonl", base)
    _write(tmp_path / "cd" / "projects" / "-p" / "b.jsonl", base + [_turn("user", "2")])
    stats = sdb.update_index(conn, base=tmp_path)
    assert stats["files"] == 2                  # both real files kept
    assert len(sdb.extends_pairs(conn)) == 1    # and the branch is detected


def test_update_prunes_rows_for_vanished_paths(conn, tmp_path):
    f = _write(tmp_path / "a" / "projects" / "-p" / "a.jsonl", [_turn("user", "one")])
    sdb.update_index(conn, base=tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM file").fetchone()[0] == 1
    f.unlink()
    stats = sdb.update_index(conn, base=tmp_path)
    assert stats["pruned"] == 1
    assert conn.execute("SELECT COUNT(*) FROM file").fetchone()[0] == 0
