# Plugins Plan

**Status: Partially implemented, for future expansion**

## Motivation

Some functionality makes sense to pull out of kitty-claude itself. For this we have a plugin system.

The design is inspired by git: plugins are just executables on PATH that get run with colon commands. Simple, no special API needed.

However, some plugins need state (e.g. tracking title history, session statistics). Rather than a complicated interface to provide state to them, we use an event system. Plugins subscribe to events and maintain their own state.

## Design

### Colon Commands

`:foo` runs `kitty-claude-foo` with stdin/stdout connected.

Plugin receives environment variables:
- `KITTY_CLAUDE_SESSION_ID`
- `KITTY_CLAUDE_TMUX_SOCKET`
- etc.

### Event Subscription (Optional)

kitty-claude takes responsibility for spawning the event stream. On startup it runs:

```bash
kitty-claude --events | kitty-claude-foo --events
```

**If your plugin doesn't implement `--events`, things just work.** The colon command interface still functions - you just won't have persistent state across invocations.

Event types:
- `{"type": "title_changed", "session_id": "...", "name": "..."}`
- `{"type": "session_opened", "session_id": "...", "name": "...", "path": "..."}`
- `{"type": "session_closed", "session_id": "..."}`
- `{"type": "sync", "sessions": [...]}`

Plugins parse JSONL from stdin and maintain their own state files.

## Current Implementation

- `discover_plugins()` finds `kitty-claude-*` on PATH
- `start_plugin_pipeline()` spawns event pipelines
- `emit_event()` writes to events.jsonl
- Colon command passthrough in colon_command.py

## Future Work

- [ ] Verify plugins are started on kitty-claude startup
- [ ] Verify colon command passthrough works
- [ ] Add `:plugins` status command
- [ ] Test with a simple example plugin

## Example Plugins

- `kitty-claude-notify`: Desktop notifications on session events
- `kitty-claude-stats`: Track usage statistics
