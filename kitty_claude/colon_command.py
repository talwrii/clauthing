#!/usr/bin/env python3
"""Colon command handlers for kitty-claude (:cd, :fork, :time, etc).

Commands are registered with @command(':name') decorator. Modules in
colon_commands/ register their own commands on import.
"""

import os
import sys
import json
import shutil
import subprocess
import uuid
import shlex
from pathlib import Path

from kitty_claude.logging import log, run
from kitty_claude.colon_commands.time import (
    save_request_start_time,
    save_response_duration,
    get_last_response_duration
)
from kitty_claude.session import (
    get_session_name,
    save_session_metadata,
    remove_open_session
)
from kitty_claude.session_utils import session_has_messages
from kitty_claude.window_utils import open_session_notes
from kitty_claude.tmux import get_runtime_tmux_state_file
from kitty_claude.rules import build_claude_md

import time


# ── Utilities (used by command modules too) ──────────────────────────────────

def get_tmux_socket():
    """Get the tmux socket name from environment or default."""
    socket = os.environ.get('KITTY_CLAUDE_TMUX_SOCKET')
    if socket:
        return socket
    tmux_var = os.environ.get('TMUX', '')
    if tmux_var:
        socket_path = tmux_var.split(',')[0]
        socket_name = os.path.basename(socket_path)
        if socket_name:
            return socket_name
    return 'kitty-claude'


def send_tmux_message(message, socket=None):
    """Send a message via tmux display-message."""
    if socket is None:
        socket = get_tmux_socket()
    try:
        run(["tmux", "-L", socket, "display-message", message], stderr=subprocess.DEVNULL)
    except:
        pass


def get_state_dir():
    """Get the XDG state directory for kitty-claude."""
    xdg_state = os.environ.get('XDG_STATE_HOME')
    if xdg_state:
        state_dir = Path(xdg_state) / "kitty-claude"
    else:
        state_dir = Path.home() / ".local" / "state" / "kitty-claude"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def get_title_history_file(profile=None):
    if profile is None:
        profile = os.environ.get('KITTY_CLAUDE_PROFILE')
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    return config_dir / "title-history.json"


def record_title(title, profile=None):
    """Record a title in the history file."""
    if not title or not title.strip():
        return
    title = title.strip()
    history_file = get_title_history_file(profile)
    history = []
    if history_file.exists():
        try:
            history = json.loads(history_file.read_text())
        except:
            history = []
    found = False
    for entry in history:
        if entry.get("title") == title:
            entry["last_used"] = time.time()
            entry["count"] = entry.get("count", 0) + 1
            found = True
            break
    if not found:
        history.append({"title": title, "last_used": time.time(), "count": 1})
    history.sort(key=lambda x: x.get("last_used", 0), reverse=True)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.write_text(json.dumps(history, indent=2))


def queue_startup_message(session_id, message, profile=None):
    """Queue a message to be shown on next session start."""
    if profile:
        base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "kitty-claude"
    session_dir = base_config / "session-configs" / session_id
    run_file = session_dir / ".run-counter"
    messages_file = session_dir / ".startup-messages"
    current_run = 0
    if run_file.exists():
        try:
            current_run = int(run_file.read_text().strip())
        except (ValueError, OSError):
            pass
    messages = []
    if messages_file.exists():
        try:
            messages = json.loads(messages_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    messages.append({"run": current_run, "text": message})
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        messages_file.write_text(json.dumps(messages))
    except OSError:
        pass


def get_timed_permissions_file():
    config_dir = Path.home() / ".config" / "kitty-claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "timed-permissions.json"


def load_timed_permissions():
    perm_file = get_timed_permissions_file()
    if perm_file.exists():
        try:
            return json.loads(perm_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_timed_permissions(permissions):
    get_timed_permissions_file().write_text(json.dumps(permissions, indent=2))


def parse_duration(duration_str):
    """Parse duration string like '1h', '30m' into seconds."""
    import re
    total = 0
    for value, unit in re.compile(r'(\d+)([hms])').findall(duration_str.lower()):
        value = int(value)
        if unit == 'h': total += value * 3600
        elif unit == 'm': total += value * 60
        elif unit == 's': total += value
    return total if total > 0 else None


def format_remaining_time(seconds):
    if seconds <= 0:
        return "expired"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0: return f"{hours}h{minutes}m"
    elif minutes > 0: return f"{minutes}m{secs}s"
    return f"{secs}s"


def cleanup_expired_timed_permissions(claude_data_dir=None):
    now = time.time()
    timed_perms = load_timed_permissions()
    expired = [p.get('pattern') for p in timed_perms if now > p.get('expires', 0)]
    active = [p for p in timed_perms if now <= p.get('expires', 0)]
    if not expired:
        return
    save_timed_permissions(active)
    if claude_data_dir:
        settings_file = Path(claude_data_dir) / "settings.json"
        if settings_file.exists():
            try:
                settings = json.loads(settings_file.read_text())
                allow_list = settings.get("permissions", {}).get("allow", [])
                original_len = len(allow_list)
                allow_list = [p for p in allow_list if p not in expired]
                if len(allow_list) != original_len:
                    settings["permissions"]["allow"] = allow_list
                    settings_file.write_text(json.dumps(settings, indent=2))
            except (json.JSONDecodeError, OSError):
                pass


# ── Command Registry ─────────────────────────────────────────────────────────

COMMANDS = {}


def command(prefix):
    """Register a colon command handler."""
    def decorator(fn):
        COMMANDS[prefix] = fn
        return fn
    return decorator


def dispatch(prompt, ctx):
    """Dispatch to registered handler. Returns result dict or None."""
    for prefix in sorted(COMMANDS.keys(), key=len, reverse=True):
        if prompt == prefix or prompt.startswith(prefix + ' '):
            return COMMANDS[prefix](ctx)
    return None


class CommandContext:
    """Context passed to every command handler."""
    def __init__(self, prompt, input_data, socket, claude_data_dir):
        self.prompt = prompt
        self.input_data = input_data
        self.socket = socket
        self.claude_data_dir = claude_data_dir

    @property
    def session_id(self):
        return self.input_data.get('session_id')

    @property
    def cwd(self):
        return self.input_data.get('cwd', os.getcwd())

    @property
    def profile(self):
        return os.environ.get('KITTY_CLAUDE_PROFILE')

    @property
    def args(self):
        for prefix in sorted(COMMANDS.keys(), key=len, reverse=True):
            if self.prompt == prefix:
                return ''
            if self.prompt.startswith(prefix + ' '):
                return self.prompt[len(prefix) + 1:]
        return ''

    def stop(self, message):
        return {"continue": False, "stopReason": message}

    def message(self, text):
        send_tmux_message(text, self.socket)


# ── Remaining commands (not big enough for their own module) ─────────────────

@command(':help')
def cmd_help(ctx):
    help_text = """kitty-claude colon commands:
:help                Show this help message
:skills              List available slash commands (skills)
:rules               List all rules
:note                Open session notes in vim
:skill [name]        Create/edit a global Claude skill (fzf if no name)
:rule [name]         Create/edit a global rule (fzf if no name)
:todo [desc]         List todos or add one for current directory
:done <num>          Mark a todo as done by number
:plan / :god         Enable planning MCP server and reload
:skills-mcp          Enable skills MCP server, then :reload
::skills             List all kitty-claude skills
::skill <name>       Create/edit a kitty-claude skill
::<skill> [prompt]   Run kitty-claude skill (injects context)
:mcp <cmd> [args]    Add a native MCP server to this session
:mcp-shell <cmd>     Expose shell command as MCP server
:mcp-approve <cmd>   Add MCP server with tmux approval proxy
:mcps                List MCP servers in this session
:mcp-remove <name>   Remove an MCP server from session
:roles               List available roles
:role [name]         Activate a role (fzf picker if no name)
:role-add <role> <n> Add permission #n to a role
:role-add-all <role> Add all current permissions to a role
:role-add-mcp <r> <s> Add MCP server from session to a role
:roles-current       Show active roles in this session
:title-role [t] [r]  Map tmux window title to a role
:login               Refresh credentials from freshest session
:login-all           Send :login to all kc1-* instances
:reload-all          Send :reload to all kc1-* instances
:send <message>      Send a message to another kitty-claude window
:current-sessions    List all currently running sessions
:sessions [N]        List recent sessions (default 10)
:resume <num|id>     Resume a session in new window
:resume-new [num|id] Resume in a new kitty-claude window
:spawn [title]       Spawn new window (no arg: pick from history)
:clear               Clear session and start fresh
:reload              Reload Claude (pick up config changes)
:cd <path>           Change directory and move session
:cdpop               Return to previous directory
:cd-tmux             Change to directory of tmux session 0
:tmux                Link/switch to a tmux window on default server
:tmux-unlink         Unlink the associated tmux window
:tmuxpath            Show path of linked tmux window
:tmuxscreen          Capture content of linked tmux window
:tmuxs-link          Add current tmux window to linked list
:tmuxs               Pick a linked tmux window (fzf)
:call                Open popup with context, returns result
:ask                 Open popup without context, returns result
:fork                Clone conversation to new window
:permissions         Show allowed commands in this session
:permissions-gui     Open permissions editor GUI
:disallow <num> ...  Remove allowed command(s) by number
:allow-for <dur> <p> Allow tool for duration
:allow-last          Allow the last tool that was used
:allow-recent        Select from recent tools to allow (fzf)
:time                Show duration of last response
:checkpoint          Save a checkpoint in the current session
:rollback            Rollback to the last checkpoint
"""
    plugins = set()
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        try:
            for entry in Path(path_dir).iterdir():
                if entry.name.startswith("kitty-claude-") and os.access(entry, os.X_OK):
                    plugins.add(entry.name[len("kitty-claude-"):])
        except (OSError, PermissionError):
            pass
    if plugins:
        help_text += "\nPlugins (from PATH):\n"
        for name in sorted(plugins):
            help_text += f"  :{name:<20s} (kitty-claude-{name})\n"

    ctx.message("📖 See console for help")
    return ctx.stop(help_text)


@command(':time')
def cmd_time(ctx):
    if not ctx.session_id:
        return ctx.stop("⏱ No session ID available")
    duration = get_last_response_duration(ctx.session_id)
    if duration is None:
        return ctx.stop("⏱ No timing data available yet")
    if duration < 1:
        s = f"{duration * 1000:.0f}ms"
    elif duration < 60:
        s = f"{duration:.1f}s"
    else:
        m = int(duration // 60)
        s = f"{m}m {duration % 60:.1f}s"
    msg = f"⏱ Last response took: {s}"
    ctx.message(msg)
    return ctx.stop(msg)


@command(':skills')
def cmd_skills(ctx):
    skills_dir = ctx.claude_data_dir / "skills"
    if not skills_dir.exists() or not any(skills_dir.iterdir()):
        return ctx.stop("No skills installed.\n\nSkills can be added to .claude/skills/ in your project.")
    skills = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if skill_dir.is_dir():
            name = skill_dir.name
            skills.append(f"  /{name} (project)" if skill_dir.is_symlink() else f"  /{name}")
    ctx.message(f"📋 Found {len(skills)} skills")
    return ctx.stop("Available slash commands:\n\n" + "\n".join(skills) if skills else "No skills found.")


@command(':rules')
def cmd_rules(ctx):
    profile = ctx.profile
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    rules_dir = config_dir / "rules"
    if not rules_dir.exists() or not any(rules_dir.iterdir()):
        return ctx.stop("No rules found.\n\nCreate rules with :rule <name>")
    rules = [f"  {f.stem}" for f in sorted(rules_dir.iterdir()) if f.suffix == '.md']
    ctx.message(f"📋 Found {len(rules)} rules")
    return ctx.stop("Available rules:\n\n" + "\n".join(rules))


@command(':skill')
def cmd_skill(ctx):
    skill_name = ctx.args.strip()
    profile = ctx.profile
    if profile:
        base = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile / "claude-data" / "skills"
    else:
        base = Path.home() / ".config" / "kitty-claude" / "claude-data" / "skills"

    if not skill_name:
        skills = []
        if base.exists():
            for d in sorted(base.iterdir()):
                if d.is_dir():
                    sf = d / "SKILL.md"
                    desc = "(no description)"
                    if sf.exists():
                        for line in sf.read_text().split('\n'):
                            if line.startswith('description:'):
                                desc = line[12:].strip()
                                break
                    skills.append((d.name, desc))
        if not skills:
            return ctx.stop("No skills found.\n\nUse :skill <name> to create a new skill.")
        import tempfile
        fzf_lines = [f"{n}\t{d}" for n, d in skills]
        tmp_in = Path(tempfile.mktemp())
        tmp_out = Path(tempfile.mktemp())
        tmp_in.write_text("\n".join(fzf_lines))
        subprocess.run(["tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "60%", "-h", "50%",
                        f"cat {tmp_in} | fzf --delimiter='\\t' --with-nth=1,2 --header='Select skill to edit' > {tmp_out}"])
        sel = tmp_out.read_text().strip() if tmp_out.exists() else ""
        tmp_in.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)
        if not sel:
            return ctx.stop("No skill selected.")
        skill_name = sel.split('\t')[0]

    if not all(c.isalnum() or c in '-_' for c in skill_name):
        return ctx.stop("❌ Invalid skill name")

    skills_dir = base / skill_name
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skills_dir / "SKILL.md"
    if not skill_file.exists():
        skill_file.write_text(f"---\nname: {skill_name}\ndescription: Execute {skill_name}\n---\nAdd your skill content here.\n")

    subprocess.run(["tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "80%", "-h", "80%", f"vim {skill_file}"])
    ctx.message(f"✓ Skill '{skill_name}' saved - use :reload to apply")
    return ctx.stop(f"✓ Skill '{skill_name}' saved\n\nUse :reload to make the skill available.")


@command(':rule')
def cmd_rule(ctx):
    rule_name = ctx.args.strip()
    profile = ctx.profile
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    rules_dir = config_dir / "rules"

    if not rule_name:
        rules = []
        if rules_dir.exists():
            for f in sorted(rules_dir.iterdir()):
                if f.suffix == '.md':
                    desc = "(no description)"
                    for line in f.read_text().split('\n'):
                        line = line.strip()
                        if line and not line.startswith('#'):
                            desc = line[:50] + ('...' if len(line) > 50 else '')
                            break
                    rules.append((f.stem, desc))
        if not rules:
            return ctx.stop("No rules found.\n\nUse :rule <name> to create a new rule.")
        import tempfile
        fzf_lines = [f"{n}\t{d}" for n, d in rules]
        tmp_in = Path(tempfile.mktemp())
        tmp_out = Path(tempfile.mktemp())
        tmp_in.write_text("\n".join(fzf_lines))
        subprocess.run(["tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "60%", "-h", "50%",
                        f"cat {tmp_in} | fzf --delimiter='\\t' --with-nth=1,2 --header='Select rule to edit' > {tmp_out}"])
        sel = tmp_out.read_text().strip() if tmp_out.exists() else ""
        tmp_in.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)
        if not sel:
            return ctx.stop("No rule selected.")
        rule_name = sel.split('\t')[0]

    if not all(c.isalnum() or c in '-_' for c in rule_name):
        return ctx.stop("❌ Invalid rule name")

    rules_dir.mkdir(parents=True, exist_ok=True)
    rule_file = rules_dir / f"{rule_name}.md"
    if not rule_file.exists():
        rule_file.write_text(f"# {rule_name}\n\nAdd your rule content here. This will be included in CLAUDE.md.\n")

    subprocess.run(["tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "80%", "-h", "80%", f"vim {rule_file}"])
    ctx.message(f"✓ Rule '{rule_name}' saved - use :reload to apply")
    return ctx.stop(f"✓ Rule '{rule_name}' saved\n\nUse :reload to rebuild CLAUDE.md and pick up the new rule.")


@command(':note')
def cmd_note(ctx):
    try:
        open_session_notes(get_runtime_tmux_state_file, session_id=ctx.session_id)
        return ctx.stop("📝 Opening session notes...")
    except Exception as e:
        return ctx.stop(f"❌ Error: {str(e)}")


@command(':todo')
def cmd_todo(ctx):
    current_dir = ctx.cwd
    state_dir = get_state_dir()
    todos_dir = state_dir / "todos"
    todos_dir.mkdir(parents=True, exist_ok=True)
    encoded = current_dir.replace('/', '-').strip('-')
    todos_file = todos_dir / f"{encoded}.json"
    todos = json.loads(todos_file.read_text()) if todos_file.exists() else []

    desc = ctx.args.strip()
    if desc:
        todos.append({"text": desc, "done": False})
        todos_file.write_text(json.dumps(todos, indent=2))
        ctx.message(f"✓ Todo added ({len(todos)} total)")
        return ctx.stop(f"✓ Todo added: {desc}")

    if not todos:
        return ctx.stop(f"No todos for {current_dir}")
    lines = [f"Todos for {current_dir}:"]
    for i, todo in enumerate(todos, 1):
        marker = "x" if todo.get("done") else " "
        lines.append(f"  [{marker}] {i}. {todo['text']}")
    return ctx.stop("\n".join(lines))


@command(':done')
def cmd_done(ctx):
    current_dir = ctx.cwd
    state_dir = get_state_dir()
    encoded = current_dir.replace('/', '-').strip('-')
    todos_file = state_dir / "todos" / f"{encoded}.json"
    if not todos_file.exists():
        return ctx.stop("No todos for this directory.")
    todos = json.loads(todos_file.read_text())
    try:
        num = int(ctx.args.strip())
        if 1 <= num <= len(todos):
            todos[num - 1]["done"] = True
            todos_file.write_text(json.dumps(todos, indent=2))
            return ctx.stop(f"✓ Done: {todos[num - 1]['text']}")
        return ctx.stop(f"❌ Invalid number. Have {len(todos)} todos.")
    except ValueError:
        return ctx.stop("❌ Usage: :done <number>")


# ── Tmux integration commands ────────────────────────────────────────────────

@command(':tmux-unlink')
def cmd_tmux_unlink(ctx):
    if not ctx.session_id:
        return ctx.stop("❌ No session ID")
    state_dir = get_state_dir()
    mf = state_dir / "sessions" / f"{ctx.session_id}.json"
    if mf.exists():
        meta = json.loads(mf.read_text())
        if "linked_tmux_window" in meta:
            del meta["linked_tmux_window"]
            mf.write_text(json.dumps(meta, indent=2))
            return ctx.stop("✓ Unlinked tmux window")
    return ctx.stop("No tmux window linked")


@command(':tmuxpath')
def cmd_tmuxpath(ctx):
    if not ctx.session_id:
        return ctx.stop("❌ No session ID")
    state_dir = get_state_dir()
    mf = state_dir / "sessions" / f"{ctx.session_id}.json"
    meta = json.loads(mf.read_text()) if mf.exists() else {}
    linked = meta.get("linked_tmux_window")
    if not linked:
        return ctx.stop("No tmux window linked. Use :tmux to link a window first.")
    try:
        result = run(["tmux", "-L", "default", "display-message", "-p", "-t", linked, "#{pane_current_path}"],
                     capture_output=True, text=True, check=True)
        path = result.stdout.strip()
        return ctx.stop(f"The linked tmux window ({linked}) is at: {path}" if path else "❌ Could not get path")
    except subprocess.CalledProcessError:
        return ctx.stop(f"❌ Linked window {linked} not found - use :tmux-unlink to reset")


@command(':tmuxscreen')
def cmd_tmuxscreen(ctx):
    if not ctx.session_id:
        return ctx.stop("❌ No session ID")
    state_dir = get_state_dir()
    mf = state_dir / "sessions" / f"{ctx.session_id}.json"
    meta = json.loads(mf.read_text()) if mf.exists() else {}
    linked = meta.get("linked_tmux_window")
    if not linked:
        return ctx.stop("No tmux window linked. Use :tmux to link a window first.")
    try:
        result = run(["tmux", "-L", "default", "capture-pane", "-p", "-t", linked],
                     capture_output=True, text=True, check=True)
        content = result.stdout.rstrip()
        lines = content.split('\n')
        while lines and not lines[0].strip():
            lines.pop(0)
        content = '\n'.join(lines)
        if content:
            ctx.message(f"✓ Captured {len(lines)} lines from window {linked}")
            return ctx.stop(f"Content of linked tmux window ({linked}):\n\n```\n{content}\n```")
        return ctx.stop(f"Linked tmux window ({linked}) is empty.")
    except subprocess.CalledProcessError:
        return ctx.stop(f"❌ Linked window {linked} not found - use :tmux-unlink to reset")


@command(':tmuxs-link')
def cmd_tmuxs_link(ctx):
    if not ctx.session_id:
        return ctx.stop("❌ No session ID")
    try:
        result = run(["tmux", "-L", "default", "display-message", "-p", "#{window_id}:#{window_name}"],
                     capture_output=True, text=True, check=True)
        parts = result.stdout.strip().split(":", 1)
        wid, wname = parts[0], parts[1] if len(parts) > 1 else parts[0]
        state_dir = get_state_dir()
        mf = state_dir / "sessions" / f"{ctx.session_id}.json"
        meta = json.loads(mf.read_text()) if mf.exists() else {}
        linked = meta.get("linked_tmux_windows", [])
        if not any(w["id"] == wid for w in linked):
            linked.append({"id": wid, "name": wname})
            meta["linked_tmux_windows"] = linked
            mf.parent.mkdir(parents=True, exist_ok=True)
            mf.write_text(json.dumps(meta, indent=2))
            return ctx.stop(f"✓ Added tmux window '{wname}' ({wid})")
        return ctx.stop(f"Already linked: '{wname}' ({wid})")
    except subprocess.CalledProcessError:
        return ctx.stop("❌ Could not access default tmux server")


@command(':tmuxs')
def cmd_tmuxs(ctx):
    if not ctx.session_id:
        return ctx.stop("❌ No session ID")
    state_dir = get_state_dir()
    mf = state_dir / "sessions" / f"{ctx.session_id}.json"
    meta = json.loads(mf.read_text()) if mf.exists() else {}
    linked = meta.get("linked_tmux_windows", [])
    if not linked:
        return ctx.stop("No linked windows. Use :tmuxs-link to add windows.")

    uid = os.getuid()
    tmp_in = Path(f"/tmp/kc-tmuxs-{uid}.txt")
    tmp_out = Path(f"/tmp/kc-tmuxs-{uid}-out.txt")
    tmp_in.write_text("\n".join(f"{w['id']}\t{w['name']}" for w in linked))
    tmp_out.unlink(missing_ok=True)

    subprocess.run(["tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "60%", "-h", "40%",
                    f"cat {tmp_in} | fzf --delimiter='\\t' --with-nth=2 --header='Select window' > {tmp_out}"])

    sel = tmp_out.read_text().strip() if tmp_out.exists() else ""
    tmp_in.unlink(missing_ok=True)
    tmp_out.unlink(missing_ok=True)
    if not sel:
        return ctx.stop("Cancelled")

    wid = sel.split("\t")[0]
    try:
        run(["tmux", "-L", "default", "select-window", "-t", wid], capture_output=True, text=True, check=True)
        return ctx.stop(f"✓ Switched to {wid}")
    except subprocess.CalledProcessError:
        return ctx.stop(f"❌ Window {wid} not found")


@command(':tmux')
def cmd_tmux(ctx):
    if not ctx.session_id:
        return ctx.stop("❌ No session ID")
    state_dir = get_state_dir()
    mf = state_dir / "sessions" / f"{ctx.session_id}.json"
    meta = json.loads(mf.read_text()) if mf.exists() else {}
    linked = meta.get("linked_tmux_window")

    if linked:
        try:
            run(["tmux", "-L", "default", "select-window", "-t", linked], capture_output=True, text=True, check=True)
            return ctx.stop(f"✓ Switched to tmux window {linked}")
        except subprocess.CalledProcessError:
            return ctx.stop(f"❌ Linked window {linked} not found - use :tmux-unlink to reset")

    try:
        result = run(["tmux", "-L", "default", "display-message", "-p", "#{window_id}:#{window_name}"],
                     capture_output=True, text=True, check=True)
        parts = result.stdout.strip().split(":", 1)
        wid, wname = parts[0], parts[1] if len(parts) > 1 else parts[0]
        meta["linked_tmux_window"] = wid
        mf.parent.mkdir(parents=True, exist_ok=True)
        mf.write_text(json.dumps(meta, indent=2))
        return ctx.stop(f"✓ Linked to tmux window '{wname}' ({wid})")
    except subprocess.CalledProcessError:
        return ctx.stop("❌ Could not access default tmux server")


# ── kc-skills (double-colon commands) ────────────────────────────────────────

@command('::skills')
def cmd_kc_skills_list(ctx):
    profile = ctx.profile
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    kc_skills_dir = config_dir / "kc-skills"
    if not kc_skills_dir.exists() or not any(kc_skills_dir.iterdir()):
        return ctx.stop("No kitty-claude skills found.\n\nCreate skills with ::skill <name>")
    skills = [f"  ::{f.stem}" for f in sorted(kc_skills_dir.iterdir()) if f.suffix == '.md']
    ctx.message(f"📋 Found {len(skills)} KC skills")
    return ctx.stop("Available kitty-claude skills:\n\n" + "\n".join(skills))


@command('::skill')
def cmd_kc_skill_edit(ctx):
    skill_name = ctx.args.strip()
    if not skill_name:
        return ctx.stop("❌ Usage: ::skill <name>")
    if not all(c.isalnum() or c in '-_' for c in skill_name):
        return ctx.stop("❌ Invalid skill name")

    profile = ctx.profile
    if profile:
        config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "kitty-claude"
    kc_skills_dir = config_dir / "kc-skills"
    kc_skills_dir.mkdir(parents=True, exist_ok=True)
    skill_file = kc_skills_dir / f"{skill_name}.md"
    if not skill_file.exists():
        skill_file.write_text(f"# {skill_name}\n\nAdd your kitty-claude skill content here.\nThis will be injected as context when you run ::{skill_name}\n")

    subprocess.run(["tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "80%", "-h", "80%", f"vim {skill_file}"])
    ctx.message(f"✓ KC skill '{skill_name}' saved")
    return ctx.stop(f"✓ KC skill '{skill_name}' saved")


# ── Import command modules (triggers registration) ───────────────────────────

import kitty_claude.colon_commands.nav_commands  # noqa: F401, E402
import kitty_claude.colon_commands.permission_commands  # noqa: F401, E402
import kitty_claude.colon_commands.mcp_commands  # noqa: F401, E402
import kitty_claude.colon_commands.session_commands  # noqa: F401, E402


# ── Hook Handlers ────────────────────────────────────────────────────────────

def handle_user_prompt_submit(claude_data_dir=None):
    """Handle UserPromptSubmit hook."""
    socket = get_tmux_socket()
    try:
        if claude_data_dir is None:
            config_env = os.environ.get('CLAUDE_CONFIG_DIR')
            claude_data_dir = Path(config_env) if config_env else Path.home() / ".config" / "kitty-claude" / "claude-data"

        input_data = json.loads(sys.stdin.read())
        prompt = input_data.get('prompt', '').strip()

        # Register running session
        session_id = input_data.get('session_id')
        if session_id:
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            cwd = input_data.get('cwd', os.getcwd())
            try:
                claude_pid = None
                result = subprocess.run(["pgrep", "-f", f"claude --resume {session_id}"], capture_output=True, text=True)
                if result.returncode == 0:
                    claude_pid = int(result.stdout.strip().split('\n')[0])
                if not claude_pid:
                    sock = os.environ.get('KITTY_CLAUDE_TMUX_SOCKET')
                    if sock:
                        result = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "#{pane_pid}"], capture_output=True, text=True)
                        if result.returncode == 0:
                            pane_pid = int(result.stdout.strip())
                            result = subprocess.run(["pgrep", "-P", str(pane_pid), "claude"], capture_output=True, text=True)
                            if result.returncode == 0:
                                claude_pid = int(result.stdout.strip().split('\n')[0])
                if claude_pid:
                    from kitty_claude.claude import register_running_session
                    register_running_session(session_id, claude_pid, cwd, profile)
            except Exception:
                pass

        # Try registered commands
        if prompt.startswith(':') or prompt.startswith('::'):
            ctx = CommandContext(prompt=prompt, input_data=input_data, socket=socket, claude_data_dir=claude_data_dir)
            result = dispatch(prompt, ctx)
            if result is not None:
                print(json.dumps(result))
                return

        # Handle :: skill invocation (catch-all for unregistered :: prefixes)
        if prompt.startswith('::') and not prompt.startswith('::skill ') and not prompt.startswith('::skills'):
            rest = prompt[2:]
            parts = rest.split(None, 1)
            skill_name = parts[0] if parts else ""
            rest_of_prompt = parts[1] if len(parts) > 1 else ""
            if skill_name:
                profile = os.environ.get('KITTY_CLAUDE_PROFILE')
                if profile:
                    config_dir = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
                else:
                    config_dir = Path.home() / ".config" / "kitty-claude"
                skill_file = config_dir / "kc-skills" / f"{skill_name}.md"
                if skill_file.exists():
                    skill_content = skill_file.read_text().strip()
                    send_tmux_message(f"📖 Loading KC skill '{skill_name}'...", socket)
                    if rest_of_prompt:
                        print(f"{rest_of_prompt}\n\n[Kitty-Claude Skill: {skill_name}]\n{skill_content}")
                    else:
                        print(f"[Kitty-Claude Skill: {skill_name}]\n{skill_content}")
                    return
                else:
                    send_tmux_message(f"❌ KC skill '{skill_name}' not found", socket)
                    print(json.dumps({"continue": False, "stopReason": f"❌ KC skill '{skill_name}' not found. Create it with ::skill {skill_name}"}))
                    return

        # Plugin dispatch: :foo -> kitty-claude-foo on PATH
        if prompt.startswith(':'):
            parts = prompt[1:].split(None, 1)
            cmd_name = parts[0] if parts else ""
            cmd_args = parts[1] if len(parts) > 1 else ""
            plugin_bin = shutil.which(f"kitty-claude-{cmd_name}")
            if plugin_bin:
                import tempfile
                env_exports = []
                if session_id:
                    env_exports.append(f"KITTY_CLAUDE_SESSION_ID={session_id}")
                env_exports.append(f"KITTY_CLAUDE_SOCKET={socket}")
                env_exports.append(f"KITTY_CLAUDE_CWD={input_data.get('cwd', os.getcwd())}")
                env_str = " ".join(env_exports)
                tmp_output = Path(tempfile.mktemp())
                plugin_cmd = f"{plugin_bin}"
                if cmd_args:
                    plugin_cmd += f" {cmd_args}"
                subprocess.run(["tmux", "-L", socket, "display-popup", "-E", "-w", "60%", "-h", "50%",
                                f"{env_str} {plugin_cmd} > {tmp_output}"])
                output = tmp_output.read_text().strip() if tmp_output.exists() else ""
                tmp_output.unlink(missing_ok=True)
                if output.startswith(':'):
                    print(output)
                elif output:
                    print(json.dumps({"continue": False, "stopReason": output}))
                else:
                    print(json.dumps({"continue": False, "stopReason": f"✓ {cmd_name}"}))
                return

        # Not a command - save timing and pass through
        if session_id:
            save_request_start_time(session_id)
        print(prompt)

    except Exception as e:
        import traceback
        error_msg = f"Hook error: {str(e)}"
        tb = traceback.format_exc()
        send_tmux_message(f"❌ {error_msg}", socket)
        profile = os.environ.get('KITTY_CLAUDE_PROFILE')
        log(f"COLON COMMAND ERROR: {error_msg}\n{tb}", profile)
        try:
            input_data = json.loads(sys.stdin.read()) if 'input_data' not in locals() else input_data
            print(input_data.get('prompt', ''))
        except:
            pass


def handle_session_start():
    """Handle SessionStart hook."""
    try:
        input_data = json.loads(sys.stdin.read())
        session_id = input_data.get('session_id')
        if not session_id:
            print(json.dumps({"continue": True}))
            return

        profile = os.environ.get('KITTY_CLAUDE_PROFILE')
        if profile:
            base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
        else:
            base_config = Path.home() / ".config" / "kitty-claude"

        session_dir = base_config / "session-configs" / session_id
        run_file = session_dir / ".run-counter"
        messages_file = session_dir / ".startup-messages"

        current_run = 0
        if run_file.exists():
            try:
                current_run = int(run_file.read_text().strip())
            except (ValueError, OSError):
                pass
        current_run += 1
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            run_file.write_text(str(current_run))
        except OSError:
            pass

        messages_to_show = []
        if messages_file.exists():
            try:
                all_messages = json.loads(messages_file.read_text())
                for msg in all_messages:
                    if msg.get("run") == current_run - 1:
                        messages_to_show.append(msg.get("text", ""))
                messages_file.unlink()
            except (json.JSONDecodeError, OSError):
                pass

        if messages_to_show:
            context = "\n".join(messages_to_show)
            sock = os.environ.get('KITTY_CLAUDE_TMUX_SOCKET')
            if sock:
                uid = os.getuid()
                msg_file = Path(f"/tmp/kc-popup-{uid}.txt")
                script_file = Path(f"/tmp/kc-popup-{uid}.sh")
                msg_file.write_text("\n".join(messages_to_show))
                script_file.write_text(f'#!/bin/bash\ncat {msg_file}\necho ""\necho "[press Enter to close, or wait 30s]"\nread -t 30\n')
                script_file.chmod(0o755)
                subprocess.Popen(["tmux", "-L", sock, "display-popup", "-w", "70",
                                  "-h", str(len(messages_to_show) + 5), "-E", str(script_file)],
                                 stderr=subprocess.DEVNULL)
            print(json.dumps({"continue": True, "additionalContext": context}))
        else:
            print(json.dumps({"continue": True}))
    except Exception as e:
        with open("/tmp/kitty-claude-session-start-error.log", "a") as f:
            f.write(f"SessionStart hook error: {str(e)}\n")
        print(json.dumps({"continue": True}))


def handle_stop():
    """Handle Stop hook."""
    try:
        input_data = json.loads(sys.stdin.read())
        session_id = input_data.get('session_id')
        if session_id:
            save_response_duration(session_id)
            profile = os.environ.get('KITTY_CLAUDE_PROFILE')
            remove_open_session(session_id, profile)

        sock = os.environ.get('KITTY_CLAUDE_TMUX_SOCKET')
        if sock:
            uid = os.getuid()
            queue_file = Path(f"/run/user/{uid}/kc-queue-{sock}.txt")
            if queue_file.exists():
                try:
                    lines = queue_file.read_text().splitlines()
                    if lines:
                        cmd = lines[0]
                        remaining = lines[1:]
                        if remaining:
                            queue_file.write_text("\n".join(remaining) + "\n")
                        else:
                            queue_file.unlink()
                        time.sleep(1)
                        subprocess.run(["tmux", "-L", sock, "send-keys", "-l", cmd], capture_output=True, timeout=5)
                        time.sleep(0.3)
                        subprocess.run(["tmux", "-L", sock, "send-keys", "Enter"], capture_output=True, timeout=5)
                except Exception:
                    pass
    except Exception as e:
        with open("/tmp/kitty-claude-stop-hook-error.log", "a") as f:
            f.write(f"Stop hook error: {str(e)}\n")


def handle_pre_tool_use():
    """Handle PreToolUse hook - deny expired timed permissions."""
    import fnmatch
    try:
        input_data = json.loads(sys.stdin.read())
        tool_name = input_data.get('tool_name', '')
        tool_input = input_data.get('tool_input', {})

        if tool_name == 'Bash':
            tool_string = f"Bash({tool_input.get('command', '')})"
        elif tool_name.startswith('mcp__'):
            tool_string = tool_name
        else:
            tool_string = tool_name

        timed_perms = load_timed_permissions()
        now = time.time()
        for perm in timed_perms:
            pattern = perm.get('pattern', '')
            expires = perm.get('expires', 0)
            if pattern.endswith(':*)'):
                prefix = pattern[:-2]
                if tool_string.startswith(prefix):
                    if now > expires:
                        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                              "permissionDecision": "deny",
                              "permissionDecisionReason": f"Timed permission expired: {pattern}"}}))
                    return
            elif fnmatch.fnmatch(tool_string, pattern) or tool_string == pattern:
                if now > expires:
                    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "deny",
                          "permissionDecisionReason": f"Timed permission expired: {pattern}"}}))
                return
    except Exception as e:
        with open("/tmp/kitty-claude-pre-tool-use-error.log", "a") as f:
            f.write(f"PreToolUse hook error: {str(e)}\n")


def handle_run_command(command):
    """Handle --run-command."""
    import io
    config_dir = os.environ.get('CLAUDE_CONFIG_DIR', '')
    session_id = Path(config_dir).name if config_dir else None
    input_data = {"session_id": session_id, "cwd": os.getcwd(), "prompt": command}

    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(input_data))
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        handle_user_prompt_submit()
    except SystemExit:
        pass
    output = sys.stdout.getvalue()
    sys.stdin = old_stdin
    sys.stdout = old_stdout

    for line in output.strip().split('\n'):
        if not line:
            continue
        try:
            result = json.loads(line)
            print(json.dumps(result))
            return
        except (json.JSONDecodeError, ValueError):
            pass
    print(output)