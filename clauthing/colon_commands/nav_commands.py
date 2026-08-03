"""Navigation and session lifecycle colon commands.

Commands: :cd, :cdpop, :cd-tmux, :reload, :clear, :fork, :call, :ask,
          :checkpoint, :rollback, :login, :god/:plan
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path

from clauthing.colon_command import command, send_tmux_message
from clauthing.claude_utils import encode_project_path
from clauthing.logging import log, run
from clauthing.session import get_session_name, save_session_metadata, mark_session_has_messages
from clauthing.session_utils import session_has_messages
from clauthing.tmux import get_window_and_pane_for_session
from clauthing.rules import build_claude_md


def get_state_dir():
    xdg_state = os.environ.get('XDG_STATE_HOME')
    if xdg_state:
        state_dir = Path(xdg_state) / "clauthing"
    else:
        state_dir = Path.home() / ".local" / "state" / "clauthing"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


# ── Shared Helpers ───────────────────────────────────────────────────────────

def one_tab_relaunch(socket, launcher, current_window_id=None):
    """Relaunch in one-tab mode: open new window, then close old.

    Unlike respawn-pane -k, this is safe because the old window
    stays alive until the new one is confirmed running.
    """
    if not current_window_id:
        try:
            result = subprocess.run(
                ["tmux", "-L", socket, "display-message", "-p", "#{window_id}"],
                capture_output=True, text=True
            )
            current_window_id = result.stdout.strip()
        except:
            pass

    kill_cmd = ""
    if current_window_id:
        kill_cmd = (
            f" && sleep 2"
            f" && [ $(tmux -L {socket} list-windows 2>/dev/null | wc -l) -gt 1 ]"
            f" && tmux -L {socket} kill-window -t {current_window_id} 2>/dev/null"
            f" || true"
        )

    subprocess.Popen([
        "sh", "-c",
        f"sleep 1 && tmux -L {socket} new-window {launcher}{kill_cmd}"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def get_current_window_id(socket):
    try:
        result = run(
            ["tmux", "-L", socket, "display-message", "-p", "#{window_id}"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except:
        return None


def get_claude_binary(profile=None):
    if profile:
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"
    config_file = config_dir / "config.json"
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
            if config.get("claude_binary"):
                return config["claude_binary"]
        except:
            pass
    return shutil.which("claude") or "claude"


def make_one_tab_launcher(target_dir, session_id, claude_config, claude_bin):
    """Create a launcher script for one-tab mode. Returns the path."""
    uid = os.getuid()
    launcher = Path(f"/tmp/cl-launch-{uid}-{session_id[:8]}.sh")
    launcher.write_text(f'''#!/bin/sh
export CLAUDE_CONFIG_DIR="{claude_config}"
cd "{target_dir}"
exec "{claude_bin}" --resume {session_id}
''')
    launcher.chmod(0o755)
    return launcher


def push_dir_stack(session_id, directory):
    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"
    metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    stack = metadata.get("dir_stack", [])
    stack.append(directory)
    metadata["dir_stack"] = stack
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    metadata_file.write_text(json.dumps(metadata, indent=2))


def pop_dir_stack(session_id):
    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"
    if not metadata_file.exists():
        return None
    metadata = json.loads(metadata_file.read_text())
    stack = metadata.get("dir_stack", [])
    if not stack:
        return None
    directory = stack.pop()
    metadata["dir_stack"] = stack
    metadata_file.write_text(json.dumps(metadata, indent=2))
    return directory


def add_checkpoint_to_session(session_file):
    import time
    checkpoint_entry = {
        "type": "checkpoint",
        "timestamp": time.time(),
        "iso_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")
    }
    with open(session_file, 'a') as f:
        f.write(json.dumps(checkpoint_entry) + '\n')


def rollback_session_to_checkpoint(session_file, target_session_file):
    lines = []
    last_checkpoint_index = -1
    with open(session_file, 'r') as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get('type') == 'checkpoint':
                    last_checkpoint_index = i
                lines.append(line)
            except json.JSONDecodeError:
                lines.append(line)
    if last_checkpoint_index == -1:
        return False
    with open(target_session_file, 'w') as f:
        for i, line in enumerate(lines):
            if i <= last_checkpoint_index:
                f.write(line + '\n')
    return True


def carry_over_session_state(old_session_id, new_session_id):
    state_dir = get_state_dir()
    old_meta_file = state_dir / "sessions" / f"{old_session_id}.json"
    new_meta_file = state_dir / "sessions" / f"{new_session_id}.json"
    if not new_meta_file.exists():
        return
    try:
        new_meta = json.loads(new_meta_file.read_text())
        # Provenance: record which session this one was cloned from (by :cd),
        # so things keyed by session_id (e.g. message inboxes) can be traced
        # back across the clone.
        new_meta["cloned_from"] = old_session_id
        if old_meta_file.exists():
            old_meta = json.loads(old_meta_file.read_text())
            # clauthing_window is the stable window identity — it must follow the
            # window across the :cd clone (the new session keeps the same window).
            # (linked-tmux state lives in the window-state file keyed by
            # clauthing_window, so it survives :cd without being carried here.)
            for key in ("dir_stack", "mcpServers", "clauthing_window"):
                if key in old_meta:
                    new_meta[key] = old_meta[key]
        new_meta_file.write_text(json.dumps(new_meta, indent=2))
    except:
        pass


def open_new_multi_tab_window(socket, profile, target_dir, session_id, current_window_id=None, window_name=None):
    """Open new window in multi-tab mode, optionally close old one."""
    clauthing_path = shutil.which("clauthing") or "clauthing"
    cmd_parts = [clauthing_path]
    if profile:
        cmd_parts.extend(["--profile", profile])
    cmd_parts.extend(["--new-claude", "--resume-session", session_id])
    cmd_str = " ".join(cmd_parts)

    new_window_cmd = ["tmux", "-L", socket, "new-window"]
    if window_name:
        new_window_cmd.extend(["-n", window_name])
    new_window_cmd.extend(["-c", target_dir, cmd_str])
    run(new_window_cmd)

    if current_window_id:
        close_script = f"""
sleep 2
if tmux -L {socket} list-windows -F '#{{@session_id}}' 2>/dev/null | grep -q '^{session_id}$'; then
    tmux -L {socket} kill-window -t {current_window_id} 2>/dev/null || true
fi
"""
        subprocess.Popen(["sh", "-c", close_script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── Clone + Change Directory (shared by :cd, :cdpop, :cd-tmux) ──────────────

def clone_session_and_change_directory(target_dir, current_dir, ctx):
    socket = ctx.socket
    claude_data_dir = ctx.claude_data_dir

    from clauthing.tmux import log_window_snapshot
    log(f":cd START target={target_dir!r} current={current_dir!r} "
        f"session={ctx.session_id} cwd={ctx.cwd!r} "
        f"TMUX_PANE={os.environ.get('TMUX_PANE')!r} "
        f"CLAUDE_CONFIG_DIR={os.environ.get('CLAUDE_CONFIG_DIR')!r}")
    log_window_snapshot(socket, "before :cd")

    # The window's CURRENT name — preserve it across :cd regardless of which
    # session ends up cloned (a fallback clone must not rename the window after
    # some unrelated session).
    cur_window_name = None
    _pane = os.environ.get("TMUX_PANE")
    if _pane:
        try:
            _r = subprocess.run(
                ["tmux", "-L", socket, "display-message", "-p", "-t", _pane, "#{window_name}"],
                capture_output=True, text=True, timeout=5)
            cur_window_name = _r.stdout.strip() or None
        except Exception:
            cur_window_name = None

    encoded_current = encode_project_path(current_dir)
    encoded_target = encode_project_path(target_dir)

    # Pick the session to clone. Prefer THIS window's own session
    # (ctx.session_id); only fall back to the most-recent transcript in the dir
    # if ours has none. Otherwise :cd can clone a *different* window's session
    # that happens to be newer in the same directory — cloning the wrong history
    # and renaming this window after that session.
    new_session_id = None
    projects_dir = claude_data_dir / "projects" / encoded_current
    source_sf = None
    if ctx.session_id:
        own = projects_dir / f"{ctx.session_id}.jsonl"
        if own.exists() and session_has_messages(own):
            source_sf = own
    if source_sf is None and projects_dir.exists():
        for sf in sorted(projects_dir.glob("*.jsonl"),
                         key=lambda p: p.stat().st_mtime, reverse=True):
            if session_has_messages(sf):
                source_sf = sf
                break
    if source_sf is not None:
        old_session_id = source_sf.stem
        new_session_id = str(uuid.uuid4())
        keep_name = cur_window_name or get_session_name(old_session_id)
        log(f":cd cloning session {old_session_id} (own={old_session_id == ctx.session_id}) "
            f"keep_name={keep_name!r}")
        target_projects_dir = claude_data_dir / "projects" / encoded_target
        target_projects_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_sf, target_projects_dir / f"{new_session_id}.jsonl")
        save_session_metadata(new_session_id, keep_name, target_dir)
        mark_session_has_messages(new_session_id)
        carry_over_session_state(old_session_id, new_session_id)

    # Look up our own window/pane by session_id so this works even when the
    # focused tmux window has already been restored to the user's window after
    # an MCP popup confirmation.
    window_id = None
    # The pane this command actually runs in — tmux sets $TMUX_PANE on the
    # claude/hook process. It's unambiguous even when two windows share a
    # session_id, or when focus has moved (e.g. after an MCP popup), so prefer
    # it over resolving by session_id (which returns the *first* match and can
    # land on the wrong window).
    pane_id = os.environ.get("TMUX_PANE") or None
    if pane_id:
        try:
            r = subprocess.run(
                ["tmux", "-L", socket, "display-message", "-p", "-t", pane_id, "#{window_id}"],
                capture_output=True, text=True, timeout=5)
            window_id = r.stdout.strip() or None
        except Exception:
            window_id = None
    if not pane_id and ctx.session_id:
        window_id, pane_id = get_window_and_pane_for_session(socket, ctx.session_id)
    if not window_id:
        window_id = get_current_window_id(socket)
    current_window_id = window_id
    log(f":cd resolved target window={window_id} pane={pane_id} "
        f"(TMUX_PANE={os.environ.get('TMUX_PANE')!r}, session={ctx.session_id})")

    # Boomerang: set @startup_command, then respawn-pane to kill the running
    # claude. ctx.stop() alone returns to the prompt — it doesn't exit claude
    # — so the launcher loop would never pick @startup_command up. respawn-pane
    # restarts the pane's default-command (boomerang.sh / `clauthing --new-claude`)
    # which then reads @startup_command at startup.
    if new_session_id:
        startup_cmd = f'SESSION_ID="{new_session_id}"; cd "{target_dir}"'
    else:
        # Fresh start in target dir — no session to resume.
        startup_cmd = f'SESSION_ID=""; cd "{target_dir}"'
    try:
        set_opt_cmd = ["tmux", "-L", socket, "set-option", "-w"]
        if window_id:
            set_opt_cmd += ["-t", window_id]
        set_opt_cmd += ["@startup_command", startup_cmd]
        subprocess.run(set_opt_cmd, check=True, timeout=5)
        log(f":cd set @startup_command for session {new_session_id} in {target_dir}")
        # If we didn't get pane_id from session lookup, fall back to querying focused pane.
        if not pane_id:
            try:
                r = subprocess.run(
                    ["tmux", "-L", socket, "display-message", "-p", "#{pane_id}"],
                    capture_output=True, text=True, timeout=5
                )
                pane_id = r.stdout.strip()
            except Exception:
                pane_id = ""
        target_arg = f"-t {pane_id}" if pane_id else ""
        log(f":cd RESPAWN pane={pane_id!r} window={window_id!r} "
            f"new_session={new_session_id} startup={startup_cmd!r}"
            + ("" if pane_id else "  ⚠ no pane_id — respawning FOCUSED pane"))
        # start_new_session=True detaches from the pane's process group so that
        # respawn-pane killing the pane doesn't also kill this scheduler.
        subprocess.Popen([
            "sh", "-c",
            f"sleep 0.5 && tmux -L {socket} respawn-pane -k {target_arg} 2>/dev/null"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
           start_new_session=True)
    except Exception as e:
        log(f":cd failed to set @startup_command: {e} — falling back to window-open approach")
        if new_session_id:
            if socket.startswith("cl1-"):
                claude_config = os.environ.get('CLAUDE_CONFIG_DIR', str(claude_data_dir))
                claude_bin = get_claude_binary(ctx.profile)
                launcher = make_one_tab_launcher(target_dir, new_session_id, claude_config, claude_bin)
                one_tab_relaunch(socket, launcher, current_window_id)
            else:
                open_new_multi_tab_window(socket, ctx.profile, target_dir, new_session_id, current_window_id)

    ctx.message(f"✓ Moving to {target_dir}...")
    return ctx.stop(f"✓ Moving to {target_dir}")


# ── Commands ─────────────────────────────────────────────────────────────────

@command(':cd', local_only=False)   # self-contained on whichever machine runs it
def cmd_cd(ctx):
    target_dir = ctx.args.strip()
    if not target_dir:
        return ctx.stop("Usage: :cd <path>")
    target_dir = str(Path(target_dir).expanduser().resolve())
    if not os.path.isdir(target_dir):
        ctx.message(f"❌ Directory does not exist: {target_dir}")
        return ctx.stop(f"❌ Directory does not exist: {target_dir}")
    if ctx.session_id:
        push_dir_stack(ctx.session_id, ctx.cwd)
    return clone_session_and_change_directory(target_dir, ctx.cwd, ctx)


@command(':cdpop')
def cmd_cdpop(ctx):
    if not ctx.session_id:
        return ctx.stop("❌ No session ID")
    target_dir = pop_dir_stack(ctx.session_id)
    if not target_dir:
        return ctx.stop("❌ Directory stack is empty")
    if not os.path.isdir(target_dir):
        return ctx.stop(f"❌ Directory does not exist: {target_dir}")
    return clone_session_and_change_directory(target_dir, ctx.cwd, ctx)


@command(':cd-tmux')
def cmd_cd_tmux(ctx):
    try:
        result = run(
            ["tmux", "-L", "default", "display-message", "-p", "-t", "0", "#{pane_current_path}"],
            capture_output=True, text=True, check=True
        )
        target_dir = result.stdout.strip()
        if not target_dir:
            return ctx.stop("❌ Could not get directory from tmux session 0")
    except subprocess.CalledProcessError:
        return ctx.stop("❌ Could not access tmux session 0 on default server")
    if not os.path.isdir(target_dir):
        return ctx.stop(f"❌ Directory does not exist: {target_dir}")
    if ctx.session_id:
        push_dir_stack(ctx.session_id, ctx.cwd)
    return clone_session_and_change_directory(target_dir, ctx.cwd, ctx)


def _move_relative(ctx, target_name, offset, label):
    """Move current window to target_name's index ± 1. label is 'after'/'before'."""
    socket = ctx.socket
    try:
        out = subprocess.run(
            ["tmux", "-L", socket, "list-windows",
             "-F", "#{window_index}\t#{window_name}"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout
    except Exception as e:
        return ctx.stop(f"❌ tmux list-windows failed: {e}")

    target_idx = None
    occupied = set()
    for line in out.splitlines():
        if "\t" not in line:
            continue
        idx_s, name = line.split("\t", 1)
        try:
            idx = int(idx_s)
        except ValueError:
            continue
        occupied.add(idx)
        if name == target_name and target_idx is None:
            target_idx = idx

    if target_idx is None:
        return ctx.stop(f"❌ no window named '{target_name}'")

    try:
        cur_id = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "#{window_id}"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
        cur_idx = int(subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "#{window_index}"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip())
    except Exception as e:
        return ctx.stop(f"❌ could not resolve current window: {e}")

    if cur_idx == target_idx:
        return ctx.stop(f"❌ current window IS '{target_name}'")

    dest_idx = target_idx + offset
    if dest_idx < 1:
        return ctx.stop(f"❌ no slot {label} '{target_name}' (it's the first window)")
    if cur_idx == dest_idx:
        return ctx.stop(f"✓ already {label} '{target_name}'")

    if dest_idx in occupied:
        cmd = ["tmux", "-L", socket, "swap-window", "-d",
               "-s", cur_id, "-t", f":{dest_idx}"]
    else:
        cmd = ["tmux", "-L", socket, "move-window", "-d",
               "-s", cur_id, "-t", f":{dest_idx}"]
    try:
        subprocess.run(cmd, check=True, capture_output=True,
                       text=True, timeout=5)
    except subprocess.CalledProcessError as e:
        return ctx.stop(f"❌ move failed: {e.stderr.strip() or e}")
    # Keep focus on the (now-moved) window — without this, tmux may switch
    # to whichever window ended up in the old slot.
    try:
        subprocess.run(
            ["tmux", "-L", socket, "select-window", "-t", cur_id],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass
    return ctx.stop(f"✓ moved {label} '{target_name}'")


@command(':echo', independent=True)
def cmd_echo(ctx):
    """Echo back whatever follows. Proof that colon commands can print."""
    return ctx.stop(ctx.args)


@command(':go', independent=True)
def cmd_go(ctx):
    """Jump to a window by name (or index).

    Usage: :go <window-name>
    """
    target = ctx.args.strip()
    if not target:
        return ctx.stop("Usage: :go <window-name>")
    socket = ctx.socket
    try:
        result = run(
            ["tmux", "-L", socket, "select-window", "-t", target],
            capture_output=True, text=True,
        )
    except Exception as e:
        return ctx.stop(f"❌ select-window failed: {e}")
    if result.returncode != 0:
        return ctx.stop(f"❌ No window matching '{target}' on socket {socket}")
    return ctx.stop(f"✓ Switched to {target}")


@command(':after')
def cmd_after(ctx):
    """Move the current window to immediately after the named window.

    Usage: :after <window-name>

    If the destination slot is already occupied, the resident window is
    swapped into the current window's old slot.
    """
    target_name = ctx.args.strip()
    if not target_name:
        return ctx.stop("Usage: :after <window-name>")
    return _move_relative(ctx, target_name, +1, "after")


@command(':before')
def cmd_before(ctx):
    """Move the current window to immediately before the named window.

    Usage: :before <window-name>

    If the destination slot is occupied, swaps with the resident window
    (same semantics as :after).
    """
    target_name = ctx.args.strip()
    if not target_name:
        return ctx.stop("Usage: :before <window-name>")
    return _move_relative(ctx, target_name, -1, "before")


@command(':close', independent=True)
def cmd_close(ctx):
    """Close a window; with no argument, close the CURRENT one.

    Usage:
        :close           close the current (active) window
        :close <index>   close window at index N
        :close <name>    close window by name (must be unique)

    Refuses to close the last window. Removes the closed session from
    open-sessions so it won't be restored on the next launch.
    """
    arg = ctx.args.strip()
    socket = ctx.socket
    try:
        out = subprocess.run(
            ["tmux", "-L", socket, "list-windows",
             "-F", "#{window_active}\t#{window_index}\t#{window_name}\t#{window_id}\t#{@session_id}"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout
    except Exception as e:
        return ctx.stop(f"❌ tmux list-windows failed: {e}")

    windows = []  # (idx, name, wid, session_id)
    active = None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        try:
            idx = int(parts[1])
        except ValueError:
            continue
        w = (idx, parts[2], parts[3], parts[4])
        windows.append(w)
        if parts[0] == "1":
            active = w

    if len(windows) <= 1:
        return ctx.stop("❌ refusing to close the last window")

    if not arg:
        target = active
        if not target:
            return ctx.stop("❌ could not determine the current window")
    elif arg.isdigit():
        idx_arg = int(arg)
        target = next((w for w in windows if w[0] == idx_arg), None)
        if not target:
            return ctx.stop(f"❌ no window at index {idx_arg}")
    else:
        matches = [w for w in windows if w[1] == arg]
        if not matches:
            return ctx.stop(f"❌ no window named '{arg}'")
        if len(matches) > 1:
            return ctx.stop(f"❌ {len(matches)} windows named '{arg}' — use the index")
        target = matches[0]

    idx, name, wid, sess_id = target
    if sess_id:
        try:
            from clauthing.session import remove_open_session
            remove_open_session(sess_id, ctx.profile)
        except Exception as e:
            log(f":close remove_open_session failed: {e}", ctx.profile)

    try:
        subprocess.run(
            ["tmux", "-L", socket, "kill-window", "-t", wid],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except subprocess.CalledProcessError as e:
        return ctx.stop(f"❌ kill-window failed: {e.stderr.strip() or e}")

    return ctx.stop(f"✓ closed window {idx} ({name})")


@command(':first')
def cmd_first(ctx):
    """Cycle the window list so the current window is first.

    Windows before the current one are rotated to the end (preserving
    their relative order); current ends up at base-index. Focus stays
    on the current window.
    """
    socket = ctx.socket
    try:
        out = subprocess.run(
            ["tmux", "-L", socket, "list-windows",
             "-F", "#{window_index}\t#{window_id}"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout
    except Exception as e:
        return ctx.stop(f"❌ tmux list-windows failed: {e}")

    windows = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        idx_s, wid = line.split("\t", 1)
        try:
            windows.append((int(idx_s), wid))
        except ValueError:
            pass
    windows.sort()
    if not windows:
        return ctx.stop("❌ no windows")

    try:
        cur_id = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "#{window_id}"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
    except Exception as e:
        return ctx.stop(f"❌ could not get current window: {e}")

    cur_pos = next((i for i, (_, wid) in enumerate(windows) if wid == cur_id), None)
    if cur_pos is None:
        return ctx.stop("❌ current window not found in list")
    if cur_pos == 0:
        return ctx.stop("✓ already first")

    # Move each window before current to a fresh index beyond the current
    # max, in order — this preserves their relative order. Then renumber.
    max_idx = windows[-1][0]
    free = max_idx + 1
    for _, wid in windows[:cur_pos]:
        try:
            subprocess.run(
                ["tmux", "-L", socket, "move-window", "-d",
                 "-s", wid, "-t", f":{free}"],
                check=True, capture_output=True, text=True, timeout=5,
            )
            free += 1
        except subprocess.CalledProcessError as e:
            return ctx.stop(f"❌ move failed: {e.stderr.strip() or e}")

    try:
        subprocess.run(
            ["tmux", "-L", socket, "move-window", "-r"],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except subprocess.CalledProcessError as e:
        return ctx.stop(f"❌ renumber failed: {e.stderr.strip() or e}")

    try:
        subprocess.run(
            ["tmux", "-L", socket, "select-window", "-t", cur_id],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass
    return ctx.stop("✓ cycled to first")


def _resolve_reload_target(socket, target):
    """Resolve a window name/index to (window_id, pane_id, session_id, cwd)."""
    try:
        r = run(["tmux", "-L", socket, "list-windows", "-F",
                 "#{window_index}\t#{window_name}\t#{window_id}\t#{pane_id}\t"
                 "#{@session_id}\t#{pane_current_path}"],
                capture_output=True, text=True)
    except Exception:
        return None
    for line in (r.stdout or "").splitlines():
        p = line.split("\t")
        if len(p) >= 6 and target in (p[0], p[1]):
            return p[2], p[3], p[4], p[5]
    return None


@command(':reload')
def cmd_reload(ctx):
    """Reload Claude (pick up config changes). `:reload <window>` reloads a
    named/indexed window instead of the current one."""
    socket = ctx.socket
    profile = ctx.profile
    target = ctx.args.strip()

    if target:
        info = _resolve_reload_target(socket, target)
        if not info:
            return ctx.stop(f"❌ No window matching '{target}'")
        window_id, pane_id, session_id, current_dir = info
        if not session_id:
            return ctx.stop(f"❌ Window '{target}' has no session")
    else:
        session_id = ctx.session_id
        if not session_id:
            return ctx.stop("❌ No session ID available")
        current_dir = ctx.cwd
        window_id = None
        pane_id = os.environ.get("TMUX_PANE") or None

    build_claude_md(profile)

    from clauthing.main import regenerate_tmux_config
    if profile:
        base_config = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "clauthing"
    session_config_dir = base_config / "session-configs" / session_id
    try:
        regenerate_tmux_config(session_config_dir, profile, socket)
    except Exception as e:
        log(f"Error regenerating tmux config: {e}", profile)

    from clauthing.claude import save_auth_from_session, setup_session_config
    from clauthing.events import update_window
    from clauthing.colon_command import record_title

    save_auth_from_session(session_id, profile)
    session_config_dir = setup_session_config(session_id, profile)

    win_target = ["-t", window_id] if window_id else []
    try:
        result = run(
            ["tmux", "-L", socket, "display-message", *win_target, "-p", "#{window_name}"],
            capture_output=True, text=True, check=True
        )
        window_name = result.stdout.strip()
        update_window(session_id, window_name, socket, current_dir, profile)
        record_title(window_name, profile)
    except Exception as e:
        window_name = None
        log(f"Error updating window: {e}", profile)

    # Boomerang: replace in-place via @startup_command + respawn-pane. Target
    # the resolved window/pane so `:reload <window>` reloads THAT window.
    startup_cmd = f'SESSION_ID="{session_id}"; cd "{current_dir}"'
    try:
        subprocess.run(
            ["tmux", "-L", socket, "set-option", "-w", *win_target,
             "@startup_command", startup_cmd],
            check=True, timeout=5
        )
        if not pane_id:
            try:
                r = subprocess.run(
                    ["tmux", "-L", socket, "display-message", "-p", "#{pane_id}"],
                    capture_output=True, text=True, timeout=5
                )
                pane_id = r.stdout.strip()
            except Exception:
                pane_id = ""
        target_arg = f"-t {pane_id}" if pane_id else ""
        subprocess.Popen([
            "sh", "-c",
            f"sleep 0.5 && tmux -L {socket} respawn-pane -k {target_arg} 2>/dev/null"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
           start_new_session=True)
        note = f"✓ Reloading {target}..." if target else "✓ Reloading..."
        ctx.message(note)
        return ctx.stop(note)
    except Exception as e:
        log(f":reload failed to set @startup_command: {e} — falling back", profile)

    if target:
        return ctx.stop(f"❌ Reload of '{target}' failed")

    if socket.startswith("cl1-"):
        claude_bin = get_claude_binary(profile)
        launcher = make_one_tab_launcher(current_dir, session_id, str(session_config_dir), claude_bin)
        one_tab_relaunch(socket, launcher)
        ctx.message("✓ Reloading...")
        return ctx.stop("✓ Reloading...")

    current_window_id = get_current_window_id(socket)
    open_new_multi_tab_window(socket, profile, current_dir, session_id, current_window_id, window_name)
    ctx.message("✓ Reloaded with same session")
    return ctx.stop("✓ Reloaded with same session")


@command(':clear')
def cmd_clear(ctx):
    socket = ctx.socket
    current_dir = ctx.cwd

    if socket.startswith("cl1-"):
        claude_config = os.environ.get('CLAUDE_CONFIG_DIR', str(ctx.claude_data_dir))
        uid = os.getuid()
        launcher = Path(f"/tmp/cl-clear-{uid}.sh")
        launcher.write_text(f'''#!/bin/sh
export CLAUDE_CONFIG_DIR="{claude_config}"
cd "{current_dir}"
exec claude
''')
        launcher.chmod(0o755)
        one_tab_relaunch(socket, launcher)
        ctx.message("✓ Clearing session...")
        return ctx.stop("✓ Clearing session...")

    clauthing_path = shutil.which("clauthing") or "clauthing"
    current_window_id = get_current_window_id(socket)
    profile = ctx.profile

    try:
        result = run(
            ["tmux", "-L", socket, "display-message", "-p", "#{window_name}"],
            capture_output=True, text=True, check=True
        )
        window_name = result.stdout.strip()
    except:
        window_name = None

    cmd_parts = [clauthing_path]
    if profile:
        cmd_parts.extend(["--profile", profile])
    cmd_parts.append("--new-claude")
    cmd_str = " ".join(cmd_parts)

    new_window_cmd = ["tmux", "-L", socket, "new-window"]
    if window_name:
        new_window_cmd.extend(["-n", window_name])
    new_window_cmd.extend(["-c", current_dir, cmd_str])
    run(new_window_cmd)

    if current_window_id:
        subprocess.Popen([
            "sh", "-c",
            f"sleep 2 && tmux -L {socket} kill-window -t {current_window_id} 2>/dev/null || true"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ctx.message("✓ Starting fresh session")
    return ctx.stop("✓ Starting fresh session")


@command(':fork')
def cmd_fork(ctx):
    encoded_current = encode_project_path(ctx.cwd)
    projects_dir = ctx.claude_data_dir / "projects" / encoded_current
    if not projects_dir.exists():
        return ctx.stop("❌ No session found")

    session_files = sorted(projects_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not session_files:
        return ctx.stop("❌ No session found")

    fork_session_id = str(uuid.uuid4())
    shutil.copy2(session_files[0], projects_dir / f"{fork_session_id}.jsonl")

    ctx.message("🔀 Forking to new window...")
    from clauthing.claude import new_window
    new_window(profile=ctx.profile, resume_session_id=fork_session_id, socket=ctx.socket)
    return ctx.stop("✓ Forked conversation to new window")


@command(':call')
def cmd_call(ctx):
    encoded_current = encode_project_path(ctx.cwd)
    projects_dir = ctx.claude_data_dir / "projects" / encoded_current
    if not projects_dir.exists():
        return ctx.stop("❌ No session found")

    session_files = sorted(projects_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not session_files:
        return ctx.stop("❌ No session found")

    call_session_id = str(uuid.uuid4())
    call_file = projects_dir / f"{call_session_id}.jsonl"
    shutil.copy2(session_files[0], call_file)

    ctx.message("📞 Opening call in popup...")
    run(["tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "90%", "-h", "90%",
         f"claude --resume {call_session_id}"])

    try:
        from clauthing.session_utils import get_last_assistant_message
        last_message = get_last_assistant_message(call_file)
        if last_message:
            ctx.message("✓ Call completed, injecting response")
            escaped = shlex.quote(f"Call result:\n\n{last_message}")
            subprocess.Popen([
                "sh", "-c",
                f"sleep 0.5 && tmux -L {ctx.socket} send-keys -l {escaped} && tmux -L {ctx.socket} send-keys Enter"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return ctx.stop("")
        else:
            return ctx.stop("Call had no responses")
    except Exception as e:
        return ctx.stop(f"Call error: {str(e)}")


@command(':ask')
def cmd_ask(ctx):
    encoded_current = encode_project_path(ctx.cwd)
    projects_dir = ctx.claude_data_dir / "projects" / encoded_current
    projects_dir.mkdir(parents=True, exist_ok=True)

    ask_session_id = str(uuid.uuid4())
    ask_file = projects_dir / f"{ask_session_id}.jsonl"
    ask_file.touch()

    ctx.message("❓ Opening ask in popup...")
    run(["tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "90%", "-h", "90%",
         f"claude --resume {ask_session_id}"])

    try:
        from clauthing.session_utils import get_last_assistant_message
        last_message = get_last_assistant_message(ask_file)
        if last_message:
            ctx.message("✓ Ask completed, injecting response")
            escaped = shlex.quote(f"Ask result:\n\n{last_message}")
            subprocess.Popen([
                "sh", "-c",
                f"sleep 0.5 && tmux -L {ctx.socket} send-keys -l {escaped} && tmux -L {ctx.socket} send-keys Enter"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return ctx.stop("")
        else:
            return ctx.stop("Ask had no responses")
    except Exception as e:
        return ctx.stop(f"Ask error: {str(e)}")


@command(':shortcuts', independent=True)
def cmd_shortcuts(ctx):
    """Show the tmux keybindings, searchable with fzf."""
    from clauthing.main import KEYBINDINGS_HELP
    # Keep only the "  KEY   desc" lines (drop the ANSI header + blanks).
    lines = [l.strip() for l in KEYBINDINGS_HELP.splitlines() if l.startswith("  ")]
    if not ctx.socket:
        return ctx.stop("\n".join(lines))
    uid = os.getuid()
    tmp_in = Path(f"/tmp/cl-shortcuts-{uid}.txt")
    tmp_in.write_text("\n".join(lines))
    try:
        subprocess.run([
            "tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "60%", "-h", "70%",
            f"cat {tmp_in} | fzf --header='keybindings — type to filter, Esc to close'"
        ], timeout=300)
    except Exception as e:
        return ctx.stop(f"❌ Could not show shortcuts: {e}")
    finally:
        tmp_in.unlink(missing_ok=True)
    return ctx.stop("")


# A trailing "(other-window)" marks this window as blocked on that one.
_BLOCKED_RE = re.compile(r"\s*\([^)]*\)\s*$")


@command(':blocked', independent=True)
def cmd_blocked(ctx):
    """Mark this window as blocked on another — puts (window) in its name.

    :blocked <window>   mark this window blocked on <window>
    :blocked manual     mark this window blocked on reality (no window to
                        validate — you'll switch to the thing yourself)
    :blocked            clear the marker

    Display only for now: the name shows what you're waiting on.
    """
    target = ctx.args.strip()
    pane = os.environ.get("TMUX_PANE")
    pt = ["-t", pane] if pane else []
    try:
        r = run(["tmux", "-L", ctx.socket, "display-message", *pt,
                 "-p", "#{window_name}"], capture_output=True, text=True)
        current = (r.stdout or "").strip()
    except Exception:
        current = ""
    if not current:
        return ctx.stop("❌ Could not read the current window name")

    base = _BLOCKED_RE.sub("", current)      # drop any existing marker
    if not target:
        if base == current:
            return ctx.stop(f"Not blocked ({current})")
        new_name = base
    elif target == "manual":
        # blocked on reality: there's no window to switch to, so skip the
        # window check — you'll switch back to the thing yourself.
        new_name = f"{base} (manual)"
    else:
        try:
            r = run(["tmux", "-L", ctx.socket, "list-windows", "-F", "#{window_name}"],
                    capture_output=True, text=True)
            names = [n for n in (r.stdout or "").splitlines() if n]
        except Exception:
            names = []
        # match either the literal name or its un-marked base
        if target not in names and target not in {_BLOCKED_RE.sub("", n) for n in names}:
            return ctx.stop(f"❌ No window matching '{target}'")
        if target == base:
            return ctx.stop("❌ A window can't be blocked on itself")
        new_name = f"{base} ({target})"

    try:
        subprocess.run(["tmux", "-L", ctx.socket, "rename-window", *pt, new_name],
                       check=True, timeout=5)
    except Exception as e:
        return ctx.stop(f"❌ Rename failed: {e}")
    return ctx.stop(f"✓ {new_name}" if target else f"✓ Unblocked ({new_name})")


@command(':rename')
def cmd_rename(ctx):
    """Rename the CURRENT window. Usage: :rename <new name>

    Targets the current pane (via $TMUX_PANE), not whichever window the client
    is viewing. The window-renamed tmux hook mirrors the new name into
    clauthing's session metadata/state, so it persists across reloads.
    """
    new_name = ctx.args.strip()
    if not new_name:
        return ctx.stop("Usage: :rename <new name>")
    pane = os.environ.get("TMUX_PANE")
    target = ["-t", pane] if pane else []
    try:
        subprocess.run(
            ["tmux", "-L", ctx.socket, "rename-window", *target, new_name],
            check=True, timeout=5,
        )
    except Exception as e:
        return ctx.stop(f"❌ Rename failed: {e}")
    ctx.message(f"✓ Renamed to '{new_name}'")
    return ctx.stop(f"✓ Renamed to '{new_name}'")


@command(':checkpoint')
def cmd_checkpoint(ctx):
    if not ctx.session_id:
        return ctx.stop("❌ No session ID available")
    encoded = encode_project_path(ctx.cwd)
    session_file = ctx.claude_data_dir / "projects" / encoded / f"{ctx.session_id}.jsonl"
    if not session_file.exists():
        return ctx.stop("❌ Session file not found")
    add_checkpoint_to_session(session_file)
    ctx.message("✓ Checkpoint saved")
    return ctx.stop("✓ Checkpoint saved")


@command(':rollback')
def cmd_rollback(ctx):
    session_id = ctx.session_id
    if not session_id:
        return ctx.stop("❌ No session ID available")

    socket = ctx.socket
    current_dir = ctx.cwd
    encoded = encode_project_path(current_dir)
    projects_dir = ctx.claude_data_dir / "projects" / encoded

    source_file = projects_dir / f"{session_id}.jsonl"
    if not source_file.exists():
        return ctx.stop("❌ Session file not found")

    new_session_id = str(uuid.uuid4())
    target_file = projects_dir / f"{new_session_id}.jsonl"

    if not rollback_session_to_checkpoint(source_file, target_file):
        return ctx.stop("❌ No checkpoint found in session")

    save_session_metadata(new_session_id, get_session_name(session_id), current_dir)
    carry_over_session_state(session_id, new_session_id)

    current_window_id = get_current_window_id(socket)

    if socket.startswith("cl1-"):
        claude_config = os.environ.get('CLAUDE_CONFIG_DIR', str(ctx.claude_data_dir))
        claude_bin = get_claude_binary(ctx.profile)
        launcher = make_one_tab_launcher(current_dir, new_session_id, claude_config, claude_bin)
        one_tab_relaunch(socket, launcher, current_window_id)
        ctx.message("✓ Rolling back to checkpoint...")
        return ctx.stop("✓ Rolling back to checkpoint")

    open_new_multi_tab_window(socket, ctx.profile, current_dir, new_session_id, current_window_id)
    ctx.message("✓ Rolled back to checkpoint")
    return ctx.stop("✓ Rolled back to checkpoint")


@command(':login')
def cmd_login(ctx):
    session_id = ctx.session_id
    if not session_id:
        return ctx.stop("❌ No session ID available")

    socket = ctx.socket
    profile = ctx.profile

    if profile:
        base_config = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "clauthing"

    session_configs_dir = base_config / "session-configs"

    import time
    best_expiry = 0
    best_creds_content = None
    best_source = None
    for session_dir in session_configs_dir.iterdir():
        if not session_dir.is_dir():
            continue
        creds_file = session_dir / ".credentials.json"
        if not creds_file.exists():
            continue
        try:
            content = creds_file.read_text()
            data = json.loads(content)
            expiry = data.get("claudeAiOauth", {}).get("expiresAt", 0)
            if expiry > best_expiry:
                best_expiry = expiry
                best_creds_content = content
                best_source = session_dir.name[:8]
        except Exception:
            continue

    if not best_creds_content:
        return ctx.stop("❌ No valid credentials found")

    now_ms = int(time.time() * 1000)
    if best_expiry < now_ms:
        return ctx.stop("❌ All credentials expired - need manual login")

    current_session_creds = session_configs_dir / session_id / ".credentials.json"
    try:
        current_session_creds.parent.mkdir(parents=True, exist_ok=True)
        current_session_creds.write_text(best_creds_content)
        shared_creds = base_config / "claude-data" / ".credentials.json"
        if shared_creds.exists() or shared_creds.is_symlink():
            shared_creds.unlink()
        shared_creds.write_text(best_creds_content)
    except Exception as e:
        return ctx.stop(f"❌ Failed to copy credentials: {e}")

    remaining = (best_expiry - now_ms) // 60000
    ctx.message(f"✓ Credentials from {best_source} - reloading...")

    from clauthing.colon_command import queue_startup_message
    queue_startup_message(session_id,
        f"✓ Logged in with credentials from session {best_source} ({remaining} min remaining)",
        profile)

    current_dir = ctx.cwd
    build_claude_md(profile)
    from clauthing.claude import save_auth_from_session, setup_session_config
    save_auth_from_session(session_id, profile)
    session_config_dir = setup_session_config(session_id, profile)

    if socket.startswith("cl1-"):
        claude_bin = get_claude_binary(profile)
        launcher = make_one_tab_launcher(current_dir, session_id, str(session_config_dir), claude_bin)
        one_tab_relaunch(socket, launcher)
        return ctx.stop("")

    # Multi-tab boomerang: respawn-pane in place so the same tmux window
    # keeps living, just with a fresh claude that reads the new creds.
    startup_cmd = f'SESSION_ID="{session_id}"; cd "{current_dir}"'
    try:
        subprocess.run(
            ["tmux", "-L", socket, "set-option", "-w", "@startup_command", startup_cmd],
            check=True, timeout=5,
        )
        try:
            r = subprocess.run(
                ["tmux", "-L", socket, "display-message", "-p", "#{pane_id}"],
                capture_output=True, text=True, timeout=5,
            )
            pane_id = r.stdout.strip()
        except Exception:
            pane_id = ""
        target_arg = f"-t {pane_id}" if pane_id else ""
        subprocess.Popen(
            ["sh", "-c",
             f"sleep 0.5 && tmux -L {socket} respawn-pane -k {target_arg} 2>/dev/null"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        log(f":login boomerang failed: {e} — falling back to kill+spawn", profile)
        clauthing_path = shutil.which("clauthing") or "clauthing"
        subprocess.Popen([clauthing_path, "--resume-session", session_id])
        subprocess.Popen([
            "sh", "-c",
            f"sleep 1.5 && tmux -L {socket} kill-pane",
        ])
    return ctx.stop("")


@command(':god')
def cmd_god(ctx):
    return _enable_plan_mcp(ctx)

@command(':planner')
def cmd_planner(ctx):
    return _enable_plan_mcp(ctx)

@command(':plan')
def cmd_plan(ctx):
    return _enable_plan_mcp(ctx)

def _enable_plan_mcp(ctx):
    session_id = ctx.session_id
    if not session_id:
        return ctx.stop("❌ No session ID available")

    socket = ctx.socket
    current_dir = ctx.cwd
    profile = ctx.profile
    clauthing_path = shutil.which("clauthing") or "clauthing"

    mcp_config_file = Path(current_dir) / ".mcp.json"
    if mcp_config_file.exists():
        try:
            mcp_config = json.loads(mcp_config_file.read_text())
        except:
            mcp_config = {"mcpServers": {}}
    else:
        mcp_config = {"mcpServers": {}}

    mcp_config.setdefault("mcpServers", {})
    mcp_config["mcpServers"]["clauthing-planning"] = {
        "command": clauthing_path,
        "args": ["--plan-mcp"]
    }

    try:
        mcp_config_file.write_text(json.dumps(mcp_config, indent=2) + "\n")
    except Exception as e:
        return ctx.stop(f"❌ Error writing .mcp.json: {str(e)}")

    build_claude_md(profile)
    from clauthing.claude import save_auth_from_session, setup_session_config
    save_auth_from_session(session_id, profile)
    session_config_dir = setup_session_config(session_id, profile)

    if socket.startswith("cl1-"):
        claude_bin = get_claude_binary(profile)
        launcher = make_one_tab_launcher(current_dir, session_id, str(session_config_dir), claude_bin)
        one_tab_relaunch(socket, launcher)
        return ctx.stop("✓ God mode enabled. Reloading...")

    current_window_id = get_current_window_id(socket)
    open_new_multi_tab_window(socket, profile, current_dir, session_id, current_window_id)
    return ctx.stop("✓ God mode enabled. Reloading...")