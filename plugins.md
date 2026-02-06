# Plugins

Plugins are executables on PATH named `kitty-claude-*`.

## Colon commands

`:foo` runs `kitty-claude-foo` with stdin/stdout connected.

## Events

Events are written to a JSONL file. Plugins subscribe via:

```bash
kitty-claude --events | kitty-claude-foo --events
```

Event types:
- `{"type": "sync", "sessions": [...]}`
- `{"type": "title_changed", "session_id": "...", "name": "..."}`
- `{"type": "session_opened", ...}`
- `{"type": "session_closed", ...}`

## Example

`kitty-claude-titles`:
- `:titles` - show recent titles picker
- `--events` - track title changes (run as daemon via pipe above)
