We have different different state.

There is state related to tmux. This is stored in tmux-state.json
There is state related to which windows are stored. This is stored in window-state.json. window-state maps tmux windows to claude sesssions.
There is state related the session.

## TODO: kitty-claude needs to handle C-d (and other ungraceful closes) gracefully

Closing a window via kitty (cmd+w), tmux (`kill-window`), or just C-d in the
shell does not fire any clauthing hook, so `tmux-state.json` keeps the window
listed even though it is gone. Same likely applies to the instances registry
and other state files.

Repro: spawn a window, hit C-d (or close the kitty tab), then look at
`/tmp/clauthing-$UID/tmux-state.json` — the closed window is still there.

Two reasonable fixes:
1. Lazy reconciliation on read: filter `tmux-state.json` against
   `tmux list-windows` before returning. Cheap, no daemon needed.
2. Watcher: a periodic prune that drops entries whose tmux window or claude
   process is gone.

(1) is probably enough on its own.

