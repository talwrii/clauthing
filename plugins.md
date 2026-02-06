# Plugins

Plugins are executables on PATH named `kitty-claude-*`.

## Colon commands

`:foo` runs `kitty-claude-foo` with stdin/stdout connected.

## Events

On startup, each plugin is spawned with `--events`. JSON events are piped to stdin:
- `{"type": "sync", "sessions": [...]}`
- `{"type": "title_changed", "session_id": "...", "name": "..."}`
- `{"type": "session_opened", ...}`
- `{"type": "session_closed", ...}`

If plugin doesn't support `--events`, it exits and we ignore.

## Example

`kitty-claude-titles`:
- `:titles` - show recent titles picker
- `--events` - track title changes in background
