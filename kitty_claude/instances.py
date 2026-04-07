"""Per-instance registry for kitty-claude.

Each `kitty-claude` launch generates a UUID, registers itself, and sets
KITTY_CLAUDE_INSTANCE_UUID in the environment so child processes (and the
logging module) can find their instance's data.

Registry layout: /run/user/$UID/kitty-claude/instances.json
(falls back to /tmp/kitty-claude-instances.json on PermissionError)

  {
    "<uuid>": {
      "pid": 12345,
      "tmux_socket": "kitty-claude",
      "profile": null,
      "cwd": "/home/.../foo",
      "started_at": "2026-04-07T11:42:00",
      "log_dir": "/run/user/1000/kitty-claude/instances/<uuid>"
    },
    ...
  }
"""
import json
import os
import datetime
import uuid as _uuid
from pathlib import Path

ENV_VAR = "KITTY_CLAUDE_INSTANCE_UUID"


def _registry_path():
    uid = os.getuid()
    primary = Path(f"/run/user/{uid}/kitty-claude/instances.json")
    try:
        primary.parent.mkdir(parents=True, exist_ok=True)
        # touch to verify writability
        if not primary.exists():
            primary.write_text("{}")
        return primary
    except (PermissionError, OSError):
        fallback = Path("/tmp/kitty-claude-instances.json")
        if not fallback.exists():
            fallback.write_text("{}")
        return fallback


def _instances_dir():
    uid = os.getuid()
    primary = Path(f"/run/user/{uid}/kitty-claude/instances")
    try:
        primary.mkdir(parents=True, exist_ok=True)
        return primary
    except (PermissionError, OSError):
        fallback = Path("/tmp/kitty-claude-instances")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _read_registry():
    path = _registry_path()
    try:
        return json.loads(path.read_text() or "{}")
    except Exception:
        return {}


def _write_registry(data):
    _registry_path().write_text(json.dumps(data, indent=2))


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def get_log_dir_for_uuid(uuid):
    d = _instances_dir() / uuid
    d.mkdir(parents=True, exist_ok=True)
    return d


def current_uuid():
    return os.environ.get(ENV_VAR)


def register_instance(tmux_socket, profile, cwd):
    """Generate a UUID, record this process in the registry, return the UUID.

    The caller is expected to set os.environ[ENV_VAR] before exec'ing children.
    """
    uuid = str(_uuid.uuid4())
    log_dir = get_log_dir_for_uuid(uuid)
    entry = {
        "pid": os.getpid(),
        "tmux_socket": tmux_socket,
        "profile": profile,
        "cwd": str(cwd),
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "log_dir": str(log_dir),
    }
    data = _read_registry()
    # Prune dead entries while we're here.
    data = {u: e for u, e in data.items() if _pid_alive(e.get("pid", -1))}
    data[uuid] = entry
    _write_registry(data)
    return uuid


def list_instances(prune=True):
    """Return a list of instance dicts (each with 'uuid' added).

    If prune=True, dead instances are removed from the registry on disk.
    """
    data = _read_registry()
    alive = {}
    for uuid, entry in data.items():
        if _pid_alive(entry.get("pid", -1)):
            alive[uuid] = entry
    if prune and alive != data:
        _write_registry(alive)
    out = []
    for uuid, entry in alive.items():
        e = dict(entry)
        e["uuid"] = uuid
        out.append(e)
    out.sort(key=lambda e: e.get("started_at", ""))
    return out
