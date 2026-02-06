# Plugins

Plugins are executables on PATH named `kitty-claude-*`.

## Colon commands

`:foo` runs `kitty-claude-foo` with stdin/stdout connected.

## Events

On startup, kitty-claude spawns pipelines for each plugin:

```bash
kitty-claude --events | kitty-claude-foo --events
```

PIDs are tracked and restarted if they die.

Event types:
- `{"type": "sync", "sessions": [...]}`
- `{"type": "title_changed", "session_id": "...", "name": "..."}`
- `{"type": "session_opened", ...}`
- `{"type": "session_closed", ...}`

## Example

`kitty-claude-titles`:
- `:titles` - show recent titles picker
- `--events` - track title changes in background
