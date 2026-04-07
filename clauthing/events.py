#!/usr/bin/env python3
"""Events log for clauthing.

Daemon-free event system. Each clauthing instance appends events to a
shared append-only JSONL log file. Consumers watch the file with inotify
and use bisect to seek by timestamp.

Event log location:
    /var/run/<uid>/clauthing/events.jsonl  (or /tmp fallback)

Each line is a JSON object with at least:
    {"ts": <unix_timestamp_float>, "type": "...", ...}

Event types:
    title_changed  - {"ts", "type", "session_id", "name"}
    session_opened - {"ts", "type", "session_id", "name", "path"}
    session_closed - {"ts", "type", "session_id"}
"""
import bisect
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

# Track plugin pipelines: {plugin_name: Popen}
_plugin_pipelines = {}


def get_runtime_dir(profile=None):
    """Get the runtime directory for clauthing."""
    uid = os.getuid()
    try:
        runtime_dir = Path(f"/var/run/{uid}/clauthing")
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        runtime_dir = Path(f"/tmp/clauthing-{uid}")
        runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def get_events_log_path(profile=None):
    """Get the events log file path."""
    runtime_dir = get_runtime_dir(profile)
    if profile:
        return runtime_dir / f"events-{profile}.jsonl"
    return runtime_dir / "events.jsonl"


def get_windows_file(profile=None):
    """Get the windows mapping file path."""
    runtime_dir = get_runtime_dir(profile)
    return runtime_dir / "windows.json"


def load_windows(profile=None):
    """Load window mappings from file."""
    path = get_windows_file(profile)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except:
            pass
    return {}


def save_windows(windows, profile=None):
    """Save window mappings to file."""
    path = get_windows_file(profile)
    path.write_text(json.dumps(windows, indent=2))


def update_window(session_id, title, socket, path=None, profile=None):
    """Update a window entry in the mappings file."""
    windows = load_windows(profile)
    windows[session_id] = {
        "title": title,
        "socket": socket,
        "path": path or "",
        "updated": time.time(),
    }
    save_windows(windows, profile)


def remove_window(session_id, profile=None):
    """Remove a window from the mappings file."""
    windows = load_windows(profile)
    if session_id in windows:
        del windows[session_id]
        save_windows(windows, profile)


def get_all_windows(profile=None):
    """Get all windows from the mappings file."""
    return load_windows(profile)


def emit_event(event, profile=None):
    """Append an event to the events log file.

    Adds a timestamp if not present. Safe to call from any context.
    Uses atomic append (O_APPEND) so concurrent writers don't corrupt.

    Plugins subscribe via: clauthing --events | plugin --events
    """
    if "ts" not in event:
        event["ts"] = time.time()

    log_path = get_events_log_path(profile)
    line = json.dumps(event, separators=(",", ":")) + "\n"

    # O_APPEND ensures atomic appends for lines < PIPE_BUF (4096 on Linux)
    fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def read_events(profile=None, since=None):
    """Read events from the log file.

    Args:
        profile: clauthing profile name
        since: If set, only return events with ts >= since (uses bisect)

    Returns:
        List of event dicts.
    """
    log_path = get_events_log_path(profile)
    if not log_path.exists():
        return []

    events = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if since is not None:
        timestamps = [e.get("ts", 0) for e in events]
        idx = bisect.bisect_left(timestamps, since)
        events = events[idx:]

    return events


def tail_events(profile=None, since=None):
    """Yield events from the log file, blocking for new ones via inotify.

    Args:
        profile: clauthing profile name
        since: If set, replay events with ts >= since before tailing

    Yields:
        Event dicts as they appear.
    """
    log_path = get_events_log_path(profile)

    # Ensure file exists
    if not log_path.exists():
        log_path.touch(mode=0o666)

    try:
        import inotify.adapters
        has_inotify = True
    except ImportError:
        has_inotify = False

    with open(log_path) as f:
        if since is not None:
            # Replay from since
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("ts", 0) >= since:
                        yield event
                except json.JSONDecodeError:
                    continue
        else:
            # Seek to end
            f.seek(0, 2)

        if has_inotify:
            yield from _tail_inotify(f, log_path)
        else:
            yield from _tail_poll(f)


def _tail_inotify(f, log_path):
    """Tail using inotify for efficient blocking."""
    import inotify.adapters

    i = inotify.adapters.Inotify()
    i.add_watch(str(log_path))

    try:
        for event in i.event_gen(yield_nones=False):
            (_, type_names, _, _) = event
            if "IN_MODIFY" in type_names:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
    finally:
        i.remove_watch(str(log_path))


def _tail_poll(f, interval=0.5):
    """Fallback tail using polling (when inotify not available)."""
    while True:
        line = f.readline()
        if line:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass
        else:
            time.sleep(interval)


def subscribe_events(profile=None, since=None):
    """Print events to stdout as they arrive. Blocks until interrupted.

    Emits a sync event first with all current sessions, then tails
    the event log for incremental updates.

    Args:
        profile: clauthing profile name
        since: If set, replay events from this timestamp first

    Returns:
        Exit code (0 on clean exit, 1 on error).
    """
    try:
        # Initial sync of current state
        sessions = get_current_sessions(profile)
        print(json.dumps({"type": "sync", "sessions": sessions}), flush=True)

        for event in tail_events(profile, since=since):
            print(json.dumps(event), flush=True)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}", flush=True)
        return 1
    return 0


def get_current_sessions(profile=None):
    """Get all current sessions with metadata and PIDs (for sync on connect)."""
    from clauthing.session import get_state_dir, get_open_sessions
    from clauthing.claude import get_running_sessions

    state_dir = get_state_dir()
    sessions_dir = state_dir / "sessions"

    # Build pid lookup from running sessions
    running = get_running_sessions(profile)
    pid_map = {s["session_id"]: s.get("pid") for s in running}

    sessions = []
    open_session_ids = get_open_sessions(profile)

    for session_id in open_session_ids:
        entry = {
            "session_id": session_id,
            "name": session_id,
            "path": "",
            "pid": pid_map.get(session_id),
        }
        metadata_file = sessions_dir / f"{session_id}.json"
        if metadata_file.exists():
            try:
                metadata = json.loads(metadata_file.read_text())
                entry["name"] = metadata.get("name", session_id)
                entry["path"] = metadata.get("path", "")
            except Exception:
                pass
        sessions.append(entry)
    return sessions


def set_title(session_id, name, profile=None):
    """Set a session title: update metadata, state file, tmux, and emit event."""
    import subprocess
    from clauthing.session import get_state_dir
    from clauthing.tmux import get_runtime_tmux_state_file

    # Update session metadata
    state_dir = get_state_dir()
    metadata_file = state_dir / "sessions" / f"{session_id}.json"
    if metadata_file.exists():
        try:
            metadata = json.loads(metadata_file.read_text())
            metadata["name"] = name
            metadata_file.write_text(json.dumps(metadata, indent=2))
        except Exception:
            pass

    # Update runtime state file and rename tmux window
    state_file = get_runtime_tmux_state_file(profile)
    tmux_socket = f"clauthing-{profile}" if profile else "clauthing"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            for window_index, window_data in state.get("windows", {}).items():
                if window_data.get("session_id") == session_id:
                    window_data["name"] = name
                    try:
                        subprocess.run(
                            ["tmux", "-L", tmux_socket,
                             "rename-window", "-t", f":{window_index}", name],
                            capture_output=True, text=True,
                        )
                    except Exception:
                        pass
                    break
            state_file.write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    # Emit title_changed event
    emit_event({
        "type": "title_changed",
        "session_id": session_id,
        "name": name,
    }, profile)

    # Update windows mapping file
    path = ""
    if metadata_file.exists():
        try:
            metadata = json.loads(metadata_file.read_text())
            path = metadata.get("path", "")
        except:
            pass
    update_window(session_id, name, tmux_socket, path, profile)


def discover_plugins():
    """Find all clauthing-* executables on PATH."""
    plugins = []
    seen = set()

    for dir_path in os.environ.get("PATH", "").split(os.pathsep):
        dir_path = Path(dir_path)
        if not dir_path.is_dir():
            continue
        try:
            for entry in dir_path.iterdir():
                if entry.name.startswith("clauthing-") and entry.name not in seen:
                    if entry.is_file() and os.access(entry, os.X_OK):
                        plugins.append(entry.name)
                        seen.add(entry.name)
        except PermissionError:
            continue

    return plugins


def start_plugin_pipeline(plugin_name, profile=None):
    """Start a plugin pipeline: clauthing --events | plugin --events.

    Returns the shell process Popen object.
    """
    kc_path = shutil.which("clauthing") or "clauthing"
    plugin_path = shutil.which(plugin_name)
    if not plugin_path:
        return None

    profile_arg = f"--profile {profile} " if profile else ""

    # Run as shell pipeline
    cmd = f"{kc_path} {profile_arg}--events | {plugin_path} --events"
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def start_all_plugins(profile=None):
    """Start event pipelines for all discovered plugins."""
    global _plugin_pipelines

    plugins = discover_plugins()
    for plugin_name in plugins:
        if plugin_name not in _plugin_pipelines or _plugin_pipelines[plugin_name].poll() is not None:
            proc = start_plugin_pipeline(plugin_name, profile)
            if proc:
                _plugin_pipelines[plugin_name] = proc


def check_and_restart_plugins(profile=None):
    """Check plugin pipelines and restart any that died."""
    global _plugin_pipelines

    for plugin_name, proc in list(_plugin_pipelines.items()):
        if proc.poll() is not None:  # Process died
            new_proc = start_plugin_pipeline(plugin_name, profile)
            if new_proc:
                _plugin_pipelines[plugin_name] = new_proc


def stop_all_plugins():
    """Stop all plugin pipelines."""
    global _plugin_pipelines

    for plugin_name, proc in _plugin_pipelines.items():
        try:
            proc.terminate()
        except:
            pass

    _plugin_pipelines = {}


def get_plugin_status():
    """Get status of all plugin pipelines."""
    status = {}
    for plugin_name, proc in _plugin_pipelines.items():
        poll = proc.poll()
        status[plugin_name] = {
            "pid": proc.pid,
            "running": poll is None,
            "exit_code": poll,
        }
    return status


