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

from kitty_claude.colon_command import command, send_tmux_message
from kitty_claude.claude_utils import encode_project_path
from kitty_claude.logging import log, run
from kitty_claude.session import get_session_name, save_session_metadata
from kitty_claude.session_utils import session_has_messages
from kitty_claude.rules import build_claude_md


def get_state_dir():
    xdg_state = os.environ.get('XDG_STATE_HOME')
    if xdg_state:
        state_dir = Path(xdg_state) / "kitty-claude"
    else:
        state_dir = Path.home() / ".local" / "state" / "kitty-claude"
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
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
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
    launcher = Path(f"/tmp/kc-launch-{uid}-{session_id[:8]}.sh")
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
    if old_meta_file.exists() and new_meta_file.exists():
        try:
            old_meta = json.loads(old_meta_file.read_text())
            new_meta = json.loads(new_meta_file.read_text())
            for key in ("dir_stack", "mcpServers", "linked_tmux_window", "linked_tmux_windows"):
                if key in old_meta:
                    new_meta[key] = old_meta[key]
            new_meta_file.write_text(json.dumps(new_meta, indent=2))
        except:
            pass


def open_new_multi_tab_window(socket, profile, target_dir, session_id, current_window_id=None, window_name=None):
    """Open new window in multi-tab mode, optionally close old one."""
    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
    cmd_parts = [kitty_claude_path]
    if profile:
        cmd_parts.extend(["--profile", profile])
    cmd_parts.extend(["--new-window", "--resume-session", session_id])
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

    encoded_current = encode_project_path(current_dir)
    encoded_target = encode_project_path(target_dir)

    projects_dir = claude_data_dir / "projects" / encoded_current
    if not projects_dir.exists():
        ctx.message("❌ Claude cannot resume without a message. Send one first.")
        return ctx.stop("❌ Claude cannot resume without a message. Send one first.")

    session_files = sorted(projects_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not session_files:
        ctx.message("❌ Claude cannot resume without a message. Send one first.")
        return ctx.stop("❌ Claude cannot resume without a message. Send one first.")

    source_file = session_files[0]
    if not session_has_messages(source_file):
        ctx.message("❌ Claude cannot resume without a message. Send one first.")
        return ctx.stop("❌ Claude cannot resume without a message. Send one first.")

    old_session_id = source_file.stem
    new_session_id = str(uuid.uuid4())
    current_window_id = get_current_window_id(socket)

    target_projects_dir = claude_data_dir / "projects" / encoded_target
    target_projects_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(session_files[0], target_projects_dir / f"{new_session_id}.jsonl")

    save_session_metadata(new_session_id, get_session_name(old_session_id), target_dir)
    carry_over_session_state(old_session_id, new_session_id)

    if socket.startswith("kc1-"):
        claude_config = os.environ.get('CLAUDE_CONFIG_DIR', str(claude_data_dir))
        claude_bin = get_claude_binary(ctx.profile)

        log(f"one-tab :cd - config={claude_config}, session={new_session_id}, target={target_dir}")
        log(f"one-tab :cd - claude binary: {claude_bin}")

        session_file = target_projects_dir / f"{new_session_id}.jsonl"
        log(f"one-tab :cd - session file exists: {session_file.exists()}")

        launcher = make_one_tab_launcher(target_dir, new_session_id, claude_config, claude_bin)
        log(f"one-tab :cd - launcher script: {launcher}")

        one_tab_relaunch(socket, launcher, current_window_id)
        ctx.message(f"✓ Changing to {target_dir}...")
        return ctx.stop(f"✓ Changing to {target_dir}")

    open_new_multi_tab_window(socket, ctx.profile, target_dir, new_session_id, current_window_id)
    ctx.message(f"✓ Moving to {target_dir}")
    return ctx.stop(f"✓ Moving to {target_dir}")


# ── Commands ─────────────────────────────────────────────────────────────────

@command(':cd')
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


@command(':reload')
def cmd_reload(ctx):
    session_id = ctx.session_id
    if not session_id:
        return ctx.stop("❌ No session ID available")

    socket = ctx.socket
    current_dir = ctx.cwd
    profile = ctx.profile

    build_claude_md(profile)

    from kitty_claude.main import regenerate_tmux_config
    if profile:
        base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "kitty-claude"
    session_config_dir = base_config / "session-configs" / session_id
    try:
        regenerate_tmux_config(session_config_dir, profile, socket)
    except Exception as e:
        log(f"Error regenerating tmux config: {e}", profile)

    from kitty_claude.claude import save_auth_from_session, setup_session_config
    from kitty_claude.events import update_window
    from kitty_claude.colon_command import record_title

    save_auth_from_session(session_id, profile)
    session_config_dir = setup_session_config(session_id, profile)

    try:
        result = run(
            ["tmux", "-L", socket, "display-message", "-p", "#{window_name}"],
            capture_output=True, text=True, check=True
        )
        window_name = result.stdout.strip()
        update_window(session_id, window_name, socket, current_dir, profile)
        record_title(window_name, profile)
    except Exception as e:
        window_name = None
        log(f"Error updating window: {e}", profile)

    if socket.startswith("kc1-"):
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

    if socket.startswith("kc1-"):
        claude_config = os.environ.get('CLAUDE_CONFIG_DIR', str(ctx.claude_data_dir))
        uid = os.getuid()
        launcher = Path(f"/tmp/kc-clear-{uid}.sh")
        launcher.write_text(f'''#!/bin/sh
export CLAUDE_CONFIG_DIR="{claude_config}"
cd "{current_dir}"
exec claude
''')
        launcher.chmod(0o755)
        one_tab_relaunch(socket, launcher)
        ctx.message("✓ Clearing session...")
        return ctx.stop("✓ Clearing session...")

    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
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

    cmd_parts = [kitty_claude_path]
    if profile:
        cmd_parts.extend(["--profile", profile])
    cmd_parts.append("--new-window")
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
    from kitty_claude.claude import new_window
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
        from kitty_claude.session_utils import get_last_assistant_message
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
        from kitty_claude.session_utils import get_last_assistant_message
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

    if socket.startswith("kc1-"):
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
        base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "kitty-claude"

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

    from kitty_claude.colon_command import queue_startup_message
    queue_startup_message(session_id,
        f"✓ Logged in with credentials from session {best_source} ({remaining} min remaining)",
        profile)

    current_dir = ctx.cwd
    build_claude_md(profile)
    from kitty_claude.claude import save_auth_from_session, setup_session_config
    save_auth_from_session(session_id, profile)
    session_config_dir = setup_session_config(session_id, profile)

    if socket.startswith("kc1-"):
        claude_bin = get_claude_binary(profile)
        launcher = make_one_tab_launcher(current_dir, session_id, str(session_config_dir), claude_bin)
        one_tab_relaunch(socket, launcher)
        return ctx.stop("")

    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
    subprocess.Popen([kitty_claude_path, "--resume-session", session_id])
    subprocess.Popen([
        "sh", "-c",
        f"sleep 1.5 && tmux -L {socket} kill-pane"
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
    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"

    mcp_config_file = Path(current_dir) / ".mcp.json"
    if mcp_config_file.exists():
        try:
            mcp_config = json.loads(mcp_config_file.read_text())
        except:
            mcp_config = {"mcpServers": {}}
    else:
        mcp_config = {"mcpServers": {}}

    mcp_config.setdefault("mcpServers", {})
    mcp_config["mcpServers"]["kitty-claude-planning"] = {
        "command": kitty_claude_path,
        "args": ["--plan-mcp"]
    }

    try:
        mcp_config_file.write_text(json.dumps(mcp_config, indent=2) + "\n")
    except Exception as e:
        return ctx.stop(f"❌ Error writing .mcp.json: {str(e)}")

    build_claude_md(profile)
    from kitty_claude.claude import save_auth_from_session, setup_session_config
    save_auth_from_session(session_id, profile)
    session_config_dir = setup_session_config(session_id, profile)

    if socket.startswith("kc1-"):
        claude_bin = get_claude_binary(profile)
        launcher = make_one_tab_launcher(current_dir, session_id, str(session_config_dir), claude_bin)
        one_tab_relaunch(socket, launcher)
        return ctx.stop("✓ God mode enabled. Reloading...")

    current_window_id = get_current_window_id(socket)
    open_new_multi_tab_window(socket, profile, current_dir, session_id, current_window_id)
    return ctx.stop("✓ God mode enabled. Reloading...")