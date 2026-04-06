# Claude Code Quirks

Quirks and behaviors of Claude Code that affect how we interact with it.

## Input Timing

When sending input to Claude Code via tmux `send-keys`:

- **Delay before Enter**: Claude Code can miss rapid input. Always add a delay (0.3s+) between sending text and sending Enter.
- **Separate Enter**: Send Enter as a separate `send-keys` call, not combined with `-l` text.

```python
# Good - with delay
subprocess.run(["tmux", "-L", socket, "send-keys", "-t", pane, "-l", ":command"])
time.sleep(0.3)
subprocess.run(["tmux", "-L", socket, "send-keys", "-t", pane, "Enter"])

# Bad - too fast, may be missed
subprocess.run(["tmux", "-L", socket, "send-keys", "-t", pane, "-l", ":command"])
subprocess.run(["tmux", "-L", socket, "send-keys", "-t", pane, "Enter"])
```

This is particularly important when sending commands to multiple windows in sequence.
