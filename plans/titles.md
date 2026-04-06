# Titles Plan

**Status: Implemented**

## Goal

Track all titles ever used so we can spawn new windows with old titles.

## Current State

Titles can be set via:
- Alt+n (rename current window)
- `:spawn <title>` (new window with title)
- Changing tmux window name directly (maybe don't want this)

## What We Want

1. Record every title that gets set
2. Store title history persistently
3. Provide a picker to spawn with an old title

## Storage

File: `~/.config/kitty-claude/title-history.json`

```json
[
  {"title": "attention", "last_used": 1234567890.0, "count": 5},
  {"title": "money", "last_used": 1234567800.0, "count": 3}
]
```

## Implementation

1. Add `record_title(name)` helper function (new file or in colon_command.py)
2. Call `record_title` when:
   - `rename_session` is called (main.py)
   - `:spawn <title>` is used (colon_command.py)
3. Add `:titles` command - fzf picker of old titles, spawns with selection
4. Maybe: `:spawn` with no args shows the picker

## Files to Change

- `kitty_claude/main.py`: Call `record_title` in `rename_session`
- `kitty_claude/colon_command.py`: Add `record_title` helper, call it in `:spawn`, add `:titles` command
