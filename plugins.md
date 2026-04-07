# Plugins

Plugins are executables on PATH named `clauthing-*`.

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

## Example

`clauthing-titles`:
- `:titles` - show recent titles picker
- `--events` - track title changes in background
