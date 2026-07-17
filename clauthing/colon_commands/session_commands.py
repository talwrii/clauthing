"""Session listing, resume, spawn, and messaging colon commands.

Commands: :sessions, :resume, :resume-new, :spawn, :current-sessions,
          :login-all, :reload-all, :send, :msgs, :message, :msg
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from clauthing.colon_command import command, send_tmux_message, get_state_dir
from clauthing.colon_command import get_title_history_file, record_title
from clauthing.logging import log, run
from clauthing.session import get_session_name


@command(':current-sessions', independent=True)
def cmd_current_sessions(ctx):
    from clauthing.claude import get_running_sessions
    from clauthing.hooks import _load_attention, _load_idle
    from clauthing.events import get_all_windows, get_runtime_dir

    sessions = get_running_sessions(ctx.profile)
    if not sessions:
        return ctx.stop("No currently running sessions")

    attention = _load_attention(ctx.profile) or {}
    idle = _load_idle(ctx.profile) or {}
    windows = get_all_windows(ctx.profile) or {}

    # Count unread per session from inbox files.
    msgs_dir = get_runtime_dir(ctx.profile) / "messages"
    unread = {}
    if msgs_dir.exists():
        for inbox in msgs_dir.glob("*.jsonl"):
            try:
                n = sum(1 for line in inbox.read_text().splitlines()
                        if line and not json.loads(line).get("read"))
                if n:
                    unread[inbox.stem] = n
            except Exception:
                continue

    from clauthing.session import get_clauthing_window

    # Name each session by the window its PROCESS actually runs in. The windows
    # state file's title goes stale (only refreshed on reload/rename), and
    # @session_id can be stale or even duplicated across two windows — so the
    # process tree is the only ground truth for "which window is this session".
    pane_windows = {}
    try:
        r = run(["tmux", "-L", ctx.socket, "list-windows", "-F",
                 "#{pane_pid}\t#{window_name}"],
                capture_output=True, text=True)
        for line in (r.stdout or "").splitlines():
            pid_s, _, name = line.partition("\t")
            if pid_s.isdigit():
                pane_windows[int(pid_s)] = name
    except Exception:
        pass

    def _window_for_pid(pid):
        """Walk up the parent chain until we reach a pane we know."""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        for _ in range(20):
            if pid in pane_windows:
                return pane_windows[pid]
            if pid <= 1:
                return None
            try:
                with open(f"/proc/{pid}/status") as f:
                    pid = next(int(l.split()[1]) for l in f
                               if l.startswith("PPid:"))
            except Exception:
                return None
        return None

    now = time.time()
    lines = ["Currently running sessions:\n"]
    for i, sess in enumerate(sessions, 1):
        cwd = sess.get('cwd', '?')
        session_id = sess['session_id']
        cw = get_clauthing_window(session_id)
        pid = sess['pid']
        live = _window_for_pid(pid)
        title = live or (windows.get(session_id) or {}).get("title")
        label = f"[{title}] " if title else ""

        tags = []
        if not live:
            tags.append("no window")   # running, but not attached to any window
        if cw and cw in attention:
            tags.append("urgent")
        n = unread.get(cw, 0) if cw else 0
        if n:
            tags.append(f"msgs:{n}")
        if cw and cw in idle:
            secs = int(now - idle[cw].get("ts", now))
            if secs < 60:
                tags.append(f"idle {secs}s")
            elif secs < 3600:
                tags.append(f"idle {secs // 60}m")
            else:
                tags.append(f"idle {secs // 3600}h{(secs % 3600) // 60}m")
        state = f" [{' | '.join(tags)}]" if tags else ""

        lines.append(f"{i}. {label}{session_id[:8]}... (PID {pid}) - {cwd}{state}")

    ctx.message(f"✓ {len(sessions)} running")
    return ctx.stop("\n".join(lines))


@command(':sessions', independent=True)
def cmd_sessions(ctx):
    from clauthing.claude import get_recent_sessions
    from datetime import datetime

    limit = 10
    arg = ctx.args.strip()
    if arg and arg.isdigit():
        limit = int(arg)

    sessions = get_recent_sessions(ctx.profile, limit=limit)
    if not sessions:
        return ctx.stop("No recent sessions found")

    lines = ["Recent sessions (ordered by last activity):\n"]
    for i, sess in enumerate(sessions, 1):
        session_id = sess['session_id']
        title = sess.get('title')
        cwd = sess.get('cwd') or '?'
        mtime = datetime.fromtimestamp(sess['last_modified']).strftime('%Y-%m-%d %H:%M')
        last_msg = sess.get('last_message') or ''

        if title:
            main_line = f"{i}. [{title}] {cwd} ({mtime})"
        else:
            main_line = f"{i}. {session_id[:8]}... - {cwd} ({mtime})"
        lines.append(main_line)

        if last_msg:
            last_msg = last_msg.replace('\n', ' ').strip()
            if len(last_msg) > 40:
                last_msg = last_msg[:40] + "..."
            lines.append(f"   └─ {last_msg}")

    lines.append(f"\nUse :resume <number> or :resume <session-id> to resume")
    ctx.message(f"✓ {len(sessions)} sessions")
    return ctx.stop("\n".join(lines))


@command(':resume')
def cmd_resume(ctx):
    from clauthing.claude import get_recent_sessions, new_window
    from datetime import datetime
    import tempfile

    arg = ctx.args.strip()
    target_session_id = None

    if not arg:
        sessions = get_recent_sessions(ctx.profile, limit=200)
        if not sessions:
            return ctx.stop("No recent sessions found")

        from clauthing.claude_utils import encode_project_path
        if ctx.profile:
            projects_root = (Path.home() / ".config" / "clauthing"
                             / "other-profiles" / ctx.profile / "claude-data" / "projects")
        else:
            projects_root = Path.home() / ".config" / "clauthing" / "claude-data" / "projects"

        def count_user_messages(sid, cwd):
            if not cwd:
                return 0
            sf = projects_root / encode_project_path(cwd) / f"{sid}.jsonl"
            if not sf.exists():
                return 0
            try:
                # Each user message = one '"type":"user"' substring (or with space).
                text = sf.read_text(errors="ignore")
                return text.count('"type":"user"') + text.count('"type": "user"')
            except Exception:
                return 0

        fzf_lines = []
        for sess in sessions:
            sid = sess['session_id']
            title = sess.get('title') or sid[:8]
            cwd = sess.get('cwd') or '?'
            mtime = datetime.fromtimestamp(sess['last_modified']).strftime('%Y-%m-%d %H:%M')
            last_msg = (sess.get('last_message') or '').replace('\n', ' ').strip()
            if len(last_msg) > 60:
                last_msg = last_msg[:60] + '...'
            n_msgs = count_user_messages(sid, sess.get('cwd'))
            fzf_lines.append(f"{sid}\t{title}\t{cwd}\t{mtime}\t{n_msgs}msg\t{last_msg}")

        tmp_in = Path(tempfile.mktemp())
        tmp_out = Path(tempfile.mktemp())
        tmp_in.write_text("\n".join(fzf_lines))
        clauthing_path = shutil.which("clauthing") or "clauthing"
        profile_arg = f"--profile {ctx.profile} " if ctx.profile else ""
        preview_cmd = (
            f"{clauthing_path} {profile_arg}--session-preview {{1}} --preview-cwd {{3}}"
        )
        subprocess.run([
            "tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "90%", "-h", "85%",
            f"cat {tmp_in} | fzf --delimiter='\\t' --with-nth=2,3,4,5,6 "
            f"--preview=\"{preview_cmd}\" --preview-window=right:50%:wrap "
            f"--header='Select session to resume' > {tmp_out}"
        ])
        sel = tmp_out.read_text().strip() if tmp_out.exists() else ""
        tmp_in.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)
        if not sel:
            return ctx.stop("Cancelled")
        target_session_id = sel.split('\t')[0]
    elif arg.isdigit():
        sessions = get_recent_sessions(ctx.profile, limit=10)
        index = int(arg) - 1
        if 0 <= index < len(sessions):
            target_session_id = sessions[index]['session_id']
        else:
            return ctx.stop(f"❌ Session number {arg} not found")
    else:
        target_session_id = arg
        # Resolve arg → session id: exact id, else unique id-prefix (also
        # recovers a dropped char in a pasted id), else the most recent
        # session whose NAME matches (e.g. :resume pain).
        if not (get_state_dir() / "sessions" / f"{arg}.json").exists():
            from clauthing.session import resolve_session_prefix
            from clauthing.claude import resolve_session_by_name
            matches = resolve_session_prefix(arg)
            if len(matches) == 1:
                target_session_id = matches[0]
            elif len(matches) > 1:
                return ctx.stop(
                    f"❌ Ambiguous prefix '{arg}' — {len(matches)} sessions match")
            else:
                by_name = resolve_session_by_name(arg, ctx.profile)
                if by_name:
                    target_session_id = by_name
                else:
                    return ctx.stop(
                        f"❌ No session matching '{arg}' (by id or name)")

    # Look up the session's stored path / name from metadata so the new
    # window opens in the right cwd with the right title.
    state_dir = get_state_dir()
    meta_file = state_dir / "sessions" / f"{target_session_id}.json"
    session_path = ctx.cwd
    session_name = target_session_id[:8]
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            session_path = meta.get("path", session_path)
            if meta.get("name"):
                session_name = meta["name"]
        except Exception:
            pass

    if ctx.socket.startswith("cl1-"):
        # One-tab mode: only one window allowed, so boomerang-replace via
        # @startup_command + respawn-pane (same flow as :cd).
        # Include rename-window so the title reflects the resumed session.
        startup_cmd = (
            f'tmux -L {ctx.socket} rename-window "{session_name}" 2>/dev/null; '
            f'SESSION_ID="{target_session_id}"; cd "{session_path}"'
        )
        try:
            subprocess.run(
                ["tmux", "-L", ctx.socket, "set-option", "-w",
                 "@startup_command", startup_cmd],
                check=True, timeout=5,
            )
            r = subprocess.run(
                ["tmux", "-L", ctx.socket, "display-message", "-p", "#{pane_id}"],
                capture_output=True, text=True, timeout=5,
            )
            pane_id = r.stdout.strip()
            target_arg = f"-t {pane_id}" if pane_id else ""
            subprocess.Popen(
                ["sh", "-c",
                 f"sleep 0.5 && tmux -L {ctx.socket} respawn-pane -k {target_arg} 2>/dev/null"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            log(f":resume one-tab boomerang failed: {e}", ctx.profile)
            return ctx.stop(f"❌ Resume failed: {e}")
    else:
        # Multi-tab mode: open a new tmux window running clauthing --new-claude
        # --resume-session. The new window's clauthing process sets @session_id
        # itself; we don't call new_window() here (which would clobber the
        # current window and trip the window-1 restore logic).
        clauthing_path = shutil.which("clauthing") or "clauthing"
        cmd_parts = [clauthing_path]
        if ctx.profile:
            cmd_parts.extend(["--profile", ctx.profile])
        cmd_parts.extend(["--new-claude", "--resume-session", target_session_id])
        cmd_str = " ".join(cmd_parts)
        try:
            subprocess.run(
                ["tmux", "-L", ctx.socket, "new-window",
                 "-c", session_path, "-n", session_name, cmd_str],
                check=True, timeout=5,
            )
        except Exception as e:
            log(f":resume multi-tab new-window failed: {e}", ctx.profile)
            return ctx.stop(f"❌ Resume failed: {e}")

    ctx.message(f"✓ Resuming {session_name}")
    return ctx.stop(f"✓ Opening session {target_session_id[:8]}... in new window")


def _resume_picker(ctx):
    """Show the fzf picker used by :resume / :resume-here.

    Returns the chosen session_id or None on cancel.
    """
    from clauthing.claude import get_recent_sessions
    from datetime import datetime
    from clauthing.claude_utils import encode_project_path
    import tempfile

    sessions = get_recent_sessions(ctx.profile, limit=200)
    if not sessions:
        return None

    if ctx.profile:
        projects_root = (Path.home() / ".config" / "clauthing"
                         / "other-profiles" / ctx.profile / "claude-data" / "projects")
    else:
        projects_root = Path.home() / ".config" / "clauthing" / "claude-data" / "projects"

    def count_user_messages(sid, cwd):
        if not cwd:
            return 0
        sf = projects_root / encode_project_path(cwd) / f"{sid}.jsonl"
        if not sf.exists():
            return 0
        try:
            text = sf.read_text(errors="ignore")
            return text.count('"type":"user"') + text.count('"type": "user"')
        except Exception:
            return 0

    fzf_lines = []
    for sess in sessions:
        sid = sess['session_id']
        title = sess.get('title') or sid[:8]
        cwd = sess.get('cwd') or '?'
        mtime = datetime.fromtimestamp(sess['last_modified']).strftime('%Y-%m-%d %H:%M')
        last_msg = (sess.get('last_message') or '').replace('\n', ' ').strip()
        if len(last_msg) > 60:
            last_msg = last_msg[:60] + '...'
        n_msgs = count_user_messages(sid, sess.get('cwd'))
        fzf_lines.append(f"{sid}\t{title}\t{cwd}\t{mtime}\t{n_msgs}msg\t{last_msg}")

    tmp_in = Path(tempfile.mktemp())
    tmp_out = Path(tempfile.mktemp())
    tmp_in.write_text("\n".join(fzf_lines))
    clauthing_path = shutil.which("clauthing") or "clauthing"
    profile_arg = f"--profile {ctx.profile} " if ctx.profile else ""
    preview_cmd = (
        f"{clauthing_path} {profile_arg}--session-preview {{1}} --preview-cwd {{3}}"
    )
    subprocess.run([
        "tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "90%", "-h", "85%",
        f"cat {tmp_in} | fzf --delimiter='\\t' --with-nth=2,3,4,5,6 "
        f"--preview=\"{preview_cmd}\" --preview-window=right:50%:wrap "
        f"--header='Select session to resume' > {tmp_out}"
    ])
    sel = tmp_out.read_text().strip() if tmp_out.exists() else ""
    tmp_in.unlink(missing_ok=True)
    tmp_out.unlink(missing_ok=True)
    if not sel:
        return None
    return sel.split('\t')[0]


@command(':resume-here')
def cmd_resume_here(ctx):
    """Resume a session IN THE CURRENT WINDOW (respawn-pane boomerang).

    Same picker / preview as :resume, but instead of spawning a new tmux
    window, swaps the resumed claude into the existing pane via the
    `@startup_command` + `respawn-pane -k` flow (matches :reload / :login).
    """
    arg = ctx.args.strip()
    target_session_id = None

    if not arg:
        target_session_id = _resume_picker(ctx)
        if not target_session_id:
            return ctx.stop("Cancelled")
    elif arg.isdigit():
        from clauthing.claude import get_recent_sessions
        sessions = get_recent_sessions(ctx.profile, limit=10)
        index = int(arg) - 1
        if 0 <= index < len(sessions):
            target_session_id = sessions[index]['session_id']
        else:
            return ctx.stop(f"❌ Session number {arg} not found")
    else:
        target_session_id = arg
        # Resolve arg → session id: exact id, else unique id-prefix (also
        # recovers a dropped char in a pasted id), else the most recent
        # session whose NAME matches (e.g. :resume pain).
        if not (get_state_dir() / "sessions" / f"{arg}.json").exists():
            from clauthing.session import resolve_session_prefix
            from clauthing.claude import resolve_session_by_name
            matches = resolve_session_prefix(arg)
            if len(matches) == 1:
                target_session_id = matches[0]
            elif len(matches) > 1:
                return ctx.stop(
                    f"❌ Ambiguous prefix '{arg}' — {len(matches)} sessions match")
            else:
                by_name = resolve_session_by_name(arg, ctx.profile)
                if by_name:
                    target_session_id = by_name
                else:
                    return ctx.stop(
                        f"❌ No session matching '{arg}' (by id or name)")

    # Look up session metadata for cwd + name.
    state_dir = get_state_dir()
    meta_file = state_dir / "sessions" / f"{target_session_id}.json"
    session_path = ctx.cwd
    session_name = target_session_id[:8]
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            session_path = meta.get("path", session_path)
            if meta.get("name"):
                session_name = meta["name"]
        except Exception:
            pass

    socket = ctx.socket
    # Boomerang: rename the current window, set @startup_command so the
    # respawned --new-claude resumes the chosen session, then respawn-pane.
    startup_cmd = (
        f'tmux -L {socket} rename-window "{session_name}" 2>/dev/null; '
        f'SESSION_ID="{target_session_id}"; cd "{session_path}"'
    )
    try:
        subprocess.run(
            ["tmux", "-L", socket, "set-option", "-w",
             "@startup_command", startup_cmd],
            check=True, timeout=5,
        )
        r = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        pane_id = r.stdout.strip()
        target_arg = f"-t {pane_id}" if pane_id else ""
        subprocess.Popen(
            ["sh", "-c",
             f"sleep 0.5 && tmux -L {socket} respawn-pane -k {target_arg} 2>/dev/null"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        log(f":resume-here boomerang failed: {e}", ctx.profile)
        return ctx.stop(f"❌ Resume failed: {e}")

    ctx.message(f"✓ Resuming {session_name} in place")
    return ctx.stop(f"✓ Resuming {target_session_id[:8]}... in current window")


@command(':resume-new')
def cmd_resume_new(ctx):
    arg = ctx.args.strip()
    socket = ctx.socket
    profile = ctx.profile

    if not arg:
        from clauthing.claude import get_recent_sessions
        from datetime import datetime
        sessions = get_recent_sessions(profile, limit=10)
        if not sessions:
            return ctx.stop("No recent sessions found")
        lines = ["Recent sessions:\n"]
        for i, sess in enumerate(sessions, 1):
            sid = sess['session_id']
            cwd = sess.get('cwd', '?')
            mtime = datetime.fromtimestamp(sess['last_modified']).strftime('%Y-%m-%d %H:%M')
            lines.append(f"{i}. {sid[:8]}... - {cwd} (last: {mtime})")
        lines.append(f"\nUse :resume-new <number> or :resume-new <session-id>")
        ctx.message(f"✓ {len(sessions)} sessions")
        return ctx.stop("\n".join(lines))

    # Resolve session ID
    target_session_id = None
    target_cwd = None
    if arg.isdigit():
        from clauthing.claude import get_recent_sessions
        sessions = get_recent_sessions(profile, limit=10)
        index = int(arg) - 1
        if 0 <= index < len(sessions):
            target_session_id = sessions[index]['session_id']
            target_cwd = sessions[index].get('cwd')
        else:
            return ctx.stop(f"❌ Session number {arg} not found")
    else:
        target_session_id = arg
        # Look up cwd from projects
        if profile:
            base_config = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
        else:
            base_config = Path.home() / ".config" / "clauthing"
        projects_dir = base_config / "claude-data" / "projects"
        if projects_dir.exists():
            for proj_dir in projects_dir.iterdir():
                if proj_dir.is_dir() and (proj_dir / f"{target_session_id}.jsonl").exists():
                    path_hash = proj_dir.name
                    if path_hash.startswith('-'):
                        path_hash = path_hash[1:]
                    parts = path_hash.split('-')
                    for num_slashes in range(len(parts), 0, -1):
                        candidate = '/' + '/'.join(parts[:num_slashes])
                        if num_slashes < len(parts):
                            candidate += '-' + '-'.join(parts[num_slashes:])
                        if Path(candidate).exists():
                            target_cwd = candidate
                            break
                    if not target_cwd:
                        target_cwd = '/' + '/'.join(parts)
                    break

    # Look up title
    session_title = None
    try:
        from clauthing.session import get_state_dir as sess_get_state_dir
        state_dir = sess_get_state_dir()
        metadata_file = state_dir / "sessions" / f"{target_session_id}.json"
        if metadata_file.exists():
            metadata = json.loads(metadata_file.read_text())
            name = metadata.get("name")
            if name and name != target_session_id and not name.startswith("clauthing-"):
                if not (len(name) == 36 and name.count('-') == 4):
                    session_title = name
    except:
        pass

    # Build command
    clauthing_path = shutil.which("clauthing") or "clauthing"
    cmd = [clauthing_path]
    if profile:
        cmd.extend(["--profile", profile])
    cmd.append("--one-tab")
    cmd.extend(["--resume-session", target_session_id])
    if target_cwd and Path(target_cwd).exists():
        cmd.extend(["--cwd", target_cwd])
    if session_title:
        cmd.extend(["--window-name", session_title])

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.2)
        if proc.poll() is not None:
            _, stderr = proc.communicate()
            if stderr:
                return ctx.stop(f"❌ Error: {stderr.decode()}")
    except Exception as e:
        return ctx.stop(f"❌ Failed to spawn: {e}")

    cwd_msg = f" in {target_cwd}" if target_cwd else ""
    ctx.message(f"✓ Spawning new window{cwd_msg[:30]}")
    return ctx.stop(f"✓ Resuming {target_session_id[:8]}...{cwd_msg} in new clauthing window")


def _unread_message_signals(profile=None):
    """Return [(clauthing_window, ts), ...] for windows with unread messages.

    Inboxes are keyed by clauthing_window (the stable window id), so the stem
    is the window id. `ts` is the timestamp of the newest unread message.
    """
    from clauthing.events import get_runtime_dir
    msgs_dir = get_runtime_dir(profile) / "messages"
    if not msgs_dir.exists():
        return []
    out = []
    for inbox in msgs_dir.glob("*.jsonl"):
        try:
            latest = 0
            for line in inbox.read_text().splitlines():
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if not msg.get("read"):
                    ts = msg.get("ts", 0)
                    if ts > latest:
                        latest = ts
            if latest:
                out.append((inbox.stem, latest))
        except Exception:
            continue
    return out


def _try_switch_to(clauthing_window, socket, dry_run=False):
    """Resolve clauthing_window → window on socket and (unless dry_run) select it.

    Returns the window_id on success, None on failure.
    """
    if not socket or socket.startswith("cl1-"):
        return None
    try:
        result = subprocess.run(
            ["tmux", "-L", socket, "list-windows", "-F", "#{window_id} #{@clauthing_window}"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    wid = None
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == clauthing_window:
            wid = parts[0]
            break
    if not wid:
        return None
    if dry_run:
        return wid
    try:
        subprocess.run(["tmux", "-L", socket, "select-window", "-t", wid],
                       check=True, timeout=5)
    except subprocess.CalledProcessError:
        return None
    try:
        subprocess.run(["tmux", "-L", socket, "refresh-client", "-S"],
                       capture_output=True, timeout=2)
    except Exception:
        pass
    return wid


def jump_to_attention(profile=None, dry_run=False):
    """Switch to whichever window most wants attention right now.

    Priority tiers (each tier exhausted before falling to the next):
      1. urgent  — claude's Notification hook fired (permission popup etc.),
                   sorted most-recent first.
      2. message — unread inter-window message, sorted most-recent first.
      3. idle    — claude finished responding and the user hasn't returned,
                   sorted OLDEST first (return to the most-neglected window).

    Skips entries that resolve to the current window, one-tab sockets, or
    stale window ids. Returns "✓ Nothing waiting" only when every tier is
    empty.
    """
    from clauthing.hooks import _load_attention, clear_attention, _load_idle

    attention = _load_attention(profile) or {}
    idle = _load_idle(profile) or {}

    # Tier-tagged candidate list: (tier_rank, sort_key, sid, socket_hint, source, info).
    # tier_rank ascending = higher priority.
    # sort_key: for urgent/message we negate ts so largest ts comes first
    # within the tier; for idle we use the raw ts so smallest (oldest) wins.
    candidates = []
    for sid, info in attention.items():
        candidates.append((0, -info.get("ts", 0), sid, info.get("socket"), "urgent", info))
    for sid, ts in _unread_message_signals(profile):
        hint = (attention.get(sid) or {}).get("socket") \
            or (idle.get(sid) or {}).get("socket")
        candidates.append((1, -ts, sid, hint, "message", None))
    for sid, info in idle.items():
        candidates.append((2, info.get("ts", 0), sid, info.get("socket"), "idle", info))

    if not candidates:
        return True, "✓ Nothing waiting"

    candidates.sort(key=lambda t: (t[0], t[1]))
    # Unpack back into the shape the loop below expects.
    candidates = [(sid, -sort_key if tier < 2 else sort_key, hint, src, info)
                  for tier, sort_key, sid, hint, src, info in candidates]

    fallback_socket = os.environ.get("CLAUTHING_TMUX_SOCKET", "clauthing")

    # Skip the *current* window — switching to where you already are is a
    # silent no-op. But remember whether we saw any current-window signals
    # so we can surface them via the message instead of silently doing
    # nothing.
    current_session = None
    try:
        r = subprocess.run(
            ["tmux", "-L", fallback_socket, "display-message", "-p", "#{@clauthing_window}"],
            capture_output=True, text=True, timeout=2,
        )
        current_session = r.stdout.strip() or None
    except Exception:
        pass

    here_sources = []
    for sid, ts, socket_hint, source, info in candidates:
        if sid == current_session:
            here_sources.append(source)
            continue
        socket = socket_hint or fallback_socket
        wid = _try_switch_to(sid, socket, dry_run=dry_run)
        if wid:
            title = (info or {}).get("title") if info else None
            label = title or sid[:8]
            prefix = "[dry-run] → " if dry_run else "→ "
            return True, f"{prefix}{label} ({wid}) [{source}]"
        if source == "urgent" and not socket_hint:
            # Stale urgent entry with no socket — drop it.
            clear_attention(sid, profile)
        if source == "idle":
            # Stale idle entry whose window has disappeared. Drop so we
            # don't keep trying it next time.
            from clauthing.hooks import clear_idle
            clear_idle(sid, profile)

    if here_sources:
        labels = {"urgent": "attention", "message": "messages", "idle": "idle"}
        bits = []
        for s in ("urgent", "message", "idle"):
            if s in here_sources:
                bits.append(labels[s])
        return True, f"📬 You're already on the window with {' + '.join(bits)}"

    return False, "❌ Nothing switchable (only one-tab/stale entries)"


def jump_to_waiting(profile=None):
    """Switch tmux to the most recent multi-tab window waiting for attention.

    Returns a (ok, message) tuple. Used by both the :waiting colon command
    and the M-, run-shell keybinding (clauthing --waiting), so the binding
    doesn't have to round-trip through claude's input.
    """
    from clauthing.hooks import _load_attention, clear_attention
    from datetime import datetime

    data = _load_attention(profile)
    if not data:
        return True, "✓ Nothing waiting"

    items = sorted(data.items(), key=lambda kv: kv[1].get('ts', 0), reverse=True)
    multi = [(sid, info) for sid, info in items
             if info.get('socket') and not info.get('socket', '').startswith('cl1-')]

    if not multi:
        lines = ["Waiting (one-tab — switch manually):"]
        for sid, info in items[:5]:
            ts = datetime.fromtimestamp(info.get('ts', 0)).strftime('%H:%M:%S')
            lines.append(f"  [{ts}] {info.get('title') or sid[:8]} — {info.get('path') or '?'}")
        return False, "\n".join(lines)

    target_sid, target_info = multi[0]
    target_socket = target_info.get('socket') or 'default'

    try:
        result = subprocess.run(
            ["tmux", "-L", target_socket, "list-windows", "-F", "#{window_id} #{@clauthing_window}"],
            capture_output=True, text=True, timeout=5
        )
    except Exception as e:
        return False, f"❌ tmux query failed: {e}"

    target_wid = None
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == target_sid:
            target_wid = parts[0]
            break

    if not target_wid:
        clear_attention(target_sid, profile)
        return False, f"❌ Window for {target_sid[:8]} not found — cleared"

    try:
        subprocess.run(["tmux", "-L", target_socket, "select-window", "-t", target_wid],
                       check=True, timeout=5)
    except subprocess.CalledProcessError:
        return False, f"❌ Could not switch to {target_wid}"

    try:
        subprocess.run(["tmux", "-L", target_socket, "refresh-client", "-S"],
                       capture_output=True, timeout=2)
    except Exception:
        pass

    title = target_info.get('title') or target_sid[:8]
    return True, f"→ Switched to {title} ({target_wid})"


@command(':waiting')
def cmd_waiting(ctx):
    """Switch to the most recent multi-tab window that's waiting for attention."""
    ok, msg = jump_to_waiting(ctx.profile)
    if ok and msg.startswith("→"):
        # Extract title for the tmux popup message (best-effort).
        ctx.message(msg.split(" (")[0])
    return ctx.stop(msg)


@command(':spawn')
def cmd_spawn(ctx):
    arg = ctx.args.strip()
    profile = ctx.profile
    socket = ctx.socket

    if not arg:
        # fzf picker of title history
        history_file = get_title_history_file()
        history = []
        if history_file.exists():
            try:
                history = json.loads(history_file.read_text())
            except:
                pass
        if not history:
            return ctx.stop("No title history yet. Use :spawn <title> to create one.")

        fzf_lines = [f"{e.get('title', '')}\t({e.get('count', 1)} uses)" for e in history]
        fzf_input = "\n".join(fzf_lines)
        fzf_cmd = f"echo '{fzf_input}' | fzf --prompt='Spawn with title: ' --with-nth=1"

        result = subprocess.run(
            ["tmux", "-L", socket, "display-popup", "-E", "-w", "60%", "-h", "50%", fzf_cmd],
            capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            arg = result.stdout.strip().split("\t")[0]
        else:
            return ctx.stop("Cancelled")

    record_title(arg)

    clauthing_path = shutil.which("clauthing") or "clauthing"
    cmd = [clauthing_path]
    if profile:
        cmd.extend(["--profile", profile])
    cmd.append("--one-tab")
    cmd.extend(["--window-name", arg])
    subprocess.Popen(cmd)

    ctx.message(f"✓ Spawning: {arg}")
    return ctx.stop(f"✓ Spawning new clauthing window: {arg}")


@command(':login-all')
def cmd_login_all(ctx):
    socket = ctx.socket
    try:
        uid = os.getuid()
        tmux_dir = Path(f"/tmp/tmux-{uid}")
        if not tmux_dir.exists():
            return ctx.stop("No tmux socket dir")

        cl1_sockets = [f.name for f in tmux_dir.iterdir() if f.name.startswith("cl1-")]
        if not cl1_sockets:
            return ctx.stop("No cl1-* instances")

        count = 0
        for cl_socket in cl1_sockets:
            if cl_socket == socket:
                continue
            result = subprocess.run(
                ["tmux", "-L", cl_socket, "list-windows", "-F", "#{window_index}"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                continue
            windows = result.stdout.strip().split('\n')
            for win_idx in windows:
                subprocess.run(["tmux", "-L", cl_socket, "send-keys", "-t", win_idx, "-l", ":login"])
                time.sleep(3.0)
                subprocess.run(["tmux", "-L", cl_socket, "send-keys", "-t", win_idx, "Enter"])
                count += 1
                time.sleep(0.2)

        ctx.message(f"✓ Sent :login to {count} instances")
        return ctx.stop(f"✓ Sent :login to {count} instances")
    except Exception as e:
        return ctx.stop(f"❌ Error: {str(e)}")


@command(':reload-all')
def cmd_reload_all(ctx):
    socket = ctx.socket
    try:
        uid = os.getuid()
        tmux_dir = Path(f"/tmp/tmux-{uid}")
        if not tmux_dir.exists():
            return ctx.stop("No tmux socket dir")

        cl1_sockets = [f.name for f in tmux_dir.iterdir() if f.name.startswith("cl1-")]
        if not cl1_sockets:
            return ctx.stop("No cl1-* instances")

        count = 0
        for cl_socket in cl1_sockets:
            if cl_socket == socket:
                continue
            result = subprocess.run(
                ["tmux", "-L", cl_socket, "list-windows", "-F", "#{window_index}"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                continue
            windows = result.stdout.strip().split('\n')
            for win_idx in windows:
                subprocess.run(["tmux", "-L", cl_socket, "send-keys", "-t", win_idx, "-l", ":reload"])
                time.sleep(0.5)
                subprocess.run(["tmux", "-L", cl_socket, "send-keys", "-t", win_idx, "Enter"])
                count += 1
                time.sleep(0.2)

        ctx.message(f"✓ Sent :reload to {count} instances")
        return ctx.stop(f"✓ Sent :reload to {count} instances")
    except Exception as e:
        return ctx.stop(f"❌ Error: {str(e)}")


@command(':send')
def cmd_send(ctx):
    message = ctx.args.strip()
    if not message:
        return ctx.stop("❌ Usage: :send <message>")

    socket = ctx.socket
    try:
        from clauthing.events import get_runtime_dir
        my_window = ctx.clauthing_window

        # Build the target list from live tmux windows (name + clauthing_window),
        # so picking a target and routing the message both key on the stable id.
        result = run(
            ["tmux", "-L", socket, "list-windows", "-F",
             "#{@clauthing_window}\t#{window_name}\t#{pane_current_path}"],
            capture_output=True, text=True, check=True,
        )
        fzf_lines = []
        for line in result.stdout.strip().splitlines():
            cw, name, path = (line.split("\t") + ["", "", ""])[:3]
            if not cw or cw == my_window:
                continue
            fzf_lines.append(f"{cw}\t{name}\t{path}")

        if not fzf_lines:
            return ctx.stop("No other windows to send to")

        uid = os.getuid()
        tmp_input = Path(f"/tmp/cl-send-{uid}.txt")
        tmp_output = Path(f"/tmp/cl-send-{uid}-out.txt")
        tmp_input.write_text("\n".join(fzf_lines))
        tmp_output.unlink(missing_ok=True)

        subprocess.run([
            "tmux", "-L", socket,
            "display-popup", "-E", "-w", "70%", "-h", "40%",
            f"cat {tmp_input} | fzf --delimiter='\\t' --with-nth=2,3 --header='Send to:' > {tmp_output}"
        ])

        selected = tmp_output.read_text().strip() if tmp_output.exists() else ""
        tmp_input.unlink(missing_ok=True)
        tmp_output.unlink(missing_ok=True)

        if not selected:
            return ctx.stop("Cancelled")

        parts = selected.split("\t")
        target_window = parts[0]
        target_title = parts[1] if len(parts) > 1 and parts[1] else target_window[:8]

        # Deliver to the recipient's inbox only — no pane injection (use :type).
        msgs_dir = get_runtime_dir() / "messages"
        msgs_dir.mkdir(exist_ok=True)
        inbox_file = msgs_dir / f"{target_window}.jsonl"
        msg_entry = {
            "from": _window_name_for(socket, my_window),
            "from_window": my_window or "",
            "message": message,
            "ts": time.time(),
            "read": False,
        }
        with open(inbox_file, "a") as f:
            f.write(json.dumps(msg_entry) + "\n")

        try:
            subprocess.run(["tmux", "-L", socket, "refresh-client", "-S"],
                           capture_output=True, timeout=2)
        except Exception:
            pass

        ctx.message(f"✓ Sent to {target_title}")
        return ctx.stop(f"✓ Message sent to {target_title}")
    except Exception as e:
        return ctx.stop(f"❌ Error: {str(e)}")


@command(':msg-read')
def cmd_msg_read(ctx):
    """Read the next unread inbox message (same as :msg with no args)."""
    if not ctx.clauthing_window:
        return ctx.stop("No window id")

    try:
        from clauthing.events import get_runtime_dir
        msgs_dir = get_runtime_dir() / "messages"
        inbox_file = msgs_dir / f"{ctx.clauthing_window}.jsonl"

        if not inbox_file.exists():
            return ctx.stop("📭 No messages in inbox")

        messages = []
        for line in inbox_file.read_text().strip().split("\n"):
            if line:
                try:
                    messages.append(json.loads(line))
                except:
                    pass

        if not messages:
            return ctx.stop("📭 No messages in inbox")

        # Show the oldest unread message, mark it read. Each :msg call
        # progresses to the next. Falls back to the most recent message if
        # everything is already read.
        unread = [m for m in messages if not m.get("read")]
        if unread:
            msg = min(unread, key=lambda m: (m.get("priority", 9999), m.get("ts", 0)))
            for m in messages:
                if m.get("ts") == msg.get("ts") and m.get("from_window") == msg.get("from_window"):
                    m["read"] = True
                    break
            with open(inbox_file, "w") as f:
                for m in messages:
                    f.write(json.dumps(m) + "\n")
            remaining = sum(1 for m in messages if not m.get("read"))
            time_str = time.strftime("%H:%M", time.localtime(msg.get("ts", 0)))
            tail = f" — {remaining} more, :msg for next" if remaining else ""
            return ctx.stop(f"📬 [{time_str}] {msg.get('from','unknown')}: {msg.get('message','')}{tail}")

        # Nothing unread — show the most recent read message as a fallback.
        msg = max(messages, key=lambda m: m.get("ts", 0))
        time_str = time.strftime("%H:%M", time.localtime(msg.get("ts", 0)))
        return ctx.stop(f"📭 (all read) [{time_str}] {msg.get('from','unknown')}: {msg.get('message','')}")
    except Exception as e:
        return ctx.stop(f"❌ Error: {str(e)}")


@command(':msgs', independent=True)
def cmd_msgs(ctx):
    """Show this window's whole inbox in a little curses UI you can reorder.

    :msg hands back the oldest-unread by (priority, ts); here you reorder the
    queue (J/K to move a message, s to save) and it saves priority = position.
    """
    if not ctx.clauthing_window:
        return ctx.stop("No window id")
    from clauthing.events import get_runtime_dir
    inbox = get_runtime_dir() / "messages" / f"{ctx.clauthing_window}.jsonl"
    if not inbox.exists():
        return ctx.stop("📭 No messages in inbox")
    if not ctx.socket:
        return ctx.stop("No socket")
    import sys as _sys
    try:
        subprocess.run([
            "tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "80%", "-h", "70%",
            _sys.executable, "-m", "clauthing.msg_arrange", str(inbox),
        ], timeout=300)
    except Exception as e:
        return ctx.stop(f"❌ Could not open arranger: {e}")
    return ctx.stop("")


def _resolve_session_by_window_name(socket, name):
    """Resolve the window named `name` on `socket`.

    Returns {session_id, window_id, clauthing_window, name} or None.
    """
    try:
        result = run(
            ["tmux", "-L", socket, "list-windows", "-F",
             "#{window_name}\t#{window_id}\t#{@session_id}\t#{@clauthing_window}"],
            capture_output=True, text=True, check=True,
        )
    except Exception:
        return None
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        win_name, win_id, sess_id = parts[0], parts[1], parts[2]
        cw = parts[3] if len(parts) > 3 else ""
        if win_name == name:
            return {"session_id": sess_id, "window_id": win_id,
                    "clauthing_window": cw, "name": win_name}
    return None


def _cmd_message_impl(ctx):
    """Shared impl for :message and :msg.

    No args  → read inbox (same as :msgs).
    args     → first whitespace-delimited token is target window name,
               remainder is the body.
    """
    args = ctx.args.strip()
    if not args:
        return cmd_msgs(ctx)

    parts = args.split(None, 1)
    target_name = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    if not body:
        return ctx.stop("❌ Usage: :message <window-name> <body>")

    socket = ctx.socket
    # "." targets the current window — send a note to yourself.
    if target_name == ".":
        target_window = ctx.clauthing_window
        if not target_window:
            return ctx.stop("❌ No window id for the current window")
        target_name = "."
    else:
        target = _resolve_session_by_window_name(socket, target_name)
        if not target or not target.get("clauthing_window"):
            return ctx.stop(f"❌ No window named '{target_name}' on socket {socket}")
        target_window = target["clauthing_window"]

    from clauthing.events import get_runtime_dir
    my_window = ctx.clauthing_window

    msgs_dir = get_runtime_dir() / "messages"
    msgs_dir.mkdir(exist_ok=True)
    # Inbox is keyed by the recipient's stable clauthing_window, so the message
    # survives the recipient running :cd (which rotates their session_id).
    inbox_file = msgs_dir / f"{target_window}.jsonl"
    msg_entry = {
        "from": _window_name_for(socket, my_window),
        "from_window": my_window or "",
        "message": body,
        "ts": time.time(),
        "read": False,
    }
    with open(inbox_file, "a") as f:
        f.write(json.dumps(msg_entry) + "\n")

    # Force immediate status-bar redraw so the (N) badge appears right away.
    # The message is delivered to the inbox only — the recipient pulls it with
    # :msg. Use :type to inject keystrokes into a window's pane.
    try:
        import subprocess as _sp
        _sp.run(["tmux", "-L", socket, "refresh-client", "-S"],
                capture_output=True, timeout=2)
    except Exception:
        pass

    where = "yourself" if target_name == "." else target_name
    return ctx.stop(f"✓ Message sent to {where}")


def _window_name_for(socket, clauthing_window):
    """Best-effort current window name for a clauthing_window (for `from`)."""
    if not clauthing_window:
        return "unknown"
    try:
        result = run(
            ["tmux", "-L", socket, "list-windows", "-F",
             "#{@clauthing_window}\t#{window_name}"],
            capture_output=True, text=True, check=True,
        )
        for line in result.stdout.strip().splitlines():
            cw, _, wname = line.partition("\t")
            if cw == clauthing_window:
                return wname or "unknown"
    except Exception:
        pass
    return "unknown"


@command(':message', independent=True)
def cmd_message(ctx):
    return _cmd_message_impl(ctx)


@command(':msg', independent=True)
def cmd_msg(ctx):
    return _cmd_message_impl(ctx)


def _session_transcript_file(ctx):
    """Path to the current window's claude transcript jsonl, or None."""
    if not ctx.session_id:
        return None
    from clauthing.claude_utils import encode_project_path
    if ctx.profile:
        projects_root = (Path.home() / ".config" / "clauthing" / "other-profiles"
                         / ctx.profile / "claude-data" / "projects")
    else:
        projects_root = Path.home() / ".config" / "clauthing" / "claude-data" / "projects"
    return projects_root / encode_project_path(ctx.cwd) / f"{ctx.session_id}.jsonl"


def _entry_user_text(entry):
    content = entry.get("message", {}).get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _entry_assistant_text(entry):
    content = entry.get("message", {}).get("content", [])
    if isinstance(content, str):
        return content.strip()
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def build_transcript_pairs(messages):
    """Collapse a list of user/assistant entries into (prompt, reply) pairs,
    oldest first. Skips the automatic Warmup prompt; joins multiple assistant
    text blocks under one prompt."""
    pairs = []
    cur_user, cur_reply = None, []
    for m in messages:
        if m.get("type") == "user":
            text = _entry_user_text(m)
            if not text or text == "Warmup":
                continue
            if cur_user is not None:
                pairs.append((cur_user, "\n".join(cur_reply).strip()))
            cur_user, cur_reply = text, []
        elif m.get("type") == "assistant":
            t = _entry_assistant_text(m)
            if t:
                cur_reply.append(t)
    if cur_user is not None:
        pairs.append((cur_user, "\n".join(cur_reply).strip()))
    return pairs


@command(':history')
def cmd_history(ctx):
    """Browse this window's conversation as prompt/reply pairs in a pager.

    Opens the custom pager at the most recent pair. j/k step between message
    pairs, Ctrl-D/Ctrl-U scroll within a long pair, ? searches backward
    (n repeats), / forward, q quits.
    """
    sf = _session_transcript_file(ctx)
    if not sf or not sf.exists():
        return ctx.stop("No transcript for this window yet")

    from clauthing.session_utils import get_session_messages
    pairs = build_transcript_pairs(get_session_messages(sf))
    if not pairs:
        return ctx.stop("No messages to show")

    import sys as _sys
    import shlex as _shlex
    uid = os.getuid()
    tmp = Path(f"/tmp/cl-history-{uid}.json")
    tmp.write_text(json.dumps(pairs))
    pager = (f"{_shlex.quote(_sys.executable)} -m clauthing.history_pager "
             f"{_shlex.quote(str(tmp))}")
    try:
        subprocess.run(["tmux", "-L", ctx.socket, "display-popup", "-E",
                        "-w", "90%", "-h", "85%", pager])
    finally:
        tmp.unlink(missing_ok=True)
    return ctx.stop("")


def launch_text_pager(socket, text, title="pager"):
    """Show `text` in `less`, full-screen in a tmux popup (overlays the window).

    less opens at the top (the beginning of the message): j/k scroll, / and ?
    search, n repeats, g/G top/bottom, q quits.
    """
    import shlex as _shlex
    uid = os.getuid()
    tmp = Path(f"/tmp/cl-pager-{uid}.txt")
    tmp.write_text(text)
    cmd = f"less -i {_shlex.quote(str(tmp))}"
    try:
        subprocess.run(["tmux", "-L", socket, "display-popup", "-E",
                        "-w", "90%", "-h", "90%", cmd])
    finally:
        tmp.unlink(missing_ok=True)


@command(':pager', independent=True)
def cmd_pager(ctx):
    """Show a claude reply full-screen (scrollable, searchable) so long output
    never gets cut off.

    :pager      show the last reply
    :pager N    show the Nth-from-last reply (:pager 2 = penultimate)
    """
    sf = _session_transcript_file(ctx)
    if not sf or not sf.exists():
        return ctx.stop("No transcript for this window yet")
    arg = ctx.args.strip()
    n = 1
    if arg:
        if not arg.isdigit() or int(arg) < 1:
            return ctx.stop("Usage: :pager [N]  (1=last, 2=penultimate, …)")
        n = int(arg)
    from clauthing.session_utils import get_assistant_messages
    msgs = get_assistant_messages(sf)
    if not msgs:
        return ctx.stop("No reply to show")
    if n > len(msgs):
        return ctx.stop(f"Only {len(msgs)} repl{'y' if len(msgs) == 1 else 'ies'} in this transcript")
    reply = msgs[-n]
    title = "last reply" if n == 1 else f"reply -{n}"
    launch_text_pager(ctx.socket, reply, title)
    return ctx.stop("")


# Primary input field to paste for well-known tools (else the full input JSON).
_TOOL_PRIMARY_FIELD = {
    "Bash": "command", "Read": "file_path", "Edit": "file_path",
    "Write": "file_path", "NotebookEdit": "notebook_path",
    "Grep": "pattern", "Glob": "pattern",
}


def _tool_paste_text(tu):
    """The text to paste for a tool use: the primary field for known tools,
    else the input as compact JSON."""
    inp = tu.get("input") or {}
    field = _TOOL_PRIMARY_FIELD.get(tu.get("name"))
    if field and isinstance(inp, dict) and inp.get(field):
        return str(inp[field])
    try:
        return json.dumps(inp, ensure_ascii=False)
    except Exception:
        return str(inp)


@command(':tools', independent=True)
def cmd_tools(ctx):
    """Browse this window's tool uses in an fzf picker (most recent first).

    Each is summarized by tool name + primary input; ↑/↓ browse with a live
    preview, Enter returns the selected tool's command/input as the stop
    reason (shown in the terminal).
    """
    sf = _session_transcript_file(ctx)
    if not sf or not sf.exists():
        return ctx.stop("No transcript for this window yet")
    from clauthing.session_utils import get_tool_uses
    tools = get_tool_uses(sf)
    if not tools:
        return ctx.stop("No tool uses to show")

    import tempfile
    uid = os.getuid()
    tmpdir = Path(tempfile.mkdtemp(prefix=f"cl-tools-{uid}-"))
    tmp_out = Path(f"/tmp/cl-tools-{uid}-out.txt")
    tmp_out.unlink(missing_ok=True)
    fzf_lines = []
    for i, tu in enumerate(tools):
        paste = _tool_paste_text(tu)
        (tmpdir / f"{i}.txt").write_text(f"{tu['name']}\n\n{paste}")
        summary = " ".join(paste.split())[:80] or "(no input)"
        fzf_lines.append(f"{i}\t{tu['name']}: {summary}")
    fzf_lines.reverse()  # most recent tool use first
    (tmpdir / "list.txt").write_text("\n".join(fzf_lines))
    try:
        subprocess.run([
            "tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "85%", "-h", "85%",
            f"cat {tmpdir}/list.txt | fzf --delimiter='\\t' --with-nth=2 "
            f"--preview='cat {tmpdir}/{{1}}.txt' --preview-window=right:60%:wrap "
            f"--header='tool uses — Enter pastes into the prompt' > {tmp_out}"
        ])
        sel = tmp_out.read_text().strip() if tmp_out.exists() else ""
    finally:
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)
        tmp_out.unlink(missing_ok=True)
    if not sel:
        return ctx.stop("Cancelled")
    try:
        idx = int(sel.split("\t")[0])
        selected = _tool_paste_text(tools[idx])
    except (ValueError, IndexError):
        return ctx.stop("❌ Could not select that tool use")
    return ctx.stop(selected)


@command(':replies', independent=True)
def cmd_replies(ctx):
    """Browse this window's replies in an fzf picker (most recent first).

    Each reply is summarized by its first words; ↑/↓ browse with a live
    preview of the full reply, Enter opens the selected reply in the pager.
    """
    sf = _session_transcript_file(ctx)
    if not sf or not sf.exists():
        return ctx.stop("No transcript for this window yet")
    from clauthing.session_utils import get_assistant_messages
    msgs = get_assistant_messages(sf)
    if not msgs:
        return ctx.stop("No replies to show")
    if not ctx.socket:
        return ctx.stop("\n".join(f"-{len(msgs) - i}  {' '.join(m.split())[:80]}"
                                  for i, m in enumerate(msgs)))

    import tempfile
    uid = os.getuid()
    tmpdir = Path(tempfile.mkdtemp(prefix=f"cl-replies-{uid}-"))
    tmp_out = Path(f"/tmp/cl-replies-{uid}-out.txt")
    tmp_out.unlink(missing_ok=True)
    n = len(msgs)
    fzf_lines = []
    for i, reply in enumerate(msgs):
        (tmpdir / f"{i}.txt").write_text(reply)
        summary = " ".join(reply.split())[:80] or "(no text)"
        fzf_lines.append(f"{i}\t-{n - i}  {summary}")
    fzf_lines.reverse()  # most recent reply first
    (tmpdir / "list.txt").write_text("\n".join(fzf_lines))
    try:
        subprocess.run([
            "tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "85%", "-h", "85%",
            f"cat {tmpdir}/list.txt | fzf --delimiter='\\t' --with-nth=2 "
            f"--preview='cat {tmpdir}/{{1}}.txt' --preview-window=right:60%:wrap "
            f"--header='replies — ↑/↓ to browse, Enter to open in pager' > {tmp_out}"
        ])
        sel = tmp_out.read_text().strip() if tmp_out.exists() else ""
    finally:
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)
        tmp_out.unlink(missing_ok=True)
    if not sel:
        return ctx.stop("Cancelled")
    try:
        idx = int(sel.split("\t")[0])
        reply = msgs[idx]
    except (ValueError, IndexError):
        return ctx.stop("❌ Could not open that reply")
    rev = n - idx
    launch_text_pager(ctx.socket, reply, "last reply" if rev == 1 else f"reply -{rev}")
    return ctx.stop("")


def _last_inbox_message(clauthing_window):
    """Return the most recent message in a window's inbox, or None.

    Non-destructive — does not touch read state.
    """
    if not clauthing_window:
        return None
    from clauthing.events import get_runtime_dir
    inbox = get_runtime_dir() / "messages" / f"{clauthing_window}.jsonl"
    if not inbox.exists():
        return None
    msgs = []
    for line in inbox.read_text().splitlines():
        if line:
            try:
                msgs.append(json.loads(line))
            except Exception:
                pass
    if not msgs:
        return None
    return max(msgs, key=lambda m: m.get("ts", 0))


def push_back_last_read(messages):
    """Mark the most-recently-read message unread (push it back onto the stack).

    "Most recently read" = the read message with the latest ts (reading
    progresses oldest→newest, so that's the one :msg last showed). Mutates the
    matching dict in `messages`; returns it, or None if nothing was read.
    """
    read_msgs = [m for m in messages if m.get("read")]
    if not read_msgs:
        return None
    target = max(read_msgs, key=lambda m: m.get("ts", 0))
    target["read"] = False
    return target


@command(':push', independent=True)
def cmd_push(ctx):
    """Push the last-read message back onto this window's stack (undo :msg)."""
    cw = ctx.clauthing_window
    if not cw:
        return ctx.stop("No window id")
    from clauthing.events import get_runtime_dir
    inbox = get_runtime_dir() / "messages" / f"{cw}.jsonl"
    if not inbox.exists():
        return ctx.stop("📭 No messages")
    msgs = [json.loads(l) for l in inbox.read_text().splitlines() if l]
    pushed = push_back_last_read(msgs)
    if pushed is None:
        return ctx.stop("Nothing to push back (no read messages)")
    inbox.write_text("\n".join(json.dumps(m) for m in msgs) + "\n")
    body = (pushed.get("message", "") or "")[:50]
    return ctx.stop(f"↩ Pushed back: {pushed.get('from', '?')}: {body}")


@command(':peek', independent=True)
def cmd_peek(ctx):
    """Show this window's most recent message without marking it read."""
    cw = ctx.clauthing_window
    if not cw:
        return ctx.stop("No window id")
    msg = _last_inbox_message(cw)
    if not msg:
        return ctx.stop("📭 No messages")
    t = time.strftime("%H:%M", time.localtime(msg.get("ts", 0)))
    return ctx.stop(f"👁 [{t}] {msg.get('from', 'unknown')}: {msg.get('message', '')}")


@command(':save', independent=True)
def cmd_save(ctx):
    """Bookmark this window's most recent message into its per-window state."""
    cw = ctx.clauthing_window
    if not cw:
        return ctx.stop("No window id")
    msg = _last_inbox_message(cw)
    if not msg:
        return ctx.stop("📭 No message to save")
    from clauthing.session import load_window_state, save_window_state
    state = load_window_state(cw, ctx.profile)
    bookmarks = state.setdefault("bookmarks", [])
    bookmarks.append(msg)
    save_window_state(cw, state, ctx.profile)
    return ctx.stop(f"🔖 Saved ({len(bookmarks)} bookmark(s))")


@command(':log', independent=True)
def cmd_log(ctx):
    """Drop a debug marker into the clauthing log with a snapshot of window
    state — your note plus the current window, attention, and idle maps. Use it
    the moment something looks wrong (e.g. M-, picking the wrong window) so we
    can correlate. Find them with:  grep 'LOG-MARKER' <instance>/combined.log
    """
    from clauthing.hooks import _load_attention, _load_idle
    note = ctx.args.strip() or "(no note)"
    socket = ctx.socket
    profile = ctx.profile

    def _tmux(fmt):
        try:
            return run(["tmux", "-L", socket, "display-message", "-p", fmt],
                       capture_output=True, text=True).stdout.strip()
        except Exception:
            return "?"

    cur = _tmux("#{window_index}\t#{window_name}\t#{@session_id}\t#{@clauthing_window}")
    try:
        wins = run(["tmux", "-L", socket, "list-windows", "-F",
                    "#{window_index}:#{window_name} sid=#{@session_id} cw=#{@clauthing_window}"],
                   capture_output=True, text=True).stdout.strip()
    except Exception:
        wins = "?"
    att = _load_attention(profile) or {}
    idle = _load_idle(profile) or {}

    log(f"LOG-MARKER: {note}", profile)
    log(f"LOG-MARKER:   current(index\\tname\\tsid\\tcw)= {cur}", profile)
    log(f"LOG-MARKER:   ctx session={ctx.session_id} clauthing_window={ctx.clauthing_window}", profile)
    log(f"LOG-MARKER:   attention({len(att)})= {json.dumps(att, sort_keys=True)}", profile)
    log(f"LOG-MARKER:   idle({len(idle)})= {json.dumps(idle, sort_keys=True)}", profile)
    log("LOG-MARKER:   windows=\n    " + wins.replace("\n", "\n    "), profile)
    return ctx.stop(f"📝 Logged marker: {note}")


@command(':backfill-window-ids', independent=True)
def cmd_backfill_window_ids(ctx):
    """Set @clauthing_window on any live window that's missing one.

    Needed once after an in-place upgrade for windows that predate
    clauthing_window. (A full restart heals automatically.)
    """
    from clauthing.tmux import backfill_clauthing_windows
    n = backfill_clauthing_windows(ctx.socket)
    return ctx.stop(f"✓ Backfilled {n} window id(s)")


@command(':type', independent=True)
def cmd_type(ctx):
    """Type text directly into another window's pane.

    Usage: :type <window-name> <text>

    This is the keystroke-injection :msg used to do. :msg now only records a
    pullable inbox message; :type is the explicit "send keys to that window".
    """
    args = ctx.args.strip()
    parts = args.split(None, 1)
    target_name = parts[0] if parts else ""
    text = parts[1] if len(parts) > 1 else ""
    if not target_name or not text:
        return ctx.stop("❌ Usage: :type <window-name> <text>")

    socket = ctx.socket
    target = _resolve_session_by_window_name(socket, target_name)
    if not target or not target.get("window_id"):
        return ctx.stop(f"❌ No window named '{target_name}' on socket {socket}")

    try:
        run(["tmux", "-L", socket, "send-keys", "-t", target["window_id"], "-l", text])
        run(["tmux", "-L", socket, "send-keys", "-t", target["window_id"], "Enter"])
    except Exception as e:
        return ctx.stop(f"❌ Error: {e}")

    return ctx.stop(f"✓ Typed into {target_name}")