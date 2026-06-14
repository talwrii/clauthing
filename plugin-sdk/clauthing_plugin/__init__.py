"""clauthing_plugin — SDK for writing clauthing plugins.

Installed separately from clauthing (its own pipx/pip package) so a plugin —
which runs in its own interpreter — can import it regardless of clauthing's
isolated venv.

Two halves:

1. **Plugin** — declare your colon commands + the permissions each needs.
   `--manifest` emits the JSON manifest clauthing reads to register + approve.

2. **API client** — thin stubs (`windows()`, `open_window()`, `close()`,
   `select()`) that shell out to the `clauthing-api` command provided by
   clauthing. The credential (`CLAUTHING_PLUGIN_TOKEN`) and socket flow through
   the environment that clauthing set when it invoked the plugin, so the client
   just inherits `os.environ` — the plugin author never handles the token.

Example plugin (`clauthing-jugglarm`):

    import clauthing_plugin as cp

    plugin = cp.Plugin("jugglarm", "snooze windows")

    @plugin.command("juggle", "close a window, reopen after a delay",
                    permissions=["window:close", "window:open"])
    def juggle(args):
        name, dur = args[0], args[1]
        win = next(w for w in cp.windows() if w["name"] == name)
        ...                      # schedule cp.open_window(resume=win["session_id"])
        cp.close(name)
        return 0

    if __name__ == "__main__":
        raise SystemExit(plugin.run())
"""
import json
import os
import shutil
import subprocess
import sys

__all__ = ["Plugin", "windows", "open_window", "close", "select", "api"]


# ── Plugin definition (manifest) ─────────────────────────────────────────────

class Plugin:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description
        self._commands = {}

    def command(self, name, description="", permissions=None):
        """Register a colon command. `permissions` lists capability strings the
        command needs (e.g. 'window:close', 'window:open') for clauthing to
        show + approve."""
        def deco(fn):
            self._commands[name] = {
                "description": description,
                "permissions": list(permissions or []),
                "handler": fn,
            }
            return fn
        return deco

    def manifest(self):
        return {
            "name": self.name,
            "description": self.description,
            "commands": [
                {"name": n, "description": c["description"],
                 "permissions": c["permissions"]}
                for n, c in sorted(self._commands.items())
            ],
        }

    def run(self, argv=None):
        argv = list(sys.argv[1:] if argv is None else argv)
        if argv and argv[0] == "--manifest":
            print(json.dumps(self.manifest(), indent=2))
            return 0
        if not argv:
            print(f"{self.name}: " + (", ".join(sorted(self._commands)) or "(no commands)"))
            return 0
        if argv[0] in self._commands:
            name, rest = argv[0], argv[1:]
        elif len(self._commands) == 1:
            name, rest = next(iter(self._commands)), argv
        else:
            print(f"unknown command: {argv[0]}", file=sys.stderr)
            return 2
        return self._commands[name]["handler"](rest) or 0


# ── API client (wraps the `clauthing-api` command) ────────────────────────

def api(*args, capture=True):
    """Call the clauthing-api command. Credential + socket are inherited
    from the environment clauthing set when it invoked this plugin."""
    binpath = shutil.which("clauthing-api") or "clauthing-api"
    return subprocess.run([binpath, *args], capture_output=capture, text=True)


def windows():
    """Return the list of clauthing windows (dicts with the stable
    clauthing_window handle, session_id, name, path, ...)."""
    r = api("windows")
    return json.loads(r.stdout or "[]")


def open_window(resume=None, cwd=None, name=None):
    args = ["open"]
    if resume:
        args += ["--resume", resume]
    if cwd:
        args += ["--cwd", cwd]
    if name:
        args += ["--name", name]
    api(*args, capture=False)


def close(window):
    api("close", window, capture=False)


def select(window):
    api("select", window, capture=False)


# ── module-level decorator API (manifest auto-built; docs from docstrings) ────
#
#     from clauthing_plugin import plugin_name, colon_command, run
#
#     plugin_name("jugglarm", "snooze windows")
#
#     @colon_command(permissions=["window:close", "window:open"])
#     def juggle(args):
#         "Close a window now and reopen it after a delay."
#         ...
#
#     run()   # console-script entry — derives the command from the exe name
#
# The manifest is built from the registered commands; each command's
# description is the first line of its docstring.

_META = {"name": None, "description": ""}
_COMMANDS = {}        # command name -> {description, permissions, handler}
_EVENTS_HANDLER = None


def plugin_name(name, description=""):
    """Set the plugin's name (+ optional description) for the manifest."""
    _META["name"] = name
    _META["description"] = description


def colon_command(permissions=None):
    """Register a colon command. Its description is taken from the function's
    docstring (first line)."""
    def deco(fn):
        doc = (fn.__doc__ or "").strip()
        _COMMANDS[fn.__name__] = {
            "description": doc.splitlines()[0].strip() if doc else "",
            "permissions": list(permissions or []),
            "handler": fn,
        }
        return fn
    return deco


def on_events(fn):
    """Register a handler invoked on the --events startup signal (optional)."""
    global _EVENTS_HANDLER
    _EVENTS_HANDLER = fn
    return fn


def build_manifest():
    return {
        "name": _META["name"],
        "description": _META["description"],
        "commands": [
            {"name": n, "description": c["description"], "permissions": c["permissions"]}
            for n, c in sorted(_COMMANDS.items())
        ],
    }


def run(argv=None):
    """Console-script entry point.

    --manifest  → print the auto-built manifest (from the decorated commands)
    --events    → run the registered on_events handler, else just drain stdin
    otherwise   → run the command matching the executable name
                  (clauthing-<cmd> runs <cmd>); remaining args are passed through.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--manifest"]:
        print(json.dumps(build_manifest(), indent=2))
        return 0
    if argv[:1] == ["--events"]:
        if _EVENTS_HANDLER:
            return _EVENTS_HANDLER() or 0
        for _ in sys.stdin:
            pass
        return 0
    exe = os.path.basename(sys.argv[0])
    cmd = exe[len("clauthing-"):] if exe.startswith("clauthing-") else exe
    entry = _COMMANDS.get(cmd)
    if argv[:1] in (["--help"], ["-h"]):
        # NB: must not start with ':' — clauthing re-dispatches plugin output
        # that begins with a colon, which would feed the help back as a prompt.
        doc = ((entry["handler"].__doc__ if entry else "") or "").strip()
        out = [f"Help for :{cmd}"]
        if doc:
            out.append(doc)
        if len(_COMMANDS) > 1:
            out.append("")
            out.append(f"{_META.get('name') or 'plugin'} commands:")
            for n, c in sorted(_COMMANDS.items()):
                d = c["description"]
                out.append(f"  :{n}" + (f" — {d}" if d else ""))
        print("\n".join(out))
        return 0
    if entry is None:
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    return entry["handler"](argv) or 0
