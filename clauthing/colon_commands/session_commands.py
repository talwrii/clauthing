"""Session listing, resume, spawn, and messaging colon commands.

Commands: :sessions, :resume, :resume-new, :spawn, :current-sessions,
          :login-all, :reload-all, :send, :msgs
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


@command(':current-sessions')
def cmd_current_sessions(ctx):
    from clauthing.claude import get_running_sessions
    sessions = get_running_sessions(ctx.profile)
    if not sessions:
        return ctx.stop("No currently running sessions")

    lines = ["Currently running sessions:\n"]
    for i, sess in enumerate(sessions, 1):
        cwd = sess.get('cwd', '?')
        session_id = sess['session_id']
        pid = sess['pid']
        lines.append(f"{i}. {session_id[:8]}... (PID {pid}) - {cwd}")

    ctx.message(f"✓ {len(sessions)} running")
    return ctx.stop("\n".join(lines))


@command(':sessions')
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
        sessions = get_recent_sessions(ctx.profile, limit=20)
        if not sessions:
            return ctx.stop("No recent sessions found")

        fzf_lines = []
        for sess in sessions:
            sid = sess['session_id']
            title = sess.get('title') or sid[:8]
            cwd = sess.get('cwd') or '?'
            mtime = datetime.fromtimestamp(sess['last_modified']).strftime('%Y-%m-%d %H:%M')
            last_msg = (sess.get('last_message') or '').replace('\n', ' ').strip()
            if len(last_msg) > 60:
                last_msg = last_msg[:60] + '...'
            fzf_lines.append(f"{sid}\t{title}\t{cwd}\t{mtime}\t{last_msg}")

        tmp_in = Path(tempfile.mktemp())
        tmp_out = Path(tempfile.mktemp())
        tmp_in.write_text("\n".join(fzf_lines))
        subprocess.run([
            "tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "80%", "-h", "60%",
            f"cat {tmp_in} | fzf --delimiter='\\t' --with-nth=2,3,4,5 "
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
        # Multi-tab mode: open a new tmux window running clauthing --new-window
        # --resume-session. The new window's clauthing process sets @session_id
        # itself; we don't call new_window() here (which would clobber the
        # current window and trip the window-1 restore logic).
        clauthing_path = shutil.which("clauthing") or "clauthing"
        cmd_parts = [clauthing_path]
        if ctx.profile:
            cmd_parts.extend(["--profile", ctx.profile])
        cmd_parts.extend(["--new-window", "--resume-session", target_session_id])
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


@command(':waiting')
def cmd_waiting(ctx):
    """Switch to the most recent multi-tab window that's waiting for attention."""
    from clauthing.hooks import _load_attention, clear_attention
    from datetime import datetime

    data = _load_attention(ctx.profile)
    if not data:
        return ctx.stop("✓ Nothing waiting")

    # Sort by ts desc, prefer multi-tab (default-socket) entries
    items = sorted(data.items(), key=lambda kv: kv[1].get('ts', 0), reverse=True)
    multi = [(sid, info) for sid, info in items
             if info.get('socket') and not info.get('socket', '').startswith('cl1-')]

    if not multi:
        # Show what's waiting even if we can't switch
        lines = ["Waiting (one-tab — switch manually):"]
        for sid, info in items[:5]:
            ts = datetime.fromtimestamp(info.get('ts', 0)).strftime('%H:%M:%S')
            lines.append(f"  [{ts}] {info.get('title') or sid[:8]} — {info.get('path') or '?'}")
        return ctx.stop("\n".join(lines))

    target_sid, target_info = multi[0]
    target_socket = target_info.get('socket') or 'default'

    # Find window_id by querying tmux user-option @session_id on each window
    try:
        result = subprocess.run(
            ["tmux", "-L", target_socket, "list-windows", "-F", "#{window_id} #{@session_id}"],
            capture_output=True, text=True, timeout=5
        )
    except Exception as e:
        return ctx.stop(f"❌ tmux query failed: {e}")

    target_wid = None
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == target_sid:
            target_wid = parts[0]
            break

    if not target_wid:
        clear_attention(target_sid, ctx.profile)
        return ctx.stop(f"❌ Window for {target_sid[:8]} not found — cleared")

    try:
        subprocess.run(["tmux", "-L", target_socket, "select-window", "-t", target_wid],
                       check=True, timeout=5)
    except subprocess.CalledProcessError:
        return ctx.stop(f"❌ Could not switch to {target_wid}")

    title = target_info.get('title') or target_sid[:8]
    ctx.message(f"→ {title}")
    return ctx.stop(f"→ Switched to {title} ({target_wid})")


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
        from clauthing.events import get_all_windows, get_runtime_dir
        windows = get_all_windows()
        my_session_id = ctx.session_id

        fzf_lines = []
        for session_id, info in windows.items():
            if session_id == my_session_id:
                continue
            title = info.get("title", session_id[:8])
            win_socket = info.get("socket", "")
            path = info.get("path", "")
            fzf_lines.append(f"{session_id}\t{title}\t{win_socket}\t{path}")

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
            f"cat {tmp_input} | fzf --delimiter='\\t' --with-nth=2,4 --header='Send to:' > {tmp_output}"
        ])

        selected = tmp_output.read_text().strip() if tmp_output.exists() else ""
        tmp_input.unlink(missing_ok=True)
        tmp_output.unlink(missing_ok=True)

        if not selected:
            return ctx.stop("Cancelled")

        parts = selected.split("\t")
        target_session_id = parts[0]
        target_title = parts[1] if len(parts) > 1 else target_session_id[:8]
        target_socket = parts[2] if len(parts) > 2 else socket

        if target_socket.startswith("cl1-"):
            target_pane = "%0"
        else:
            result = run(
                ["tmux", "-L", target_socket, "list-windows", "-F", "#{window_id} #{@session_id}"],
                capture_output=True, text=True, check=True
            )
            target_pane = None
            for line in result.stdout.strip().split("\n"):
                line_parts = line.split()
                if len(line_parts) >= 2 and line_parts[1] == target_session_id:
                    target_pane = line_parts[0]
                    break

        if target_pane:
            run(["tmux", "-L", target_socket, "send-keys", "-t", target_pane, "-l", message])
            run(["tmux", "-L", target_socket, "send-keys", "-t", target_pane, "Enter"])

            # Store in inbox
            msgs_dir = get_runtime_dir() / "messages"
            msgs_dir.mkdir(exist_ok=True)
            inbox_file = msgs_dir / f"{target_session_id}.jsonl"
            my_windows = get_all_windows()
            my_info = my_windows.get(my_session_id, {})
            msg_entry = {
                "from": my_info.get("title", "unknown"),
                "from_session": my_session_id,
                "message": message,
                "ts": time.time(),
                "read": False
            }
            with open(inbox_file, "a") as f:
                f.write(json.dumps(msg_entry) + "\n")

            ctx.message(f"✓ Sent to {target_title}")
            return ctx.stop(f"✓ Message sent to {target_title}")
        else:
            return ctx.stop("❌ Could not find target window")
    except Exception as e:
        return ctx.stop(f"❌ Error: {str(e)}")


@command(':msgs')
def cmd_msgs(ctx):
    if not ctx.session_id:
        return ctx.stop("No session ID")

    try:
        from clauthing.events import get_runtime_dir
        msgs_dir = get_runtime_dir() / "messages"
        inbox_file = msgs_dir / f"{ctx.session_id}.jsonl"

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

        lines = []
        for msg in messages:
            ts = msg.get("ts", 0)
            time_str = time.strftime("%H:%M", time.localtime(ts))
            from_title = msg.get("from", "unknown")
            text = msg.get("message", "")
            read_mark = "" if msg.get("read") else "●"
            lines.append(f"{read_mark} [{time_str}] {from_title}: {text}")

        # Mark all as read
        with open(inbox_file, "w") as f:
            for msg in messages:
                msg["read"] = True
                f.write(json.dumps(msg) + "\n")

        # Show in popup
        uid = os.getuid()
        tmp_msgs = Path(f"/tmp/cl-msgs-{uid}.txt")
        tmp_msgs.write_text("\n".join(lines))
        subprocess.run([
            "tmux", "-L", ctx.socket,
            "display-popup", "-E", "-w", "80%", "-h", "60%",
            f"cat {tmp_msgs}; read -n1"
        ])
        tmp_msgs.unlink(missing_ok=True)
        return ctx.stop(f"📬 {len(messages)} message(s)")
    except Exception as e:
        return ctx.stop(f"❌ Error: {str(e)}")