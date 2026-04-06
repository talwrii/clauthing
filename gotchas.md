When creating the tmux.conf file used by kitty-claude be careful about quoting "{x}" in python format strings has a special meaning. It also has a special meaning in tmux scripts.

## ~/.claude is useless

Don't look in `~/.claude/` for session data. kitty-claude runs each session with its own `CLAUDE_CONFIG_DIR` under `~/.config/kitty-claude/session-configs/<session-id>/`. The global `.claude` directory is never used.

