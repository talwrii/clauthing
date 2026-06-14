"""The clauthing plugin directory: plugins stored by name with pinned permissions.

`~/.config/clauthing/[other-profiles/<profile>/]plugins/<name>/plugin.json` holds
`{name, permissions, token}`. Permissions are pinned — a plugin re-requesting a
different set is flagged for re-approval rather than silently widened.

There is no flat token->plugin table; a presented token is resolved by scanning
the directory (small N).
"""
import json
import secrets
from pathlib import Path


def plugins_dir(profile=None):
    if profile:
        base = Path.home() / ".config" / "clauthing" / "other-profiles" / profile
    else:
        base = Path.home() / ".config" / "clauthing"
    return base / "plugins"


def _entry_file(name, profile=None):
    return plugins_dir(profile) / name / "plugin.json"


def load_plugin(name, profile=None):
    """Return the stored entry {name, permissions, token} for `name`, or None."""
    f = _entry_file(name, profile)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return None
    return None


def save_plugin(name, permissions, token, profile=None):
    """Record an approved plugin (permissions pinned as a sorted list)."""
    f = _entry_file(name, profile)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(
        {"name": name, "permissions": sorted(set(permissions)), "token": token},
        indent=2,
    ))


def issue_token():
    return secrets.token_hex(16)


def resolve_token(token, profile=None):
    """Return the plugin entry whose token matches, or None."""
    if not token:
        return None
    pd = plugins_dir(profile)
    if not pd.exists():
        return None
    for d in pd.iterdir():
        f = d / "plugin.json"
        if f.exists():
            try:
                entry = json.loads(f.read_text())
            except Exception:
                continue
            if entry.get("token") == token:
                return entry
    return None


def permissions_match(entry, requested):
    """True if the entry's pinned permissions equal the requested set."""
    return sorted(set(entry.get("permissions", []))) == sorted(set(requested))
