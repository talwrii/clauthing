# E2E smoke tests for colon commands

## Approach

Full end-to-end: launch `clauthing --one-tab --no-kitty` via pexpect, wait for Claude to be ready, type colon commands into the tmux pane, check the pane output for expected responses. Same pattern as existing `test_proxy_mcp.py`.

## File

`tests/test_colon_commands.py`

## What to test

1. **`:help`** — type it, check pane contains "colon commands" text
2. **`:time`** — type it, check pane contains "timing" or "No timing data"
3. **`:sessions`** — type it, check pane contains "sessions" text
4. **Normal prompt** — type "hi", check Claude responds (new `>` prompt appears)

## Implementation

1. Reuse helpers from `test_proxy_mcp.py`: `find_socket`, `session_ready`, `capture_pane`, `send_keys`, `wait_for_text`, `wait_for_prompt`, `kill_server`
2. Extract shared helpers to `tests/helpers.py` so both test files can use them
3. Test flow:
   - `pexpect.spawn("clauthing --one-tab --no-kitty")`
   - Wait for tmux socket, handle trust prompt
   - Wait for Claude `>` prompt
   - Send "hi", wait for response (confirms Claude is working)
   - Send `:help`, check for "Operation stopped by hook" + help text keywords
   - Send `:time`, check for hook response with timing text
   - Send `:sessions`, check for hook response
   - Cleanup: kill tmux server

## Key files

- `tests/test_colon_commands.py` (new)
- `tests/helpers.py` (new — extracted from test_proxy_mcp.py)
- `tests/test_proxy_mcp.py` (update imports to use helpers.py)
