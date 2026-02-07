Install with --editable and pipx by default. Therefore we only need to reinstall if we add new entry points.

## Running Tests

```bash
# Run unit tests (excludes tests needing mcp module)
python3 -m pytest tests/test_session_utils.py -v

# Run all tests (requires mcp module installed)
python3 -m pytest tests/ -v
```

Test files:
- `tests/test_session_utils.py` - Session utility functions
- `tests/test_skills_mcp.py` - Skills MCP server (requires `mcp` module)
- `tests/test_proxy_mcp.py` - Proxy MCP server
