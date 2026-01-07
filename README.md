# kitty-claude
A wrapper around claude-code, run inside kitty with tmux.

Keep all you claude codes in one place. With a distinct X11 class to select.

## Motivation
claude code has a number of features that that I want which are absent from the claude desktop app. Mostly things related to programmatic access to messages, hooks surrounding messages being sent, and the ability to run deterministic commands.

However, I will likely still want to use a terminal even with claude code and I don't want claude code using up all my terminals. So I want to wrap up claude code in a kitty window running tmux.


## Keybidings

  Tab Switching

  - Ctrl+j - Switch to previous window
  - Ctrl+k - Switch to next window
  - Alt+o - Toggle to last window (switch between current and previous)

  Other Useful Keybindings

  - Ctrl+n - Open new window
  - Ctrl+w - Close current window (won't close if it's the last one)
  - Alt+r - Restart kitty-claude


