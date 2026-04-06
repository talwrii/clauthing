"""Permissions and roles colon commands.

Commands: :permissions, :disallow, :allow-for, :allow-last, :allow-recent,
          :permissions-gui, :roles, :role, :role-add, :role-add-all,
          :role-add-mcp, :roles-current, :title-role
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from kitty_claude.colon_command import command, send_tmux_message, get_state_dir
from kitty_claude.colon_command import (
    load_timed_permissions, save_timed_permissions,
    parse_duration, format_remaining_time
)
from kitty_claude.logging import log, run


# ── Shared helpers ───────────────────────────────────────────────────────────

def get_config_dir(profile):
    if profile:
        return Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    return Path.home() / ".config" / "kitty-claude"


def get_roles_dir(profile):
    return get_config_dir(profile) / "mcp-roles"


def gather_permissions(claude_data_dir, cwd):
    """Gather all permissions from session settings + project settings.
    Returns list of (rule, source_label, source_file_path).
    """
    rules = []

    settings_file = claude_data_dir / "settings.json"
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
            for rule in settings.get("permissions", {}).get("allow", []):
                rules.append((rule, "session", str(settings_file)))
        except (json.JSONDecodeError, OSError):
            pass

    project_settings = Path(cwd) / ".claude" / "settings.local.json"
    if project_settings.exists():
        try:
            proj = json.loads(project_settings.read_text())
            for rule in proj.get("permissions", {}).get("allow", []):
                rules.append((rule, "project", str(project_settings)))
        except (json.JSONDecodeError, OSError):
            pass

    # Deduplicate preserving order
    seen = set()
    unique = []
    for rule, label, source in rules:
        if rule not in seen:
            seen.add(rule)
            unique.append((rule, label, source))
    return unique


def get_active_role_permissions(session_id, profile):
    """Get a dict mapping rule -> list of role names that contain it."""
    roles_dir = get_roles_dir(profile)
    rule_in_roles = {}

    if not session_id:
        return rule_in_roles

    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"
    metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    active_roles = metadata.get("activeRoles", [])

    for role_name in active_roles:
        role_file = roles_dir / f"{role_name}.json"
        if role_file.exists():
            try:
                role = json.loads(role_file.read_text())
                for rule in role.get("permissions", {}).get("allow", []):
                    rule_in_roles.setdefault(rule, []).append(role_name)
            except (json.JSONDecodeError, OSError):
                pass

    return rule_in_roles


def find_last_tool_in_session(session_file):
    """Find the last tool_use in a session file. Returns (tool_name, tool_input) or None."""
    last_tool = None
    try:
        with open(session_file, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    content = entry.get('message', {}).get('content', [])
                    if isinstance(content, list):
                        for item in content:
                            if item.get('type') == 'tool_use':
                                last_tool = item
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return last_tool


def tool_to_pattern(tool):
    """Convert a tool_use entry to a permission pattern string."""
    tool_name = tool.get('name', '')
    tool_input = tool.get('input', {})
    if tool_name == 'Bash':
        cmd = tool_input.get('command', '')
        base_cmd = cmd.split()[0] if cmd else ''
        return f"Bash({base_cmd}:*)"
    elif tool_name.startswith('mcp__'):
        return tool_name
    return tool_name


def find_session_file(claude_data_dir, cwd, session_id):
    """Find the session JSONL file."""
    path_hash = cwd.replace('/', '-')
    return claude_data_dir.parent / "projects" / path_hash / f"{session_id}.jsonl"


# ── Commands ─────────────────────────────────────────────────────────────────

@command(':permissions')
def cmd_permissions(ctx):
    rules = gather_permissions(ctx.claude_data_dir, ctx.cwd)
    rule_in_roles = get_active_role_permissions(ctx.session_id, ctx.profile)

    now = time.time()
    timed_perms = load_timed_permissions()
    timed_lookup = {}
    for perm in timed_perms:
        pattern = perm.get('pattern', '')
        remaining = perm.get('expires', 0) - now
        timed_lookup[pattern] = format_remaining_time(remaining) if remaining > 0 else "expired"

    if not rules:
        return ctx.stop("No allowed commands configured.")

    lines = "Allowed commands in this session:\n\n"
    current_label = None
    for i, (rule, label, _source) in enumerate(rules, 1):
        if label != current_label:
            current_label = label
            lines += f"  [{label}]\n"
        tags = []
        if rule in rule_in_roles:
            tags.append(", ".join(rule_in_roles[rule]))
        if rule in timed_lookup:
            tags.append(f"⏱ {timed_lookup[rule]}")
        tags_str = f"  [{', '.join(tags)}]" if tags else ""
        lines += f"  {i:3d}. {rule}{tags_str}\n"

    lines += "\nUse :disallow <num> [num2 ...] to remove permission(s)."
    return ctx.stop(lines)


@command(':disallow')
def cmd_disallow(ctx):
    arg = ctx.args.strip()
    if not arg:
        return ctx.stop("Usage: :disallow <num> [num2 num3 ...]\nRun :permissions to see numbered list.")

    parts = arg.split()
    for p in parts:
        if not p.isdigit():
            return ctx.stop(f"Invalid number: {p}\nUsage: :disallow <num> [num2 num3 ...]")

    target_nums = [int(p) for p in parts]
    rules = gather_permissions(ctx.claude_data_dir, ctx.cwd)

    for num in target_nums:
        if num < 1 or num > len(rules):
            return ctx.stop(f"Invalid number {num}. Run :permissions to see valid range (1-{len(rules)}).")

    removed = []
    errors = []
    for num in sorted(set(target_nums), reverse=True):
        rule, _label, source_file = rules[num - 1]
        try:
            source_path = Path(source_file)
            data = json.loads(source_path.read_text())
            allow_list = data.get("permissions", {}).get("allow", [])
            if rule in allow_list:
                allow_list.remove(rule)
                source_path.write_text(json.dumps(data, indent=2))
                removed.append(rule)
        except Exception as e:
            errors.append(f"Error removing {rule[:30]}: {e}")

    msgs = []
    if removed:
        if len(removed) == 1:
            msgs.append(f"Removed: {removed[0]}")
        else:
            msgs.append(f"Removed {len(removed)} permissions:")
            for r in removed:
                msgs.append(f"  - {r[:60]}")
        ctx.message(f"Removed {len(removed)} permission(s)")
    if errors:
        msgs.append("Errors:")
        msgs.extend(f"  - {e}" for e in errors)
    return ctx.stop("\n".join(msgs) if msgs else "Nothing removed.")


@command(':allow-for')
def cmd_allow_for(ctx):
    arg = ctx.args.strip()
    parts = arg.split(None, 1)
    if len(parts) < 2:
        return ctx.stop("Usage: :allow-for <duration> <pattern|num>\nExamples:\n  :allow-for 1h Bash(npm:*)\n  :allow-for 1h 5")

    duration_str, pattern_or_num = parts
    duration_secs = parse_duration(duration_str)
    if duration_secs is None:
        return ctx.stop(f"Invalid duration: {duration_str}\nUse format like: 1h, 30m, 2h30m, 90s")

    if pattern_or_num.isdigit():
        rules = gather_permissions(ctx.claude_data_dir, ctx.cwd)
        num = int(pattern_or_num)
        if num < 1 or num > len(rules):
            return ctx.stop(f"Invalid number {num}. Run :permissions to see valid range (1-{len(rules)}).")
        pattern = rules[num - 1][0]
    else:
        pattern = pattern_or_num

    expires_at = time.time() + duration_secs

    timed_perms = load_timed_permissions()
    timed_perms = [p for p in timed_perms if p.get('pattern') != pattern]
    timed_perms.append({'pattern': pattern, 'expires': expires_at, 'created': time.time()})
    save_timed_permissions(timed_perms)

    # Also add to Claude's settings
    settings_file = ctx.claude_data_dir / "settings.json"
    try:
        settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
        allow = settings.setdefault('permissions', {}).setdefault('allow', [])
        if pattern not in allow:
            allow.append(pattern)
        settings_file.write_text(json.dumps(settings, indent=2))
    except Exception as e:
        return ctx.stop(f"Error updating settings: {e}")

    readable = format_remaining_time(duration_secs)
    ctx.message(f"Timed {pattern[:30]}... for {readable}")
    return ctx.stop(f"Allowed for {readable}: {pattern}\n\nThis permission will be denied after {readable}.")


@command(':allow-last')
def cmd_allow_last(ctx):
    if not ctx.session_id:
        return ctx.stop("No session ID available")

    profile = ctx.profile
    if profile:
        base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "kitty-claude"

    path_hash = ctx.cwd.replace('/', '-')
    session_file = base_config / "claude-data" / "projects" / path_hash / f"{ctx.session_id}.jsonl"
    if not session_file.exists():
        return ctx.stop(f"Session log not found: {session_file}")

    last_tool = find_last_tool_in_session(session_file)
    if not last_tool:
        return ctx.stop("No tool use found in session")

    pattern = tool_to_pattern(last_tool)

    settings_file = ctx.claude_data_dir / "settings.json"
    try:
        settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
        allow = settings.setdefault('permissions', {}).setdefault('allow', [])
        if pattern not in allow:
            allow.append(pattern)
            settings_file.write_text(json.dumps(settings, indent=2))
            ctx.message(f"✓ Allowed: {pattern[:50]}")
            return ctx.stop(f"✓ Allowed: {pattern}")
        else:
            return ctx.stop(f"Already allowed: {pattern}")
    except Exception as e:
        return ctx.stop(f"Error updating settings: {e}")


@command(':allow-recent')
def cmd_allow_recent(ctx):
    if not ctx.session_id:
        return ctx.stop("No session ID available")

    profile = ctx.profile
    if profile:
        base_config = Path.home() / ".config" / "kitty-claude" / "other-profiles" / profile
    else:
        base_config = Path.home() / ".config" / "kitty-claude"

    path_hash = ctx.cwd.replace('/', '-')
    session_file = base_config / "claude-data" / "projects" / path_hash / f"{ctx.session_id}.jsonl"
    if not session_file.exists():
        return ctx.stop(f"Session log not found: {session_file}")

    # Collect recent tools
    tools_seen = []
    try:
        with open(session_file, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    content = entry.get('message', {}).get('content', [])
                    if isinstance(content, list):
                        for item in content:
                            if item.get('type') == 'tool_use':
                                pattern = tool_to_pattern(item)
                                tool_input = item.get('input', {})
                                if item.get('name') == 'Bash':
                                    display = f"{pattern}  # {tool_input.get('command', '')[:60]}"
                                else:
                                    display = pattern
                                tools_seen = [(p, d) for p, d in tools_seen if p != pattern]
                                tools_seen.append((pattern, display))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        return ctx.stop(f"Error reading session log: {e}")

    if not tools_seen:
        return ctx.stop("No tool use found in session")

    recent_tools = list(reversed(tools_seen[-20:]))

    # fzf picker
    fzf_lines = [f"{i}\t{p}\t{d}" for i, (p, d) in enumerate(recent_tools)]
    tmp_input = Path(tempfile.mktemp())
    tmp_output = Path(tempfile.mktemp())
    tmp_input.write_text("\n".join(fzf_lines))
    tmp_output.unlink(missing_ok=True)

    try:
        subprocess.run([
            "tmux", "-L", ctx.socket,
            "display-popup", "-E", "-w", "80%", "-h", "50%",
            f"cat {tmp_input} | fzf --delimiter='\\t' --with-nth=3 --header='Select tool to allow' > {tmp_output}"
        ])
        if not tmp_output.exists() or not tmp_output.read_text().strip():
            return ctx.stop("Selection cancelled")

        selected = tmp_output.read_text().strip()
        parts = selected.split('\t')
        pattern = parts[1] if len(parts) >= 2 else None
    except Exception as e:
        return ctx.stop(f"Error running fzf: {e}")
    finally:
        tmp_input.unlink(missing_ok=True)
        tmp_output.unlink(missing_ok=True)

    if not pattern:
        return ctx.stop("Could not parse selection")

    settings_file = ctx.claude_data_dir / "settings.json"
    try:
        settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
        allow = settings.setdefault('permissions', {}).setdefault('allow', [])
        if pattern not in allow:
            allow.append(pattern)
            settings_file.write_text(json.dumps(settings, indent=2))
            ctx.message(f"✓ Allowed: {pattern[:50]}")
            return ctx.stop(f"✓ Allowed: {pattern}")
        else:
            return ctx.stop(f"Already allowed: {pattern}")
    except Exception as e:
        return ctx.stop(f"Error updating settings: {e}")


@command(':permissions-gui')
def cmd_permissions_gui(ctx):
    if not ctx.session_id:
        return ctx.stop("No session ID.")
    kitty_claude_path = shutil.which("kitty-claude") or "kitty-claude"
    subprocess.Popen([kitty_claude_path, "--permissions-gui", ctx.session_id])
    ctx.message("Opening permissions editor...")
    return ctx.stop("")


@command(':roles')
def cmd_roles(ctx):
    roles_dir = get_roles_dir(ctx.profile)
    if not roles_dir.exists() or not any(roles_dir.glob("*.json")):
        return ctx.stop("No roles found. Use :role-add <name> <num> to create one.")

    lines = []
    for role_file in sorted(roles_dir.glob("*.json")):
        try:
            role = json.loads(role_file.read_text())
            servers = list(role.get("mcpServers", {}).keys())
            perms = role.get("permissions", {}).get("allow", [])
            parts = []
            if servers:
                parts.append(f"servers: {', '.join(servers)}")
            if perms:
                parts.append(f"{len(perms)} permissions")
            desc = "; ".join(parts) if parts else "(empty)"
            lines.append(f"  {role_file.stem}: {desc}")
        except:
            lines.append(f"  {role_file.stem}: (error reading)")

    ctx.message(f"📋 {len(lines)} roles")
    return ctx.stop("Roles:\n" + "\n".join(lines))


@command(':role-add-all')
def cmd_role_add_all(ctx):
    role_name = ctx.args.strip()
    if not role_name:
        return ctx.stop("Usage: :role-add-all <role-name>")
    if not all(c.isalnum() or c in '-_' for c in role_name):
        return ctx.stop("Role name can only contain letters, numbers, dash, underscore.")

    rules = gather_permissions(ctx.claude_data_dir, ctx.cwd)
    unique_rules = [r for r, _l, _s in rules]
    if not unique_rules:
        return ctx.stop("No permissions to add.")

    roles_dir = get_roles_dir(ctx.profile)
    roles_dir.mkdir(parents=True, exist_ok=True)
    role_file = roles_dir / f"{role_name}.json"
    role_data = json.loads(role_file.read_text()) if role_file.exists() else {"mcpServers": {}, "permissions": {"allow": []}}

    existing = set(role_data.setdefault("permissions", {}).setdefault("allow", []))
    added = 0
    for rule in unique_rules:
        if rule not in existing:
            role_data["permissions"]["allow"].append(rule)
            existing.add(rule)
            added += 1

    role_file.write_text(json.dumps(role_data, indent=2))
    ctx.message(f"Added {added} permissions to '{role_name}'")
    return ctx.stop(f"Added {added} permissions to role '{role_name}' ({len(role_data['permissions']['allow'])} total).")


@command(':role-add-mcp')
def cmd_role_add_mcp(ctx):
    parts = ctx.args.strip().split(None, 1)
    if len(parts) != 2:
        return ctx.stop("Usage: :role-add-mcp <role-name> <server-name>")

    role_name, server_name = parts
    if not all(c.isalnum() or c in '-_' for c in role_name):
        return ctx.stop("Role name can only contain letters, numbers, dash, underscore.")

    if not ctx.session_id:
        return ctx.stop("No session ID.")

    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{ctx.session_id}.json"
    metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    session_servers = metadata.get("mcpServers", {})

    if server_name not in session_servers:
        available = ", ".join(session_servers.keys()) if session_servers else "none"
        return ctx.stop(f"Server '{server_name}' not in session. Available: {available}")

    roles_dir = get_roles_dir(ctx.profile)
    roles_dir.mkdir(parents=True, exist_ok=True)
    role_file = roles_dir / f"{role_name}.json"
    role_data = json.loads(role_file.read_text()) if role_file.exists() else {"mcpServers": {}, "permissions": {"allow": []}}

    role_data.setdefault("mcpServers", {})[server_name] = session_servers[server_name]
    role_file.write_text(json.dumps(role_data, indent=2))
    ctx.message(f"Added '{server_name}' to role '{role_name}'")
    return ctx.stop(f"Added MCP server '{server_name}' to role '{role_name}'.")


@command(':role-add')
def cmd_role_add(ctx):
    parts = ctx.args.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        return ctx.stop("Usage: :role-add <role-name> <num>\nRun :permissions to see numbered list.")

    role_name = parts[0]
    target_num = int(parts[1])
    if not all(c.isalnum() or c in '-_' for c in role_name):
        return ctx.stop("Role name can only contain letters, numbers, dash, underscore.")

    rules = gather_permissions(ctx.claude_data_dir, ctx.cwd)
    if target_num < 1 or target_num > len(rules):
        return ctx.stop(f"Invalid number {target_num}. Run :permissions to see valid range (1-{len(rules)}).")

    rule_to_add = rules[target_num - 1][0]

    roles_dir = get_roles_dir(ctx.profile)
    roles_dir.mkdir(parents=True, exist_ok=True)
    role_file = roles_dir / f"{role_name}.json"
    role_data = json.loads(role_file.read_text()) if role_file.exists() else {"mcpServers": {}, "permissions": {"allow": []}}

    allow = role_data.setdefault("permissions", {}).setdefault("allow", [])
    if rule_to_add in allow:
        return ctx.stop(f"Already in role '{role_name}': {rule_to_add}")

    allow.append(rule_to_add)
    role_file.write_text(json.dumps(role_data, indent=2))
    ctx.message(f"Added to role '{role_name}'")
    return ctx.stop(f"Added to role '{role_name}': {rule_to_add}")


@command(':roles-current')
def cmd_roles_current(ctx):
    if not ctx.session_id:
        return ctx.stop("No session ID.")

    config_dir = get_config_dir(ctx.profile)
    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{ctx.session_id}.json"
    metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    active_roles = metadata.get("activeRoles", [])

    implicit_roles = []
    if (config_dir / "mcp-roles" / "default.json").exists() and "default" not in active_roles:
        implicit_roles.append("default (implicit)")

    title_roles_file = config_dir / "title-roles.json"
    if title_roles_file.exists():
        try:
            title_mappings = json.loads(title_roles_file.read_text())
            tmux_socket = os.environ.get('KITTY_CLAUDE_TMUX_SOCKET', 'kitty-claude')
            result = subprocess.run(
                ["tmux", "-L", tmux_socket, "display-message", "-p", "#{window_name}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                window_name = result.stdout.strip()
                if window_name in title_mappings:
                    for r in title_mappings[window_name]:
                        if r not in active_roles:
                            implicit_roles.append(f"{r} (from title '{window_name}')")
        except:
            pass

    all_roles = [f"  {r}" for r in active_roles] + [f"  {r}" for r in implicit_roles]
    if all_roles:
        return ctx.stop("Active roles in this session:\n\n" + "\n".join(all_roles))
    return ctx.stop("No active roles. Use :role <name> to activate one.")


@command(':title-role')
def cmd_title_role(ctx):
    parts = ctx.args.strip().split()
    config_dir = get_config_dir(ctx.profile)
    title_roles_file = config_dir / "title-roles.json"
    mappings = json.loads(title_roles_file.read_text()) if title_roles_file.exists() else {}

    if len(parts) < 1:
        if mappings:
            lines = "Title-role mappings:\n\n"
            for title, role_list in mappings.items():
                lines += f"  {title} -> {', '.join(role_list)}\n"
            return ctx.stop(lines)
        return ctx.stop("No title-role mappings. Use :title-role <title> <role> to add one.")

    if len(parts) < 2:
        return ctx.stop("Usage: :title-role <title> <role>\n       :title-role (show mappings)")

    title, role_name = parts[0], parts[1]
    if title not in mappings:
        mappings[title] = []
    if role_name not in mappings[title]:
        mappings[title].append(role_name)
    title_roles_file.write_text(json.dumps(mappings, indent=2))
    ctx.message(f"Mapped '{title}' -> {role_name}")
    return ctx.stop(f"Mapped title '{title}' -> role '{role_name}'\nCurrent: {title} -> {', '.join(mappings[title])}")


@command(':role')
def cmd_role(ctx):
    role_name = ctx.args.strip()
    if not ctx.session_id:
        return ctx.stop("❌ No session ID")

    config_dir = get_config_dir(ctx.profile)
    roles_dir = config_dir / "mcp-roles"

    if not role_name:
        # fzf picker
        if not roles_dir.exists() or not any(roles_dir.glob("*.json")):
            return ctx.stop("No roles found.")

        fzf_lines = []
        for role_file in sorted(roles_dir.glob("*.json")):
            try:
                role = json.loads(role_file.read_text())
                servers = len(role.get("mcpServers", {}))
                perms = len(role.get("permissions", {}).get("allow", []))
                fzf_lines.append(f"{role_file.stem}\t{servers} servers, {perms} perms")
            except:
                fzf_lines.append(f"{role_file.stem}\t(error)")

        tmp_input = Path(tempfile.mktemp())
        tmp_output = Path(tempfile.mktemp())
        tmp_input.write_text("\n".join(fzf_lines))
        tmp_output.unlink(missing_ok=True)

        subprocess.run([
            "tmux", "-L", ctx.socket,
            "display-popup", "-E", "-w", "60%", "-h", "40%",
            f"cat {tmp_input} | fzf --delimiter='\\t' --with-nth=1,2 --header='Select role to activate' > {tmp_output}"
        ])

        selection = tmp_output.read_text().strip() if tmp_output.exists() else ""
        tmp_input.unlink(missing_ok=True)
        tmp_output.unlink(missing_ok=True)

        if not selection:
            return ctx.stop("No role selected.")
        role_name = selection.split('\t')[0]

    # Activate
    role_file = roles_dir / f"{role_name}.json"
    if not role_file.exists():
        return ctx.stop(f"❌ Role '{role_name}' not found. Use :roles to list.")

    role = json.loads(role_file.read_text())
    role_servers = role.get("mcpServers", {})

    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{ctx.session_id}.json"
    metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
    metadata.setdefault("mcpServers", {}).update(role_servers)
    active_roles = metadata.get("activeRoles", [])
    if role_name not in active_roles:
        active_roles.append(role_name)
    metadata["activeRoles"] = active_roles
    metadata_file.write_text(json.dumps(metadata, indent=2))

    server_names = ", ".join(role_servers.keys()) if role_servers else "none"
    perm_count = len(role.get("permissions", {}).get("allow", []))
    ctx.message(f"✓ Role '{role_name}' activated - use :reload")
    return ctx.stop(f"✓ Role '{role_name}' activated (servers: {server_names}, permissions: {perm_count})\n\nUse :reload to apply.")