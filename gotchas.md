When creating the tmux.conf file used by clauthing be careful about quoting "{x}" in python format strings has a special meaning. It also has a special meaning in tmux scripts.

## ~/.claude is useless

Don't look in `~/.claude/` for session data. clauthing runs each session with its own `CLAUDE_CONFIG_DIR` under `~/.config/clauthing/session-configs/<session-id>/`. The global `.claude` directory is never used.

