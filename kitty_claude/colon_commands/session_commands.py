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

from kitty_claude.colon_command import command, send_tmux_message, get_state_dir
from kitty_claude.colon_command import get_title_history_file, record_title
from kitty_claude.logging import log, run
from kitty_claude.session import get_session_name


@command(':current-sessions')
def cmd_current_sessions(ctx):
    from kitty_claude.claude import get_running_sessions
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
    from kitty_claude.claude import get_recent_sessions
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
    arg = ctx.args.strip()
    if not arg:
        return ctx.stop("Usage: :resume <number|session-id>")

    if arg.isdigit():
        from kitty_claude.claude import get_recent_sessions
        sessions = get_recent_sessions(ctx.profile, limit=10)
        index = int(arg) - 1
        if 0 <= index < len(sessions):
            target_session_id = sessions[index]['session_id']
        else:
            return ctx.stop(f"❌ Session number {arg} not found")
    else:
        target_session_id = arg

    from kitty_claude.claude import new_window
    new_window(profile=ctx.profile, resume_session_id=target_session_id, socket=ctx.socket)
    ctx.message("✓ Resuming session")
    return ctx.stop(f"✓ Opening session {target_session_id[:8]}... in new window")


@command(':resume-new')
def cmd_resume_new(ctx):
    arg = ctx.args.strip()
    socket = ctx.socket
    profile = ctx.profile

    if not arg:
        from kitty_claude.claude import get_recent_sessions
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
        from kitty_claude.claude import get_recent_sessions
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
            base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
        else:
            base_config = Path.home() / ".config" / "kitty-claude"
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
        from kitty_claude.session import get_state_dir as sess_get_state_dir
        state_dir = sess_get_state_dir()
        metadata_file = state_dir / "sessions" / f"{target_session_id}.json"
        if metadata_file.exists():
            metadata = json.loads(metadata_file.read_text())
            name = metadata.get("name")
            if name and name != target_session_id and not name.startswith("kitty-claude-"):
                if not (len(name) == 36 and name.count('-') == 4):
                    session_title = name
    except:
        pass

    # Build command
    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
    cmd = [kitty_claude_path]
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
    return ctx.stop(f"✓ Resuming {target_session_id[:8]}...{cwd_msg} in new kitty-claude window")


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

    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
    cmd = [kitty_claude_path]
    if profile:
        cmd.extend(["--profile", profile])
    cmd.append("--one-tab")
    cmd.extend(["--window-name", arg])
    subprocess.Popen(cmd)

    ctx.message(f"✓ Spawning: {arg}")
    return ctx.stop(f"✓ Spawning new kitty-claude window: {arg}")


@command(':login-all')
def cmd_login_all(ctx):
    socket = ctx.socket
    try:
        uid = os.getuid()
        tmux_dir = Path(f"/tmp/tmux-{uid}")
        if not tmux_dir.exists():
            return ctx.stop("No tmux socket dir")

        kc1_sockets = [f.name for f in tmux_dir.iterdir() if f.name.startswith("kc1-")]
        if not kc1_sockets:
            return ctx.stop("No kc1-* instances")

        count = 0
        for kc_socket in kc1_sockets:
            if kc_socket == socket:
                continue
            result = subprocess.run(
                ["tmux", "-L", kc_socket, "list-windows", "-F", "#{window_index}"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                continue
            windows = result.stdout.strip().split('\n')
            for win_idx in windows:
                subprocess.run(["tmux", "-L", kc_socket, "send-keys", "-t", win_idx, "-l", ":login"])
                time.sleep(3.0)
                subprocess.run(["tmux", "-L", kc_socket, "send-keys", "-t", win_idx, "Enter"])
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

        kc1_sockets = [f.name for f in tmux_dir.iterdir() if f.name.startswith("kc1-")]
        if not kc1_sockets:
            return ctx.stop("No kc1-* instances")

        count = 0
        for kc_socket in kc1_sockets:
            if kc_socket == socket:
                continue
            result = subprocess.run(
                ["tmux", "-L", kc_socket, "list-windows", "-F", "#{window_index}"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                continue
            windows = result.stdout.strip().split('\n')
            for win_idx in windows:
                subprocess.run(["tmux", "-L", kc_socket, "send-keys", "-t", win_idx, "-l", ":reload"])
                time.sleep(0.5)
                subprocess.run(["tmux", "-L", kc_socket, "send-keys", "-t", win_idx, "Enter"])
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
        from kitty_claude.events import get_all_windows, get_runtime_dir
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
        tmp_input = Path(f"/tmp/kc-send-{uid}.txt")
        tmp_output = Path(f"/tmp/kc-send-{uid}-out.txt")
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

        if target_socket.startswith("kc1-"):
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
        from kitty_claude.events import get_runtime_dir
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
        tmp_msgs = Path(f"/tmp/kc-msgs-{uid}.txt")
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