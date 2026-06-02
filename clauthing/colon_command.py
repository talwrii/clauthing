#!/usr/bin/env python3
"""Colon command handlers for clauthing (:cd, :fork, :time, etc).

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

from clauthing.logging import log, run
from clauthing.claude_utils import encode_project_path
from clauthing.colon_commands.time import (
    save_request_start_time,
    save_response_duration,
    get_last_response_duration
)
from clauthing.session import (
    get_session_name,
    save_session_metadata,
    remove_open_session
)
from clauthing.session_utils import session_has_messages
from clauthing.window_utils import open_session_notes
from clauthing.tmux import get_runtime_tmux_state_file
from clauthing.rules import build_claude_md
from clauthing import linked_tmux

import time


# ── Utilities (used by command modules too) ──────────────────────────────────

def get_tmux_socket():
    """Get the tmux socket name from environment or default."""
    socket = os.environ.get('CLAUTHING_TMUX_SOCKET')
    if socket:
        return socket
    tmux_var = os.environ.get('TMUX', '')
    if tmux_var:
        socket_path = tmux_var.split(',')[0]
        socket_name = os.path.basename(socket_path)
        if socket_name:
            return socket_name
    return 'clauthing'


def send_tmux_message(message, socket=None):
    """Send a message via tmux display-message."""
    if socket is None:
        socket = get_tmux_socket()
    try:
        run(["tmux", "-L", socket, "display-message", message], stderr=subprocess.DEVNULL)
    except:
        pass


def get_state_dir():
    """Get the XDG state directory for clauthing."""
    xdg_state = os.environ.get('XDG_STATE_HOME')
    if xdg_state:
        state_dir = Path(xdg_state) / "clauthing"
    else:
        state_dir = Path.home() / ".local" / "state" / "clauthing"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def get_title_history_file(profile=None):
    if profile is None:
        profile = os.environ.get('CLAUTHING_PROFILE')
    if profile:
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"
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
        base_config = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "clauthing"
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
    config_dir = Path.home() / ".config" / "clauthing"
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
# Commands marked `independent=True` can run via `clauthing --run-independent`
# (M-; popup) without going through claude's hook pipeline. They must not
# rely on stdin's hook payload or modify claude's running state.
INDEPENDENT_COMMANDS = set()


def command(prefix, *, independent=False):
    """Register a colon command handler.

    Args:
        prefix: The :prefix string (e.g. ":msg").
        independent: When True, the command is also exposed to the M-; popup
            and can run without round-tripping through claude. Use for
            commands that only read/write external state or other tmux
            windows — NOT for ones that touch the current claude session.
    """
    def decorator(fn):
        COMMANDS[prefix] = fn
        if independent:
            INDEPENDENT_COMMANDS.add(prefix)
        return fn
    return decorator


def dispatch(prompt, ctx):
    """Dispatch to registered handler. Returns result dict or None."""
    for prefix in sorted(COMMANDS.keys(), key=len, reverse=True):
        if prompt == prefix or prompt.startswith(prefix + ' '):
            return COMMANDS[prefix](ctx)
    return None


def run_independent(prompt):
    """Run an independent colon command outside the hook pipeline.

    Used by `clauthing --run-independent` (M-; popup). Constructs a minimal
    CommandContext: pulls session_id from the current tmux pane's
    `@session_id`, cwd from the pane's working directory, socket from env.

    Returns (ok, message). Refuses commands not marked `independent=True`.
    """
    import subprocess as _sp
    prompt = (prompt or "").strip()
    # Strip any leading colons so the user can type either `foo` or `:foo`
    # in the popup, then prepend exactly one.
    prompt = ":" + prompt.lstrip(":").strip()
    if prompt == ":":
        return False, "Empty command"

    matched = None
    for prefix in sorted(COMMANDS.keys(), key=len, reverse=True):
        if prompt == prefix or prompt.startswith(prefix + ' '):
            matched = prefix
            break
    if matched is None:
        head = prompt.split()[0] if prompt.split() else prompt
        return False, f"Unknown colon command: {head}"
    if matched not in INDEPENDENT_COMMANDS:
        return False, f"{matched} is not marked independent — won't run outside claude"

    socket = os.environ.get("CLAUTHING_TMUX_SOCKET", "clauthing")
    session_id = None
    clauthing_window = None
    cwd = os.getcwd()
    try:
        r = _sp.run(
            ["tmux", "-L", socket, "display-message", "-p",
             "#{@session_id}\t#{@clauthing_window}\t#{pane_current_path}"],
            capture_output=True, text=True, timeout=2,
        )
        parts = r.stdout.strip().split("\t", 2)
        if len(parts) > 0 and parts[0]:
            session_id = parts[0]
        if len(parts) > 1 and parts[1]:
            clauthing_window = parts[1]
        if len(parts) > 2 and parts[2]:
            cwd = parts[2]
    except Exception:
        pass

    input_data = {"session_id": session_id, "clauthing_window": clauthing_window, "cwd": cwd}
    claude_data_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    ctx = CommandContext(prompt, input_data, socket, claude_data_dir)
    try:
        result = COMMANDS[matched](ctx)
    except Exception as e:
        import traceback as _tb
        return False, f"❌ {matched} raised: {e}\n{_tb.format_exc()}"
    if isinstance(result, dict):
        return True, result.get("stopReason", "")
    return True, str(result or "")


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
    def clauthing_window(self):
        """The stable window id (survives :cd, unlike session_id).

        The WINDOW owns the id, so the live tmux @clauthing_window option is
        authoritative — it's what the status bar / inbox keys use. Resolution:
        input_data (already read off the pane) → live tmux option → session
        metadata (last resort, e.g. if the option can't be read).
        """
        cw = self.input_data.get('clauthing_window')
        if cw:
            return cw
        cw = self._read_tmux_clauthing_window()
        if cw:
            return cw
        sid = self.session_id
        if sid:
            from clauthing.session import get_clauthing_window
            return get_clauthing_window(sid)
        return None

    def _read_tmux_clauthing_window(self):
        pane = os.environ.get('TMUX_PANE')
        target = ['-t', pane] if pane else []
        try:
            r = subprocess.run(
                ['tmux', '-L', self.socket, 'display-message', '-p',
                 *target, '#{@clauthing_window}'],
                capture_output=True, text=True, timeout=2,
            )
            return r.stdout.strip() or None
        except Exception:
            return None

    @property
    def cwd(self):
        return self.input_data.get('cwd', os.getcwd())

    @property
    def profile(self):
        return os.environ.get('CLAUTHING_PROFILE')

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
    help_text = """clauthing colon commands:
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
::skills             List all clauthing skills
::skill <name>       Create/edit a clauthing skill
::<skill> [prompt]   Run clauthing skill (injects context)
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
:login-all           Send :login to all cl1-* instances
:reload-all          Send :reload to all cl1-* instances
:send <message>      Send a message to another clauthing window
:current-sessions    List all currently running sessions
:sessions [N]        List recent sessions (default 10)
:resume <num|id>     Resume a session in new window
:resume-new [num|id] Resume in a new clauthing window
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
                if entry.name.startswith("clauthing-") and os.access(entry, os.X_OK):
                    plugins.add(entry.name[len("clauthing-"):])
        except (OSError, PermissionError):
            pass
    if plugins:
        help_text += "\nPlugins (from PATH):\n"
        for name in sorted(plugins):
            help_text += f"  :{name:<20s} (clauthing-{name})\n"

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
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"
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
        base = Path.home() / ".config" / "clauthing" / "other-profiles" / profile / "claude-data" / "skills"
    else:
        base = Path.home() / ".config" / "clauthing" / "claude-data" / "skills"

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
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"
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
    encoded = encode_project_path(current_dir)
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
    encoded = encode_project_path(current_dir)
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
    return ctx.stop(linked_tmux.unlink(ctx.clauthing_window, ctx.profile)[1])


@command(':tmuxpath-current')
def cmd_tmuxpath_current(ctx):
    """cwd of the focused window on the user's default tmux server."""
    return ctx.stop(linked_tmux.current_default_path()[1])


@command(':tmuxpath')
def cmd_tmuxpath(ctx):
    return ctx.stop(linked_tmux.linked_path(ctx.clauthing_window, ctx.profile)[1])


@command(':tmuxscreen')
def cmd_tmuxscreen(ctx):
    return ctx.stop(linked_tmux.linked_screen(ctx.clauthing_window, ctx.profile)[1])


@command(':tmuxs-link')
def cmd_tmuxs_link(ctx):
    return ctx.stop(linked_tmux.list_add(ctx.clauthing_window, ctx.profile)[1])


@command(':tmuxs')
def cmd_tmuxs(ctx):
    linked = linked_tmux.get_linked_list(ctx.clauthing_window, ctx.profile)
    if not linked:
        return ctx.stop("No linked windows. Use :tmuxs-link to add windows.")

    uid = os.getuid()
    tmp_in = Path(f"/tmp/cl-tmuxs-{uid}.txt")
    tmp_out = Path(f"/tmp/cl-tmuxs-{uid}-out.txt")
    tmp_in.write_text("\n".join(f"{w['id']}\t{w['name']}" for w in linked))
    tmp_out.unlink(missing_ok=True)

    subprocess.run(["tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "60%", "-h", "40%",
                    f"cat {tmp_in} | fzf --delimiter='\\t' --with-nth=2 --header='Select window' > {tmp_out}"])

    sel = tmp_out.read_text().strip() if tmp_out.exists() else ""
    tmp_in.unlink(missing_ok=True)
    tmp_out.unlink(missing_ok=True)
    if not sel:
        return ctx.stop("Cancelled")

    return ctx.stop(linked_tmux.select_window(sel.split("\t")[0])[1])


@command(':pattern-approve')
def cmd_pattern_approve(ctx):
    """Toggle pattern-approve PreToolUse hook for this session.

    Usage:
        :pattern-approve            toggle on/off
        :pattern-approve on         enable
        :pattern-approve off        disable

    The setting lives in session metadata so it survives :reload. Run
    :reload after toggling for the change to take effect (claude reads
    settings.json at startup, so the new hook needs a fresh claude)."""
    if not ctx.session_id:
        return ctx.stop("❌ No session ID")
    state_dir = get_state_dir()
    mf = state_dir / "sessions" / f"{ctx.session_id}.json"
    meta = json.loads(mf.read_text()) if mf.exists() else {}
    arg = ctx.args.strip().lower()
    if arg in ("on", "enable", "true", "1"):
        new = True
    elif arg in ("off", "disable", "false", "0"):
        new = False
    elif arg == "":
        new = not bool(meta.get("pattern_approve"))
    else:
        return ctx.stop("Usage: :pattern-approve [on|off]")
    meta["pattern_approve"] = new
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text(json.dumps(meta, indent=2))
    state = "on" if new else "off"
    return ctx.stop(f"✓ pattern-approve {state} (run :reload to apply)")


@command(':tmux-spawn')
def cmd_tmux_spawn(ctx):
    """Spawn a fresh tmux window in the user's default server, linked to this
    clauthing window. Refuses if a window is already linked (use :tmux-unlink).

    Optional arg: a shell command to run (e.g. `:tmux-spawn vim README.md`)."""
    name_hint = (get_session_name(ctx.session_id) if ctx.session_id else "") \
        or (ctx.session_id[:8] if ctx.session_id else "")
    return ctx.stop(linked_tmux.spawn(
        ctx.clauthing_window, ctx.profile, name_hint, ctx.args.strip())[1])


@command(':tmux-kill')
def cmd_tmux_kill(ctx):
    """Kill the linked default-server window and clear the link."""
    return ctx.stop(linked_tmux.kill(ctx.clauthing_window, ctx.profile)[1])


@command(':tmux')
def cmd_tmux(ctx):
    return ctx.stop(linked_tmux.toggle(ctx.clauthing_window, ctx.profile)[1])


# ── cl-skills (double-colon commands) ────────────────────────────────────────

@command('::skills')
def cmd_kc_skills_list(ctx):
    profile = ctx.profile
    if profile:
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"
    kc_skills_dir = config_dir / "cl-skills"
    if not kc_skills_dir.exists() or not any(kc_skills_dir.iterdir()):
        return ctx.stop("No clauthing skills found.\n\nCreate skills with ::skill <name>")
    skills = [f"  ::{f.stem}" for f in sorted(kc_skills_dir.iterdir()) if f.suffix == '.md']
    ctx.message(f"📋 Found {len(skills)} KC skills")
    return ctx.stop("Available clauthing skills:\n\n" + "\n".join(skills))


@command('::skill')
def cmd_kc_skill_edit(ctx):
    skill_name = ctx.args.strip()
    if not skill_name:
        return ctx.stop("❌ Usage: ::skill <name>")
    if not all(c.isalnum() or c in '-_' for c in skill_name):
        return ctx.stop("❌ Invalid skill name")

    profile = ctx.profile
    if profile:
        config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        config_dir = Path.home() / ".config" / "clauthing"
    kc_skills_dir = config_dir / "cl-skills"
    kc_skills_dir.mkdir(parents=True, exist_ok=True)
    skill_file = kc_skills_dir / f"{skill_name}.md"
    if not skill_file.exists():
        skill_file.write_text(f"# {skill_name}\n\nAdd your clauthing skill content here.\nThis will be injected as context when you run ::{skill_name}\n")

    subprocess.run(["tmux", "-L", ctx.socket, "display-popup", "-E", "-w", "80%", "-h", "80%", f"vim {skill_file}"])
    ctx.message(f"✓ KC skill '{skill_name}' saved")
    return ctx.stop(f"✓ KC skill '{skill_name}' saved")


# ── Import command modules (triggers registration) ───────────────────────────

import clauthing.colon_commands.nav_commands  # noqa: F401, E402
import clauthing.colon_commands.permission_commands  # noqa: F401, E402
import clauthing.colon_commands.mcp_commands  # noqa: F401, E402
import clauthing.colon_commands.session_commands  # noqa: F401, E402

