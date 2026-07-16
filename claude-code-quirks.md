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

## Config files & permissions

Claude anchors almost everything — permissions, project settings, `CLAUDE.md`, `.mcp.json` — to the **git-repo root**, which is the nearest *ancestor directory that contains a `.git`* (found by walking up from the cwd; it falls back to the start directory if there's no `.git` above). So `<repo-root>/.claude/` is repo-relative, not relative to the literal cwd — a subdirectory session still reads the root's `.claude/`. Permissions are just allow/deny/ask rules in `settings.json` files: a rule allows (runs silently) or denies (blocks), and with no rule Claude asks you. Clicking **"Yes, don't ask again"** makes Claude *write* a `Bash(...)` rule into `<repo-root>/.claude/settings.local.json` — a gitignored, machine-written file — so persistent approvals are pinned to that git repo and do **not** follow a clauthing session across `:cd`. Plain **"Yes"** (session-only) is written *nowhere*, yet a resumed conversation that previously ran approved tools tends to run them again without prompting: the effective grant rides in the **transcript** (from prior approved tool executions), which is uneditable and unqueryable. The config-dir `settings.json` (`$CLAUDE_CONFIG_DIR/settings.json`) is Claude's *user scope* — global to Claude, but clauthing points it per-session and writes it, so we label its rules `[clauthing]`. File precedence is managed > local > project > user, **except** permission rules *merge* across all scopes and any `deny` beats any `allow`. Rule patterns: `Bash(git:*)` is a prefix match, and the space before `*` enforces a word boundary (`Bash(ls *)` matches `ls -la` but not `lsof`); approving a compound command saves a *separate* rule per sub-command. There is a built-in read-only command set (e.g. file reads, `grep`) that is auto-approved without a rule. `claude -p` (headless) loads the same config as interactive unless `--bare`, but its permission behaviour differs from interactive — don't use `-p` to reason about what an interactive session will allow. Transcripts live at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` keyed to the exact cwd, so `--resume`/`--continue` only find a session from the directory it was created in.
