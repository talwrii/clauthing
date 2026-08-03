#!/usr/bin/env python3
"""Session management for clauthing.

Sessions are tracked in:
- ~/.local/state/clauthing/sessions/ - Session metadata
- ~/.config/clauthing/open-sessions.json - List of currently open sessions

Note: This is for debugging and liable to change.
"""
import json
import re
import subprocess
from pathlib import Path

from clauthing.logging import log, run


def get_state_dir():
    """Get the XDG state directory for clauthing."""
    import os
    xdg_state = os.environ.get('XDG_STATE_HOME')
    if xdg_state:
        state_dir = Path(xdg_state) / "clauthing"
    else:
        state_dir = Path.home() / ".local" / "state" / "clauthing"

    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def save_session_metadata(session_id, name, path):
    """Save session metadata to state directory."""
    state_dir = get_state_dir()
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(exist_ok=True)

    metadata_file = sessions_dir / f"{session_id}.json"
    metadata = {
        "name": name,
        "path": path,
        "created": run(["date", "-Iseconds"], capture_output=True, text=True).stdout.strip()
    }

    metadata_file.write_text(json.dumps(metadata, indent=2))


def get_clauthing_window(session_id):
    """Return the stable clauthing_window id stored in session metadata, or None.

    clauthing_window identifies a *window* (1-to-(0 or 1) with sessions) and is
    stable across :cd clones and restarts, whereas session_id rotates. See
    claudes.md.
    """
    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"
    if metadata_file.exists():
        try:
            return json.loads(metadata_file.read_text()).get("clauthing_window")
        except Exception:
            pass
    return None


def ensure_clauthing_window(session_id):
    """Return this session's clauthing_window, minting + persisting one if absent.

    Stored in the session metadata file so it carries across :cd clones (via
    carry_over_session_state) and restarts. Used as the stable key for
    window-scoped state (messages, attention, notes).
    """
    import uuid
    state_dir = get_state_dir()
    sessions_dir = state_dir / "sessions"
    metadata_file = sessions_dir / f"{session_id}.json"
    metadata = {}
    if metadata_file.exists():
        try:
            metadata = json.loads(metadata_file.read_text())
        except Exception:
            metadata = {}
    cw = metadata.get("clauthing_window")
    if not cw:
        cw = str(uuid.uuid4())
        metadata["clauthing_window"] = cw
        try:
            sessions_dir.mkdir(parents=True, exist_ok=True)
            metadata_file.write_text(json.dumps(metadata, indent=2))
        except Exception as e:
            log(f"Error saving clauthing_window for {session_id}: {e}", None)
    return cw


def session_transcripts_dir(profile=None):
    """Where claude keeps this profile's per-session transcripts."""
    base = Path.home() / ".config" / "clauthing"
    if profile:
        base = base / "other-profiles" / profile
    return base / "session-configs"


def session_last_activity(session_id, profile=None):
    """Timestamp of this session's last conversation turn, or 0 if it never had
    one (transcript mtime).

    This is the only trustworthy recency signal: the session metadata file's
    mtime gets touched by a restore attempt, so it reports failed restores as
    "recent" and ranks genuinely-active sessions below dead ones.
    """
    if not session_id:
        return 0
    newest = 0
    try:
        for f in session_transcripts_dir(profile).glob(
                f"{session_id}/projects/*/{session_id}.jsonl"):
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
    except Exception:
        pass
    return newest


_UUIDISH = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{4,}$", re.I)


def _is_unnamed(session_id, name):
    """A session whose 'name' is a uuid was never actually named — restoring it
    yields a window titled with a raw session id, not a real window."""
    return not name or name == session_id or bool(_UUIDISH.match(name))


def restorable_sessions(profile=None):
    """Exactly one session to restore per window NAME, best candidate first.

    open-sessions.json only drops an entry on a CLEAN claude exit, so a killed
    tmux server (kitty restart, reboot) leaves every session in it forever. It
    had grown to 47 entries — mostly long-dead — which is why restore opened a
    pile of duplicate windows ("two windows called state").

    One window per NAME: a name is one window in the user's model, and inboxes
    are keyed by name, so two live windows sharing a name would share an inbox.
    We never drop a name — a name whose sessions are all empty husks still gets
    its window back (just without history), because losing the window entirely
    is the worse failure. Within a name we prefer the most recently ACTIVE
    session, falling back to the last one added.

    Older same-name sessions aren't deleted — they stay resumable via :resume.
    """
    sessions = get_open_sessions(profile)
    order = {sid: i for i, sid in enumerate(sessions)}
    best = {}                          # name -> (has_transcript, ts, order, sid)
    for sid in sessions:
        name = get_session_name(sid) or sid
        if _is_unnamed(sid, name):     # uuid-named husk, not a real window
            continue
        ts = session_last_activity(sid, profile)
        key = (1 if ts else 0, ts, order[sid])
        if name not in best or key > best[name][0]:
            best[name] = (key, sid)
    # most recently active first, so restore order is most-relevant-first
    return [sid for _key, sid in sorted(best.values(), key=lambda x: x[0], reverse=True)]


def prune_open_sessions(profile=None):
    """Rewrite open-sessions.json down to what restore would actually open, so
    the list stops growing without bound. Returns (kept, dropped)."""
    before = get_open_sessions(profile)
    keep = restorable_sessions(profile)
    if len(keep) != len(before):
        f = get_open_sessions_file(profile)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"sessions": keep}, indent=2))
        except Exception as e:
            log(f"Error pruning open sessions: {e}", profile)
    return keep, [s for s in before if s not in set(keep)]


def inbox_slug(window_name):
    """Filesystem-safe inbox stem for a window name ('work alive' -> 'work-alive')."""
    s = "".join(c if (c.isalnum() or c in "-_.") else "-"
                for c in str(window_name or "").strip())
    return s.strip("-") or "unnamed"


def get_messages_dir(profile=None):
    """Directory holding message inboxes.

    Messages are user data, not runtime state, so they live under the state dir
    rather than the runtime dir (which is /tmp and is wiped on reboot).
    """
    base = get_state_dir()
    d = (base / "other-profiles" / profile / "messages") if profile else base / "messages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def inbox_path(window_name, profile=None):
    """Inbox file for a window NAME.

    Keyed by name rather than clauthing_window: the id is minted fresh whenever
    the tmux server dies (kitty restart / reboot), which stranded every inbox.
    The name is what you actually address (:msg god ...) and it persists. Two
    windows sharing a name therefore share an inbox — intended: you send to
    "god", not to a particular incarnation of it.
    """
    return get_messages_dir(profile) / f"{inbox_slug(window_name)}.jsonl"


def rename_inbox(old_name, new_name, profile=None):
    """Follow a window rename so its messages aren't stranded under the old
    name. Merges (keeping both sides' messages) if the target already exists."""
    src, dst = inbox_path(old_name, profile), inbox_path(new_name, profile)
    if src == dst or not src.exists():
        return
    try:
        if dst.exists():
            with open(dst, "a") as out:
                out.write(src.read_text())
            src.unlink()
        else:
            src.rename(dst)
    except Exception as e:
        log(f"Error moving inbox {old_name} -> {new_name}: {e}", profile)


def window_ids_file(profile=None):
    """Durable map of window-name -> clauthing_window, per tmux socket.

    The live identity normally rides on the tmux window option
    @clauthing_window. That dies with the tmux server (kitty restart, reboot),
    which used to mint a fresh id for every window and strand all their
    inboxes. This file is the off-tmux backup so identity survives a restart.
    """
    return get_state_dir() / "window-ids.json"


def _load_window_ids(profile=None):
    try:
        return json.loads(window_ids_file(profile).read_text())
    except Exception:
        return {}


def remember_window_id(socket, window_name, clauthing_window, profile=None):
    """Record that `window_name` on `socket` owns `clauthing_window`."""
    if not (socket and window_name and clauthing_window):
        return
    data = _load_window_ids(profile)
    data.setdefault(socket, {})[window_name] = clauthing_window
    try:
        f = window_ids_file(profile)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, indent=2))
    except Exception as e:
        log(f"Error saving window id for {window_name}: {e}", profile)


def recall_window_id(socket, window_name, profile=None):
    """The clauthing_window `window_name` had before the tmux server died, or
    None. Callers must still run the uniqueness guard — two windows sharing a
    name would otherwise both claim the id."""
    if not (socket and window_name):
        return None
    return _load_window_ids(profile).get(socket, {}).get(window_name)


def get_window_state_dir(profile=None):
    """Directory for per-window state files, keyed by clauthing_window."""
    if profile:
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"
    return config_dir / "window-state"


def load_window_state(clauthing_window, profile=None):
    """Load the per-window state dict for a clauthing_window (or {})."""
    if not clauthing_window:
        return {}
    f = get_window_state_dir(profile) / f"{clauthing_window}.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {}


def save_window_state(clauthing_window, state, profile=None):
    """Persist the per-window state dict for a clauthing_window."""
    if not clauthing_window:
        return
    d = get_window_state_dir(profile)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{clauthing_window}.json").write_text(json.dumps(state, indent=2))


def set_clauthing_window(session_id, clauthing_window):
    """Record a specific clauthing_window on a session's metadata.

    Used when a window is respawned and the new session must adopt the window's
    existing id (the window owns the id, not the session).
    """
    state_dir = get_state_dir()
    sessions_dir = state_dir / "sessions"
    metadata_file = sessions_dir / f"{session_id}.json"
    metadata = {}
    if metadata_file.exists():
        try:
            metadata = json.loads(metadata_file.read_text())
        except Exception:
            metadata = {}
    if metadata.get("clauthing_window") == clauthing_window:
        return
    metadata["clauthing_window"] = clauthing_window
    try:
        sessions_dir.mkdir(parents=True, exist_ok=True)
        metadata_file.write_text(json.dumps(metadata, indent=2))
    except Exception as e:
        log(f"Error setting clauthing_window for {session_id}: {e}", None)


def get_session_name(session_id):
    """Get session name from metadata, or return session_id if not found."""
    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"

    if metadata_file.exists():
        try:
            metadata = json.loads(metadata_file.read_text())
            return metadata.get("name", session_id)
        except:
            pass

    return session_id


def get_open_sessions_file(profile=None):
    """Get the persistent open sessions file path."""
    if profile:
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"
    return config_dir / "open-sessions.json"


def add_open_session(session_id, profile=None):
    """Add a session to the list of open sessions."""
    sessions_file = get_open_sessions_file(profile)
    sessions_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        if sessions_file.exists():
            sessions = json.loads(sessions_file.read_text())
        else:
            sessions = {"sessions": []}

        # Add session if not already in list
        if session_id not in sessions.get("sessions", []):
            sessions.setdefault("sessions", []).append(session_id)
            sessions_file.write_text(json.dumps(sessions, indent=2))
            log(f"Added session to open-sessions: {session_id}", profile)
    except Exception as e:
        log(f"Error adding session to open-sessions: {e}", profile)


def remove_open_session(session_id, profile=None):
    """Remove a session from the list of open sessions."""
    sessions_file = get_open_sessions_file(profile)

    try:
        if sessions_file.exists():
            sessions = json.loads(sessions_file.read_text())
            session_list = sessions.get("sessions", [])
            if session_id in session_list:
                session_list.remove(session_id)
                sessions["sessions"] = session_list
                sessions_file.write_text(json.dumps(sessions, indent=2))
                log(f"Removed session from open-sessions: {session_id}", profile)
    except Exception as e:
        log(f"Error removing session from open-sessions: {e}", profile)


def mark_session_has_messages(session_id):
    """Record in the session metadata that this session has at least one
    user prompt. Used by the restore loop to choose between `claude --resume`
    (has messages, jsonl exists) and a fresh `claude --session-id` spawn.
    """
    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"
    try:
        if metadata_file.exists():
            metadata = json.loads(metadata_file.read_text())
        else:
            metadata = {}
        if metadata.get("has_messages"):
            return
        metadata["has_messages"] = True
        metadata_file.parent.mkdir(parents=True, exist_ok=True)
        metadata_file.write_text(json.dumps(metadata, indent=2))
    except Exception as e:
        log(f"Error marking session has_messages: {e}", None)


def session_metadata_has_messages(session_id):
    """Return True if the session metadata records at least one user prompt.
    (Distinct from session_utils.session_has_messages, which inspects the
    jsonl file on disk.)"""
    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"
    try:
        if metadata_file.exists():
            return bool(json.loads(metadata_file.read_text()).get("has_messages"))
    except Exception:
        pass
    return False


def resolve_session_prefix(prefix):
    """Resolve a (possibly partial) session id to full ids by prefix match.

    Returns the sorted list of session ids whose id starts with `prefix`,
    drawn from the session metadata dir. Callers treat exactly one match as a
    hit, zero as not-found, and more than one as ambiguous. Guards against a
    dropped/typo'd character in a pasted id (a unique prefix still resolves).
    """
    prefix = (prefix or "").strip()
    if not prefix:
        return []
    sessions_dir = get_state_dir() / "sessions"
    try:
        return sorted(p.stem for p in sessions_dir.glob(f"{prefix}*.json"))
    except Exception:
        return []


def get_open_sessions(profile=None):
    """Get list of open sessions."""
    sessions_file = get_open_sessions_file(profile)

    try:
        if sessions_file.exists():
            sessions = json.loads(sessions_file.read_text())
            return sessions.get("sessions", [])
    except:
        pass

    return []


