#!/usr/bin/env python3
"""Claude Code hook handlers for clauthing.

These are invoked by `clauthing --session-start`, `--user-prompt-submit`,
`--stop`, `--pre-tool-use` and `--run-command`. They were previously in
colon_command.py but moved here so colon_command.py is just about commands.
"""
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from clauthing.colon_command import (
    CommandContext,
    dispatch,
    get_tmux_socket,
    send_tmux_message,
    load_timed_permissions,
)
from clauthing.colon_commands.time import (
    save_request_start_time,
    save_response_duration,
)
from clauthing.logging import log
from clauthing.session import (
    mark_session_has_messages,
    remove_open_session,
)
from clauthing.events import get_runtime_dir, load_windows


def _attention_file(profile=None):
    return get_runtime_dir(profile) / "attention.json"


def _load_attention(profile=None):
    f = _attention_file(profile)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_attention(data, profile=None):
    _attention_file(profile).write_text(json.dumps(data, indent=2))


def mark_attention(session_id, profile=None):
    if not session_id:
        return
    data = _load_attention(profile)
    win = load_windows(profile).get(session_id, {})
    data[session_id] = {
        "ts": time.time(),
        "title": win.get("title"),
        "socket": win.get("socket"),
        "path": win.get("path"),
    }
    _save_attention(data, profile)


def clear_attention(session_id, profile=None):
    if not session_id:
        return
    data = _load_attention(profile)
    if session_id in data:
        del data[session_id]
        _save_attention(data, profile)


def handle_notification():
    """Handle Notification hook — Claude wants the user's attention."""
    try:
        input_data = json.loads(sys.stdin.read())
        session_id = input_data.get('session_id')
        profile = os.environ.get('CLAUTHING_PROFILE')
        mark_attention(session_id, profile)
    except Exception as e:
        with open("/tmp/clauthing-notification-hook-error.log", "a") as f:
            f.write(f"Notification hook error: {str(e)}\n")


def handle_user_prompt_submit(claude_data_dir=None):
    """Handle UserPromptSubmit hook."""
    socket = get_tmux_socket()
    try:
        if claude_data_dir is None:
            config_env = os.environ.get('CLAUDE_CONFIG_DIR')
            claude_data_dir = Path(config_env) if config_env else Path.home() / ".config" / "clauthing" / "claude-data"

        input_data = json.loads(sys.stdin.read())
        prompt = input_data.get('prompt', '').strip()

        # Register running session
        session_id = input_data.get('session_id')
        if session_id:
            profile = os.environ.get('CLAUTHING_PROFILE')
            clear_attention(session_id, profile)
            cwd = input_data.get('cwd', os.getcwd())
            try:
                claude_pid = None
                result = subprocess.run(["pgrep", "-f", f"claude --resume {session_id}"], capture_output=True, text=True)
                if result.returncode == 0:
                    claude_pid = int(result.stdout.strip().split('\n')[0])
                if not claude_pid:
                    sock = os.environ.get('CLAUTHING_TMUX_SOCKET')
                    if sock:
                        result = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "#{pane_pid}"], capture_output=True, text=True)
                        if result.returncode == 0:
                            pane_pid = int(result.stdout.strip())
                            result = subprocess.run(["pgrep", "-P", str(pane_pid), "claude"], capture_output=True, text=True)
                            if result.returncode == 0:
                                claude_pid = int(result.stdout.strip().split('\n')[0])
                if claude_pid:
                    from clauthing.claude import register_running_session
                    register_running_session(session_id, claude_pid, cwd, profile)
            except Exception:
                pass

            # Record that this session has had at least one real prompt.
            # The restore loop uses this flag to decide between
            # `claude --resume` (has messages) and a fresh spawn (blank).
            try:
                mark_session_has_messages(session_id)
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
                profile = os.environ.get('CLAUTHING_PROFILE')
                if profile:
                    config_dir = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
                else:
                    config_dir = Path.home() / ".config" / "clauthing"
                skill_file = config_dir / "cl-skills" / f"{skill_name}.md"
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

        # Plugin dispatch: :foo -> clauthing-foo on PATH
        if prompt.startswith(':'):
            parts = prompt[1:].split(None, 1)
            cmd_name = parts[0] if parts else ""
            cmd_args = parts[1] if len(parts) > 1 else ""
            plugin_bin = shutil.which(f"clauthing-{cmd_name}")
            if plugin_bin:
                import tempfile
                env_exports = []
                if session_id:
                    env_exports.append(f"CLAUTHING_SESSION_ID={session_id}")
                env_exports.append(f"CLAUTHING_SOCKET={socket}")
                env_exports.append(f"CLAUTHING_CWD={input_data.get('cwd', os.getcwd())}")
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
        error_msg = f"Hook error: {str(e)}"
        tb = traceback.format_exc()
        send_tmux_message(f"❌ {error_msg}", socket)
        profile = os.environ.get('CLAUTHING_PROFILE')
        log(f"COLON COMMAND ERROR: {error_msg}\n{tb}", profile)
        try:
            input_data = json.loads(sys.stdin.read()) if 'input_data' not in locals() else input_data
            print(input_data.get('prompt', ''))
        except Exception:
            pass


def handle_session_start():
    """Handle SessionStart hook."""
    try:
        input_data = json.loads(sys.stdin.read())
        session_id = input_data.get('session_id')
        if not session_id:
            print(json.dumps({"continue": True}))
            return

        profile = os.environ.get('CLAUTHING_PROFILE') or None
        # Snapshot auth fields from the session's .claude.json into
        # claude-auth.json early — so future sessions / reloads can repopulate
        # auth even if claude is killed before exiting cleanly.
        try:
            from clauthing.claude import save_auth_from_session
            save_auth_from_session(session_id, profile)
        except Exception as _e:
            log(f"SessionStart save_auth failed: {_e}", profile)
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
            sock = os.environ.get('CLAUTHING_TMUX_SOCKET')
            if sock:
                uid = os.getuid()
                msg_file = Path(f"/tmp/cl-popup-{uid}.txt")
                script_file = Path(f"/tmp/cl-popup-{uid}.sh")
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
        with open("/tmp/clauthing-session-start-error.log", "a") as f:
            f.write(f"SessionStart hook error: {str(e)}\n")
        print(json.dumps({"continue": True}))


def handle_stop():
    """Handle Stop hook."""
    try:
        input_data = json.loads(sys.stdin.read())
        session_id = input_data.get('session_id')
        profile = os.environ.get('CLAUTHING_PROFILE') or None
        if session_id:
            clear_attention(session_id, profile)
            save_response_duration(session_id)
            # Snapshot auth so :reload / restarts can repopulate without OAuth.
            try:
                from clauthing.claude import save_auth_from_session
                save_auth_from_session(session_id, profile)
            except Exception as _e:
                log(f"Stop save_auth failed: {_e}", profile)
            # NB: do NOT remove from open_sessions here. open_sessions
            # means "window is worth restoring", not "currently mid-response".
            # Removal happens when the user explicitly closes the window
            # (clauthing --close-window, bound to C-w).

        sock = os.environ.get('CLAUTHING_TMUX_SOCKET')
        if sock:
            uid = os.getuid()
            queue_file = Path(f"/run/user/{uid}/cl-queue-{sock}.txt")
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
        with open("/tmp/clauthing-stop-hook-error.log", "a") as f:
            f.write(f"Stop hook error: {str(e)}\n")


def handle_pre_tool_use():
    """Handle PreToolUse hook - deny expired timed permissions."""
    try:
        input_data = json.loads(sys.stdin.read())
        session_id = input_data.get('session_id')
        if session_id:
            clear_attention(session_id, os.environ.get('CLAUTHING_PROFILE'))
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
        with open("/tmp/clauthing-pre-tool-use-error.log", "a") as f:
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
