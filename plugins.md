# Plugins

Plugins are kind of in flux at athe moment.

There are few types of plugins. The most basic type are colon-commands.
There are also workflow plugins. These are for windows which select workflow.

Colon command plugins are executables on PATH named `clauthing-*`.

## Colon commands

`:foo` runs `clauthing-foo` with stdin/stdout connected.

## Events

On startup, clauthing spawns pipelines for each plugin:

```bash
clauthing --events | clauthing-foo --events
```

PIDs are tracked and restarted if they die.

Event types:
- `{"type": "sync", "sessions": [...]}`
- `{"type": "title_changed", "session_id": "...", "name": "..."}`
- `{"type": "session_opened", ...}`
- `{"type": "session_closed", ...}`

## What a plugin gets (the contract)

A colon-command plugin is run in a tmux popup (so it can be interactive — fzf,
vim, prompts). clauthing passes it, via the environment:

- `CLAUTHING_SOCKET` — the tmux socket (`clauthing` or `clauthing-<profile>`)
- `CLAUTHING_SESSION_ID` — the calling window's claude session
- `CLAUTHING_CWD` — the calling window's directory
- *(planned)* `CLAUTHING_WINDOW` — the calling window's stable `clauthing_window`
- *(planned)* `CLAUTHING_PROFILE`
- *(planned)* `CLAUTHING_PLUGIN_CONFIG` — the plugin's own config dir (where it
  stores the token it obtains; see Token lifecycle)

The plugin's **stdout** is the result: plain text is shown to the user; a line
starting with `:` is re-dispatched as a colon command (chaining).

Plugins own their own scheduling (e.g. `systemd-run --user`) and state (their
own files). clauthing does not manage timers or persist plugin state.

## The API: acting on clauthing

A plugin talks back to clauthing through one stable command: **`clauthing-api`**.
The plugin presents a token on each call (`CLAUTHING_PLUGIN_TOKEN`); the command
resolves which plugin is calling and checks the verb against that plugin's
approved permissions.

### Token lifecycle (identify, approve, reuse)

On first run the plugin **identifies itself** (by name) and **requests the
permissions it needs**, via `clauthing-api ping`:

1. clauthing looks the plugin up in its **plugin directory** (keyed by name).
2. **New plugin** — it shows the requested permissions for approval; on approval
   it records the plugin + approved permissions in the directory and returns a
   token, which the plugin stores in its config dir
   (`$CLAUTHING_PLUGIN_CONFIG/token`).
3. **Known plugin** — clauthing checks the requested permissions against the
   recorded set: **they must not have changed**. A changed request needs
   re-approval; otherwise the stored token is accepted.

On every later call the plugin sends its stored token (`CLAUTHING_PLUGIN_TOKEN`);
clauthing resolves it via the plugin directory and checks the verb's permission.

The `clauthing_plugin` SDK does all of this transparently — the plugin author
just calls `windows()` / `open_window()` / `close()`, and the SDK pings to
obtain (or reuse) the token, stores it, and attaches it.

### Interface — each verb is its own permission

| verb | does | permission |
|------|------|------------|
| `ping` | startup handshake — confirm the connection / token | `ping` |
| `windows` | list windows as JSON (stable `clauthing_window`, session, path) | `windows` |
| `open --resume <session> [--cwd] [--name]` | open / resume a window | `window:open` |
| `close <window>` | close a window (`id`/`name`/`clauthing_window`) | `window:close` |

A plugin declares the permissions it needs in its manifest; clauthing only lets
it use the verbs it was approved for. Address windows by the stable
`clauthing_window`, not `session_id` (which rotates on `:cd`).

> `clauthing-workflow` (with its own `select-window`) remains the specialised
> *window-switching* tool; `clauthing-api` is the general plugin API.

## Permissions

The manifest (`--manifest`, via the `clauthing_plugin` SDK) declares which
permissions each command needs. On install, clauthing shows those permissions
for approval; on approval it stores the approved set alongside the plugin's
token. Every `clauthing-api` call is checked against the calling token's
approved permissions.

## Philosophy — voluntary isolation (informal)

The plugin framework is **informal**. There's a *pretense* of isolation: each
plugin is given a token scoped to a limited set of abilities. But there is **no
sandboxing** — a plugin runs as you and can go get other credentials — so the
limits are entirely **voluntary**. They don't stop a determined or compromised
plugin.

What they *do* give you is a way to voluntarily prevent **mistakes**: a plugin
that only asked for `windows` cannot accidentally `close` one. The token + the
permission list are guard rails against fumbling, not a security boundary.

Real sandboxing may come later; the token plumbing exists so it can be added
without changing the plugin contract.

## Lifecycle: install → manifest → approve → isolate (planned)

1. **Install** — `:install <source>`:
   - `:install talwrii/blah` — fetch from GitHub (`user/repo`) by default.
   - `:install ~/mine/blah` — install from a local path.
   Makes `clauthing-<name>` available on PATH.
2. **Manifest** — clauthing runs `clauthing-<name> --manifest` (built with the
   `clauthing_plugin` SDK) to learn the commands + the permissions each requests.
3. **Approve** — show the requested permissions; on approval, store the
   manifest. Only commands from approved manifests are exposed (the "what gets
   shown" permission system).
4. **Isolate** — give the plugin:
   - its own **config dir**: `~/.config/clauthing/plugins/<name>/`
   - a **token** (passed as `CLAUTHING_PLUGIN_TOKEN`) it presents when calling
     the clauthing API.

### The plugin directory

clauthing stores plugins **by name**, not as a flat token→plugin table:
`~/.config/clauthing/plugins/<name>/` holds

- `manifest.json` — the approved manifest
- `permissions` — the approved permission set, **pinned**: if a later `ping`
  requests a different set, clauthing flags it for re-approval rather than
  silently widening access
- `token` — the issued credential
- the plugin's own config / state ($CLAUTHING_PLUGIN_CONFIG points here)

`clauthing-api` resolves `CLAUTHING_PLUGIN_TOKEN` against this directory and —
*eventually* — checks the call against the recorded permissions.

> Security is **fictitious for now**: the token is issued, stored, and passed
> through, but the API does not yet enforce per-permission access. The plumbing
> exists so we can clamp down later (capability-scoped tokens) without changing
> the plugin contract.

## Example: a general-purpose plugin

`jugglarm` (`:juggle <window> 1h`) — close a window now, reopen/resume it after
a delay. Needs permissions `windows`, `window:open`, `window:close`. Pure
orchestration over the contract above:
1. `clauthing-api windows` → resolve the target → its `session_id` + cwd
2. `systemd-run --user --on-active=1h` a `clauthing-api open --resume <id>`
   (its own scheduling), carrying `CLAUTHING_PLUGIN_TOKEN`
3. `clauthing-api close <window>` now

`clauthing-titles`:
- `:titles` - show recent titles picker
- `--events` - track title changes in background (opt-in; a plugin that doesn't
  handle `--events` should drain stdin so it isn't restart-looped)
