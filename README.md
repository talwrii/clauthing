# kitty-claude
A wrapper around claude-code, run claude code inside kitty with tmux.  Keep all you claude codes in one place.

## Motivation
claude code has a number of features that that I want which are absent from the claude desktop app. Mostly things related to programmatic access to messages, hooks surrounding messages being sent, and the ability to run deterministic commands.

However, I will likely still want to use a terminal even with claude code and I don't want claude code using up all my terminals. So I want to wrap up claude code in a kitty window running tmux. I also wanted aa lot of features. Using `tmux` adds a layer of indirection necessry to automate claude code.

## Single window
I have a mode of development where I cycle through a lot of separate x11 windows. I use `--one-tab` for this.



## Keybidings

  Tab Switching

  - Ctrl+j - Switch to previous window
  - Ctrl+k - Switch to next window
  - Alt+o - Toggle to last window (switch between current and previous)

  Other Useful Keybindings

  - Ctrl+n - Open new window
  - Ctrl+w - Close current window (won't close if it's the last one)
  - Alt+r - Restart kitty-claude

## Colon commands
There are some commands implemented with hooks. Type `:help` to see the commands.

## Doublecolon skills
Claude has skills but they are stuck in the mindset of "allow claude to do the orchestration". Colon skills are a kitty-claude specific feature. If you type `::blah` the blah skill is immediately sent to claude.

To create the skill `blah` you can use `::skill blah`.


## Session Storage
Session metadata is stored in `~/.local/state/kitty-claude/sessions/` and open sessions are tracked in `~/.config/kitty-claude/open-sessions.json` (for debugging purposes only - liable to change).
