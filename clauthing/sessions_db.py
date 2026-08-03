#!/usr/bin/env python3
"""An index of clauthing transcripts, and the "extends" relation between them.

WHY
    A `:cd` copies a transcript into a new session id, so the same conversation
    exists under many ids and one conversation is often a strict continuation of
    another. To ask "which session should I actually be using?" you need to know
    which transcripts extend which.

HOW
    Transcripts are append-only, so we keep a HASH CHAIN per file:

        h[0] = sha256(canonical(msg[0]))
        h[n] = sha256(h[n-1] + canonical(msg[n]))

    h[n] therefore identifies "the conversation so far" at message n. Two
    consequences we rely on:

      * extending is incremental — to index new messages you resume from the
        stored head hash and fold in only what was appended, never re-reading
        the prefix.
      * B extends A  <=>  A's head hash appears somewhere in B's chain. That is
        a single indexed lookup, not an O(n*m) comparison of transcripts.

WHAT WE HASH
    An explicit ALLOWLIST (see `canonical_message`) — role, timestamp, content
    — not the raw JSON line.

    Be clear about why, because the obvious reason is WRONG: a `:cd` does NOT
    rewrite per-entry metadata. It copies the transcript verbatim and only the
    file's path changes, so sessionId / cwd / uuid are byte-identical between
    copies. Measured on a 400-file sample, hashing raw lines groups
    conversations exactly as the allowlist does (11 conversations either way).

    So the allowlist buys nothing TODAY. It is kept as insurance: it makes the
    hash mean "this conversation" rather than "these file bytes", so the index
    still works if a future flow rewrites ids in place (a resume renumbering
    sessionId, a format/version bump) — which raw hashing would report as a
    brand-new unrelated conversation.

    The cost of that insurance: two conversations with identical messages but
    different cwd hash the same and are reported as one. If you ever need to
    tell those apart, add cwd to the hashed payload.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

SCHEMA_VERSION = 1

# A "name" that is really a session id means the window was never named.
_UUIDISH = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{4,}$", re.I)

# Fields that identify a conversation turn. Everything else in a transcript
# entry is per-copy bookkeeping and must NOT be hashed.
_CONTENT_ALLOWLIST = ("text", "thinking", "name", "input", "content")


def _default_base():
    return Path.home() / ".config" / "clauthing" / "session-configs"


def db_path():
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    d = base / "clauthing"
    d.mkdir(parents=True, exist_ok=True)
    return d / "sessions.db"


# ── canonicalisation ─────────────────────────────────────────────────────────

def _canonical_block(block):
    """One content block reduced to its meaningful parts."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return json.dumps(block, sort_keys=True, default=str)
    out = {"type": block.get("type")}
    for k in _CONTENT_ALLOWLIST:
        if k in block:
            v = block[k]
            out[k] = (_canonical_content(v) if k == "content"
                      else v if isinstance(v, str) else
                      json.dumps(v, sort_keys=True, default=str))
    return json.dumps(out, sort_keys=True, default=str)


def _canonical_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\x1e".join(_canonical_block(b) for b in content)
    return json.dumps(content, sort_keys=True, default=str)


def canonical_message(entry):
    """The bytes that identify a turn, or None if the entry isn't a turn.

    Excludes sessionId / cwd / uuid / parentUuid / version. NB these are in
    fact identical across `:cd` copies (a clone is a verbatim file copy), so
    excluding them changes nothing measurable today — see the module docstring.
    It is defensive, so the hash tracks the conversation rather than the file
    bytes if any id is ever rewritten in place.
    """
    if not isinstance(entry, dict):
        return None
    kind = entry.get("type")
    if kind not in ("user", "assistant"):
        return None
    msg = entry.get("message") or {}
    role = msg.get("role") or kind
    payload = {
        "role": role,
        "ts": entry.get("timestamp") or "",
        "content": _canonical_content(msg.get("content")),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()


def chain(prev_hash, canonical):
    """Fold one message into the chain. `prev_hash` is '' for the first."""
    h = hashlib.sha256()
    h.update(prev_hash.encode())
    h.update(b"\x00")
    h.update(canonical)
    return h.hexdigest()


def message_hash(canonical):
    """Hash of a single message, independent of position."""
    return hashlib.sha256(canonical).hexdigest()


# ── storage ──────────────────────────────────────────────────────────────────

def connect(path=None):
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS file (
            path       TEXT PRIMARY KEY,
            session    TEXT,
            project    TEXT,
            size       INTEGER,
            mtime      REAL,
            n_messages INTEGER,
            head_hash  TEXT,
            indexed_at REAL
        );
        CREATE TABLE IF NOT EXISTS message (
            path       TEXT NOT NULL,
            pos        INTEGER NOT NULL,
            chain_hash TEXT NOT NULL,
            msg_hash   TEXT NOT NULL,
            role       TEXT,
            ts         TEXT,
            preview    TEXT,
            PRIMARY KEY (path, pos)
        );
        CREATE INDEX IF NOT EXISTS idx_msg_chain ON message(chain_hash);
        CREATE INDEX IF NOT EXISTS idx_msg_hash  ON message(msg_hash);
        CREATE INDEX IF NOT EXISTS idx_file_head ON file(head_hash);
    """)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    return conn


def transcript_roots(base=None):
    """Every tree that holds transcripts, as (path, layout).

    There are TWO layouts and missing the second one hides whole windows:
      'per-session'  <base>/<session-config-id>/projects/<cwd>/<sid>.jsonl
      'shared'       <config>/claude-data/projects/<cwd>/<sid>.jsonl
    The shared claude-data tree is where a window that never got its own
    session-config dir writes — the `god` transcripts live only there.
    """
    if base is not None:
        b = Path(base)
        roots = [(b, "per-session")]
        shared = b / "claude-data" / "projects"
        if shared.is_dir():
            roots.append((shared, "shared"))
        return roots
    cfg = Path.home() / ".config" / "clauthing"
    roots = [(cfg / "session-configs", "per-session")]
    for extra in (cfg / "claude-data" / "projects",):
        if extra.is_dir():
            roots.append((extra, "shared"))
    return roots


def _walk_transcripts(base):
    """Yield (path, session, project) candidates across every layout, BEFORE
    symlink de-duplication."""
    for root, layout in transcript_roots(base):
        if not root.exists():
            continue
        if layout == "shared":
            # <root>/<encoded-cwd>/<session>.jsonl — no per-session dir, so the
            # transcript's own id stands in as the session.
            for enc in root.iterdir():
                if enc.is_dir():
                    for f in enc.glob("*.jsonl"):
                        yield f, f.stem, enc.name
            continue
        for sdir in root.iterdir():
            proj = sdir / "projects"
            if not proj.is_dir():
                continue
            for enc in proj.iterdir():
                if enc.is_dir():
                    for f in enc.glob("*.jsonl"):
                        yield f, sdir.name, enc.name


def transcript_files(base=None):
    """(real_path, session, project) for every DISTINCT transcript.

    Every session-config dir's projects/ is a SYMLINK to one shared
    claude-data/projects tree, so a naive walk sees each real transcript ~156
    times (once per config dir) and reported 123x phantom duplication. We dedupe
    by real inode and key each file under its resolved real path, so a
    transcript is indexed exactly once no matter how many symlinks point at it.
    """
    out, seen = [], set()
    for f, session, project in _walk_transcripts(base):
        try:
            st = f.stat()
            key = (st.st_dev, st.st_ino)
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        real = f.resolve()
        # Use the real file's own location for identity, so repeated runs and
        # different symlink views all agree on one canonical path.
        out.append((real, real.stem, real.parent.name))
    return out


def _iter_canonical(path, skip=0):
    """Yield (canonical_bytes, role, ts, preview) for turns after `skip` turns."""
    n = 0
    with open(path, "r", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            canon = canonical_message(entry)
            if canon is None:
                continue
            n += 1
            if n <= skip:
                continue
            msg = entry.get("message") or {}
            yield (canon, msg.get("role") or entry.get("type"),
                   entry.get("timestamp") or "",
                   _canonical_content(msg.get("content"))[:200])


def index_file(conn, path, session, project, force=False):
    """Index one transcript, resuming from what's already stored.

    Returns (added, status) where status is 'unchanged' | 'appended' | 'rebuilt'.
    Append-only is an assumption, not a guarantee: if a file SHRANK or its
    prefix no longer matches, we rebuild it rather than silently chain onto a
    stale hash.
    """
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return 0, "missing"

    row = conn.execute("SELECT * FROM file WHERE path=?", (str(path),)).fetchone()
    if row and not force and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
        return 0, "unchanged"

    skip, prev_hash, status = 0, "", "rebuilt"
    if row and not force and st.st_size >= (row["size"] or 0):
        # Grew (or unchanged size with a new mtime) -> trust append-only and
        # resume from the stored head.
        skip, prev_hash, status = row["n_messages"], row["head_hash"] or "", "appended"
    if status == "rebuilt":
        conn.execute("DELETE FROM message WHERE path=?", (str(path),))

    pos = skip
    added = 0
    for canon, role, ts, preview in _iter_canonical(path, skip=skip):
        prev_hash = chain(prev_hash, canon)
        conn.execute(
            "INSERT OR REPLACE INTO message VALUES (?,?,?,?,?,?,?)",
            (str(path), pos, prev_hash, message_hash(canon), role, ts, preview))
        pos += 1
        added += 1

    conn.execute(
        "INSERT OR REPLACE INTO file VALUES (?,?,?,?,?,?,?,?)",
        (str(path), session, project, st.st_size, st.st_mtime, pos,
         prev_hash, time.time()))
    return added, ("unchanged" if added == 0 and status == "appended" else status)


def update_index(conn, base=None, force=False, progress=None):
    """Bring the index up to date. Returns a summary dict."""
    stats = {"files": 0, "messages": 0, "unchanged": 0, "appended": 0,
             "rebuilt": 0, "pruned": 0}
    files = transcript_files(base)
    current = {str(p) for p, _s, _pr in files}
    for i, (path, session, project) in enumerate(files):
        added, status = index_file(conn, path, session, project, force=force)
        stats["files"] += 1
        stats["messages"] += added
        if status in stats:
            stats[status] += 1
        if progress and i % 200 == 0:
            progress(i, len(files))
        if i % 500 == 0:
            conn.commit()

    # Drop rows for paths that are no longer canonical transcripts — this is
    # what clears the phantom symlink duplicates left by an older index.
    stale = [r["path"] for r in conn.execute("SELECT path FROM file").fetchall()
             if r["path"] not in current]
    for p in stale:
        conn.execute("DELETE FROM message WHERE path=?", (p,))
        conn.execute("DELETE FROM file WHERE path=?", (p,))
    stats["pruned"] = len(stale)
    conn.commit()
    return stats


# ── queries ──────────────────────────────────────────────────────────────────

def extends_pairs(conn):
    """Which CONVERSATIONS continue which, as rows of
    (child_head, parent_head, parent_len, child_len, n_child_files).

    Deliberately keyed on head hash rather than file path: one conversation
    routinely exists as many identical files (every `:cd` copies it), and a
    file-level join would emit parent_copies x child_copies rows for what is
    really a single relationship.

    A conversation's head hash identifies it in full, so a child containing
    that hash mid-chain has the parent as a strict prefix. Equal-length matches
    are identical copies, not extensions, so they're excluded.
    """
    rows = conn.execute("""
        SELECT cf.head_hash AS child_head,
               f.head_hash  AS parent_head,
               MIN(f.n_messages)  AS parent_len,
               MIN(cf.n_messages) AS child_len,
               COUNT(DISTINCT cf.path) AS n_child_files
        FROM file f
        JOIN message m ON m.chain_hash = f.head_hash
        JOIN file cf   ON cf.path = m.path
        WHERE f.n_messages > 0
          AND cf.n_messages > f.n_messages
          AND cf.head_hash != f.head_hash
        GROUP BY f.head_hash, cf.head_hash
        ORDER BY child_len - parent_len DESC
    """).fetchall()
    return [(r["child_head"], r["parent_head"], r["parent_len"],
             r["child_len"], r["n_child_files"]) for r in rows]


def conversation_name(row):
    """Human name for a conversation — the window name it ran under.

    A transcript's session id is its filename stem; the enclosing
    session-configs dir is a second candidate (a `:cd` copy keeps the original
    stem but lands under a new dir). Falls back to the project directory's last
    path segment, so a conversation is never shown as a bare uuid.
    """
    from clauthing.session import get_session_name
    for sid in (Path(row["path"]).stem, row["session"]):
        if not sid:
            continue
        try:
            name = get_session_name(sid)
        except Exception:
            name = None
        if name and not _UUIDISH.match(name) and name != sid:
            return name
    project = (row["project"] or "").rstrip("-")
    return project.rsplit("-", 1)[-1] or "?"


def describe(conn, head_hash):
    """A readable identity for a conversation: (session, project, n_files)."""
    r = conn.execute("""
        SELECT session, project, COUNT(*) n FROM file
        WHERE head_hash = ? GROUP BY head_hash
        ORDER BY MAX(mtime) DESC LIMIT 1
    """, (head_hash,)).fetchone()
    if not r:
        return ("?", "?", 0)
    return (r["session"], r["project"], r["n"])


def duplicates(conn):
    """Groups of files whose conversations are byte-identical (same head)."""
    rows = conn.execute("""
        SELECT head_hash, COUNT(*) n, GROUP_CONCAT(path, char(10)) paths
        FROM file WHERE n_messages > 0
        GROUP BY head_hash HAVING n > 1 ORDER BY n DESC
    """).fetchall()
    return [(r["head_hash"], r["n"], r["paths"].split("\n")) for r in rows]


def conversations(conn):
    """One row per distinct conversation (not per file): the content identity.

    A conversation routinely exists as many identical files, so anything that
    compares conversations must collapse those first or the work explodes
    combinatorially.
    """
    return conn.execute("""
        SELECT head_hash, MIN(path) AS path, MAX(n_messages) AS n_messages,
               COUNT(*) AS n_files, MAX(mtime) AS mtime,
               MIN(session) AS session, MIN(project) AS project
        FROM file WHERE n_messages > 0
        GROUP BY head_hash ORDER BY n_messages DESC
    """).fetchall()


def shared_ancestors(conn, min_depth=1):
    """Pairs of conversations that share a prefix then diverge (forks).

    Returns [(head_a, head_b, shared_depth, len_a, len_b)] where shared_depth is
    how many leading messages they have in common — the fork point. A chain hash
    at position p encodes the entire prefix up to p, so the deepest position
    where both conversations carry the same chain hash IS their divergence
    point; no message-by-message comparison is needed.

    Restricted to one representative file per conversation: without that, N
    copies of one side times M of the other produce N*M identical rows.
    """
    rows = conn.execute("""
        WITH rep AS (
            SELECT head_hash, MIN(path) AS path, MAX(n_messages) AS n
            FROM file WHERE n_messages > 0 GROUP BY head_hash
        )
        SELECT ra.head_hash AS a, rb.head_hash AS b,
               MAX(ma.pos) + 1 AS shared, ra.n AS na, rb.n AS nb
        FROM rep ra
        JOIN message ma ON ma.path = ra.path
        JOIN message mb ON mb.chain_hash = ma.chain_hash
        JOIN rep rb ON rb.path = mb.path AND rb.head_hash > ra.head_hash
        GROUP BY ra.head_hash, rb.head_hash
        HAVING shared >= ?
        ORDER BY shared DESC
    """, (min_depth,)).fetchall()
    return [(r["a"], r["b"], r["shared"], r["na"], r["nb"]) for r in rows]


def forks(conn, min_depth=1):
    """Shared-ancestor pairs where NEITHER side is just a prefix of the other —
    i.e. both actually diverged, rather than one simply continuing the other."""
    return [(a, b, d, na, nb) for a, b, d, na, nb in shared_ancestors(conn, min_depth)
            if d < na and d < nb]


def tips(conn):
    """Files that nothing else extends — the newest version of each lineage."""
    return conn.execute("""
        SELECT f.head_hash, MIN(f.path) AS path, MAX(f.n_messages) AS n_messages,
               COUNT(*) AS n_files, MAX(f.mtime) AS mtime,
               MIN(f.session) AS session, MIN(f.project) AS project
        FROM file f
        WHERE f.n_messages > 0
          AND NOT EXISTS (
            SELECT 1 FROM message m JOIN file cf ON cf.path = m.path
            WHERE m.chain_hash = f.head_hash
              AND cf.n_messages > f.n_messages)
        GROUP BY f.head_hash
        ORDER BY MAX(f.mtime) DESC
    """).fetchall()


def files_for_session(conn, session_id):
    """Indexed transcripts belonging to a session id (it is the filename stem).

    A session's transcript is copied into many config dirs, so this returns all
    copies, longest first — the longest is the one actually being written to.
    """
    return conn.execute(
        "SELECT * FROM file WHERE path LIKE ? ORDER BY n_messages DESC",
        (f"%/{session_id}.jsonl",)).fetchall()


def newer_than(conn, head_hash, n_messages):
    """The longest conversation that CONTINUES `head_hash`, or None.

    Used to answer "is this window on a stale version?" — if a window's session
    ends at head_hash but some other conversation carries that hash mid-chain,
    that other one has everything this window has, plus more.
    """
    return conn.execute("""
        SELECT cf.head_hash, MIN(cf.path) AS path, MAX(cf.n_messages) AS n_messages,
               MIN(cf.session) AS session, MIN(cf.project) AS project,
               MAX(cf.mtime) AS mtime, COUNT(*) AS n_files
        FROM message m JOIN file cf ON cf.path = m.path
        WHERE m.chain_hash = ? AND cf.n_messages > ?
        GROUP BY cf.head_hash
        ORDER BY MAX(cf.n_messages) DESC LIMIT 1
    """, (head_hash, n_messages)).fetchone()


def _state_file_sessions():
    """{window_index: session_id} from clauthing's runtime tmux state.

    Needed because @session_id is a tmux WINDOW OPTION and dies with the tmux
    server, so after a restart most windows report no session at all.
    """
    try:
        from clauthing.main import get_runtime_tmux_state_file
        f = get_runtime_tmux_state_file(os.environ.get("CLAUTHING_PROFILE"))
        state = json.loads(Path(f).read_text())
    except Exception:
        return {}
    return {idx: (d or {}).get("session_id")
            for idx, d in (state.get("windows") or {}).items()}


def shared_newer(conn, head_hash, n_messages):
    """Conversations that share a common ancestor with `head_hash` AND are
    longer, richest first.

    Broader than newer_than(): that only finds strict continuations (this
    conversation is a prefix of them). This also finds FORKS — a sibling that
    branched from a shared prefix and then grew past us. shared_depth is the
    fork point (how many leading messages are common).
    """
    return conn.execute("""
        WITH me AS (
            SELECT path FROM file WHERE head_hash = ?
            ORDER BY n_messages DESC LIMIT 1
        )
        SELECT cf.head_hash, MIN(cf.path) AS path, MAX(cf.n_messages) AS n_messages,
               MAX(mm.pos) + 1 AS shared_depth,
               MIN(cf.session) AS session, MIN(cf.project) AS project,
               MAX(cf.mtime) AS mtime
        FROM me
        JOIN message mm ON mm.path = me.path
        JOIN message mc ON mc.chain_hash = mm.chain_hash
        JOIN file cf    ON cf.path = mc.path AND cf.head_hash != ?
        WHERE cf.n_messages > ?
        GROUP BY cf.head_hash
        ORDER BY n_messages DESC
    """, (head_hash, head_hash, n_messages)).fetchall()


def copy_count(conn, head_hash):
    """How many files hold this exact conversation (identical `:cd` copies)."""
    r = conn.execute("SELECT COUNT(*) c FROM file WHERE head_hash = ?",
                     (head_hash,)).fetchone()
    return r["c"] if r else 0


def related_conversations(conn, head_hash):
    """Every OTHER conversation that shares history with `head_hash`, classified.

    Returns [(row, relation)] where relation is:
      'ancestor'   a shorter prefix of this one (we continue it)
      'descendant' extends this one (shares our full head, plus more)
      'fork'       shares a prefix then diverges (neither contains the other)

    Identical copies (same head — the `:cd` duplicates) are NOT included; count
    them with copy_count(). "Nothing longer" and "nothing shared" are different
    questions — this answers the second. Two conversations that began from
    different first messages share NO prefix, so unrelated windows show nothing.
    """
    me = conn.execute("SELECT path, n_messages FROM file WHERE head_hash = ? "
                      "ORDER BY n_messages DESC LIMIT 1", (head_hash,)).fetchone()
    if not me:
        return []
    my_len = me["n_messages"]
    rows = conn.execute("""
        SELECT cf.head_hash, MIN(cf.path) AS path, MAX(cf.n_messages) AS n_messages,
               MAX(mm.pos) + 1 AS shared_depth, MIN(cf.session) AS session,
               MIN(cf.project) AS project, MAX(cf.mtime) AS mtime
        FROM message mm
        JOIN message mc ON mc.chain_hash = mm.chain_hash
        JOIN file cf    ON cf.path = mc.path AND cf.head_hash != ?
        WHERE mm.path = ?
        GROUP BY cf.head_hash
        ORDER BY n_messages DESC
    """, (head_hash, me["path"])).fetchall()

    out = []
    for r in rows:
        d = r["shared_depth"]
        if d >= my_len:
            rel = "descendant"                 # contains all of us, plus more
        elif d >= r["n_messages"]:
            rel = "ancestor"                   # we contain all of it
        else:
            rel = "fork"                       # common prefix, then both diverge
        out.append((r, rel))
    return out


def running_sessions(socket="clauthing"):
    """[(window_index, window_name, session_id)] for windows whose tmux
    @session_id is set RIGHT NOW.

    Reads the option directly — NOT the state-file-by-index fallback that
    live_windows() uses, because that fallback mismaps windows after a server
    restart (it once reported the god window as owning pain's session). Windows
    whose option was lost are simply skipped: better omitted than wrong.
    """
    import subprocess
    try:
        r = subprocess.run(
            ["tmux", "-L", socket, "list-windows", "-a", "-F",
             "#{window_index}\t#{window_name}\t#{@session_id}"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    out = []
    for line in (r.stdout or "").strip().splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].strip():
            out.append((parts[0], parts[1], parts[2].strip()))
    return out


def live_windows(socket="clauthing"):
    """[(window_name, session_id)] for the live tmux windows, falling back to
    the runtime state file when the tmux option was lost."""
    import subprocess
    try:
        r = subprocess.run(
            ["tmux", "-L", socket, "list-windows", "-a", "-F",
             "#{window_index}\t#{window_name}\t#{@session_id}"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    by_index = _state_file_sessions()
    out = []
    for line in (r.stdout or "").strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        idx, name = parts[0], parts[1]
        sid = (parts[2].strip() if len(parts) > 2 else "") or by_index.get(idx)
        out.append((name, sid or None))
    return out


def index_session_from_disk(conn, session_id, base=None):
    """Find and index a session's transcripts that aren't in the index yet."""
    found = 0
    for path, session, project in transcript_files(base):
        if path.stem == session_id:
            index_file(conn, path, session, project)
            found += 1
    if found:
        conn.commit()
    return found


def tip_named(conn, window_name):
    """The richest lineage tip whose name matches `window_name`, or None.

    Answers "this window is empty — which conversation was it?" A restore that
    opens a fresh session leaves the real conversation orphaned in the index;
    the window name is the link back to it.
    """
    best = None
    for row in tips(conn):
        if conversation_name(row).lower() != (window_name or "").lower():
            continue
        if best is None or row["n_messages"] > best["n_messages"]:
            best = row
    return best


def check_windows(conn, socket="clauthing", refresh=True):
    """For each live window: is a newer continuation of its session indexed?

    Returns [(window, session_id, current_len, newer_row_or_None, note)].
    Refreshes just those transcripts first — they are being appended right now,
    so a stale index would report false "nothing newer" for every live window.
    """
    results = []
    for name, sid in live_windows(socket):
        if not sid:
            results.append((name, None, 0, None, "no session id on window"))
            continue
        rows = files_for_session(conn, sid)
        if refresh:
            if rows:
                for r in rows:
                    index_file(conn, r["path"], r["session"], r["project"])
                conn.commit()
            else:
                index_session_from_disk(conn, sid)   # never indexed before
            rows = files_for_session(conn, sid)
        if not rows:
            # Empty session: the window has no history at all. Its real
            # conversation is probably orphaned in the index under this name.
            results.append((name, sid, 0, tip_named(conn, name), "empty session"))
            continue
        cur = rows[0]
        newer = newer_than(conn, cur["head_hash"], cur["n_messages"])
        if newer is None:
            # Not stale within its own lineage — but a richer conversation may
            # still exist under this window's name (a different lineage).
            alt = tip_named(conn, name)
            if alt and alt["n_messages"] > cur["n_messages"] * 2:
                results.append((name, sid, cur["n_messages"], alt, "different lineage"))
                continue
        results.append((name, sid, cur["n_messages"], newer, ""))
    return results


def latest_for(conn, path):
    """The longest descendant of `path` (what you should be resuming), or None."""
    row = conn.execute("SELECT head_hash FROM file WHERE path=?", (str(path),)).fetchone()
    if not row or not row["head_hash"]:
        return None
    best = conn.execute("""
        SELECT cf.* FROM message m JOIN file cf ON cf.path = m.path
        WHERE m.chain_hash = ? AND m.path != ?
        ORDER BY cf.n_messages DESC LIMIT 1
    """, (row["head_hash"], str(path))).fetchone()
    return best


# ── cli ──────────────────────────────────────────────────────────────────────

def _short(path, width=58):
    p = str(path)
    return p if len(p) <= width else "…" + p[-(width - 1):]


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="clauthing-sessions",
        description="Index clauthing transcripts and query how they relate.")
    ap.add_argument("--db", help="index location (default: state dir)")
    ap.add_argument("--base", help="session-configs dir")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("update", help="update the index (incremental)")
    p.add_argument("--force", action="store_true", help="re-hash everything")

    sub.add_parser("extends", help="show which transcripts continue which")
    sub.add_parser("dups", help="show byte-identical conversations")
    p = sub.add_parser("tips", help="newest conversation of each lineage")
    p.add_argument("-n", type=int, default=40, help="how many to show")
    p = sub.add_parser("shared", help="conversations sharing an ancestor (forks)")
    p.add_argument("--min-depth", type=int, default=2,
                   help="ignore pairs sharing fewer than N leading messages")
    p.add_argument("--all", action="store_true",
                   help="include pairs where one is just a prefix of the other")
    p = sub.add_parser("latest", help="the longest continuation of a transcript")
    p.add_argument("path")
    p = sub.add_parser("windows", help="are any live windows on a stale version?")
    p.add_argument("--socket", default="clauthing")
    p.add_argument("--no-refresh", action="store_true",
                   help="don't re-index the live transcripts first")

    p = sub.add_parser("running",
                       help="running sessions vs longer relatives (shared ancestor)")
    p.add_argument("--socket", default="clauthing")
    p.add_argument("--all", action="store_true",
                   help="show ALL shared-history relatives (copies/ancestors/forks), "
                        "not only longer ones")
    sub.add_parser("stats", help="index summary")

    args = ap.parse_args(argv)
    # Default: the tips — the current version of each conversation, which is
    # what you almost always want ("which one should I resume?").
    cmd = args.cmd or "tips"
    if cmd == "tips" and not hasattr(args, "n"):
        args.n = 40
    conn = connect(args.db)

    if cmd == "update":
        def prog(i, n):
            print(f"  {i}/{n} files…", file=sys.stderr, flush=True)
        t0 = time.time()
        s = update_index(conn, args.base, force=args.force, progress=prog)
        print(f"indexed {s['files']} distinct files in {time.time()-t0:.1f}s: "
              f"{s['messages']} new messages ({s['unchanged']} unchanged, "
              f"{s['appended']} appended, {s['rebuilt']} rebuilt, "
              f"{s['pruned']} stale rows pruned)")

    elif cmd == "extends":
        pairs = extends_pairs(conn)
        if not pairs:
            print("No extends relationships found (run `update` first?)")
            return 0
        print(f"{len(pairs)} extends relationship(s), biggest continuation first:\n")
        for child_h, parent_h, plen, clen, nfiles in pairs[:40]:
            ps, pp, pn = describe(conn, parent_h)
            cs, cp, cn = describe(conn, child_h)
            print(f"  {ps[:8]}  {plen:5d} msgs  [{pp}]  ({pn} copies)")
            print(f"    └─+{clen-plen}→  {cs[:8]}  {clen:5d} msgs  [{cp}]  ({cn} copies)")

    elif cmd == "dups":
        for head, n, paths in duplicates(conn)[:50]:
            print(f"{n} identical copies ({head[:12]}):")
            for p in paths[:6]:
                print(f"    {_short(p)}")

    elif cmd == "tips":
        rows = tips(conn)
        print(f"{len(rows)} lineage tip(s) — newest first "
              f"(nothing extends these; these are what to resume):\n")
        for r in rows[:args.n]:
            when = time.strftime("%m-%d %H:%M", time.localtime(r["mtime"] or 0))
            copies = f"{r['n_files']:3d}x" if r["n_files"] > 1 else "  -"
            print(f"  {when}  {conversation_name(r):<16.16}  "
                  f"{r['n_messages']:5d} msgs  {copies}  {r['head_hash'][:8]}")

    elif cmd == "shared":
        pairs = (shared_ancestors(conn, args.min_depth) if args.all
                 else forks(conn, args.min_depth))
        kind = "pair(s) sharing an ancestor" if args.all else "genuine fork(s)"
        print(f"{len(pairs)} {kind} (min shared depth {args.min_depth}):\n")
        for a, b, depth, na, nb in pairs[:40]:
            sa, pa, _ = describe(conn, a)
            sb, pb, _ = describe(conn, b)
            print(f"  shared first {depth} msgs, then diverge:")
            print(f"      {sa[:8]}  {na:5d} msgs (+{na-depth})  [{pa}]")
            print(f"      {sb[:8]}  {nb:5d} msgs (+{nb-depth})  [{pb}]")

    elif cmd == "windows":
        rows = check_windows(conn, args.socket, refresh=not args.no_refresh)
        stale = [r for r in rows if r[3]]
        print(f"{len(rows)} live window(s), {len(stale)} on a STALE version:\n")
        for name, sid, cur, newer, note in rows:
            if newer:
                why = note or "newer version"
                print(f"  ⚠ {name:<16.16} {cur:5d} msgs  → {why}: "
                      f"{newer['n_messages']} msgs (+{newer['n_messages'] - cur})")
                print(f"      :resume {Path(newer['path']).stem}")
            elif note:
                print(f"    {name:<16.16} {note}, nothing found under this name")
            else:
                print(f"  ✓ {name:<16.16} {cur:5d} msgs  (newest)")
        if stale:
            print("\nThose windows are missing history that IS still on disk — "
                  "resume the id shown to get it back.")

    elif cmd == "running":
        sessions = running_sessions(args.socket)
        print(f"{len(sessions)} running session(s) with a live id:\n")
        for idx, name, sid in sessions:
            fs = files_for_session(conn, sid)
            if not fs:
                index_session_from_disk(conn, sid)
                fs = files_for_session(conn, sid)
            if not fs:
                print(f"  {idx:>2} {name:<14} {sid[:8]}   (no transcript on disk)")
                continue
            cur = fs[0]
            head = f"{idx:>2} {name:<14} {sid[:8]}   {cur['n_messages']} msgs"
            if args.all:
                copies = copy_count(conn, cur["head_hash"])
                other = related_conversations(conn, cur["head_hash"])
                print(f"  {head}   ({copies} identical copies)")
                if not other:
                    print("      no ancestor / fork / longer version shares its history")
                for r, k in other[:8]:
                    print(f"      {k:<10} {r['n_messages']:5d} msgs  "
                          f"shared {r['shared_depth']}   :resume {Path(r['path']).stem}")
                continue
            rel = shared_newer(conn, cur["head_hash"], cur["n_messages"])
            if not rel:
                print(f"  ✓ {head}   (nothing longer shares its history)")
                continue
            print(f"  ⚠ {head}")
            for r in rel[:5]:
                kind = ("continues it" if r["shared_depth"] >= cur["n_messages"]
                        else f"forks at msg {r['shared_depth']}")
                print(f"      → {r['n_messages']:5d} msgs (+{r['n_messages']-cur['n_messages']})"
                      f"  {kind}   :resume {Path(r['path']).stem}")

    elif cmd == "latest":
        best = latest_for(conn, args.path)
        if not best:
            print("No continuation found — this is already the tip.")
        else:
            print(f"resume instead: {best['path']}  ({best['n_messages']} msgs)")

    else:
        f = conn.execute("SELECT COUNT(*) c, SUM(n_messages) m FROM file").fetchone()
        print(f"db:       {args.db or db_path()}")
        print(f"files:    {f['c'] or 0}")
        print(f"messages: {f['m'] or 0}")
        print(f"extends:  {len(extends_pairs(conn))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
