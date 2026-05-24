"""Lazy loader for the MCP SDK.

The mcp package takes ~700ms to import (pydantic schema generation, anyio,
jsonschema, httpx all load eagerly). Call get_mcp() only inside functions
that are actually running an MCP server, never at module level.
"""
import types as _types

_cache = None


def get_mcp():
    """Return a namespace with the MCP symbols used across clauthing servers.

    Result is cached after the first call so subsequent calls are free.
    """
    global _cache
    if _cache is not None:
        return _cache
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp import ClientSession
    from mcp.types import Tool, TextContent
    _cache = _types.SimpleNamespace(
        Server=Server,
        stdio_server=stdio_server,
        stdio_client=stdio_client,
        StdioServerParameters=StdioServerParameters,
        ClientSession=ClientSession,
        Tool=Tool,
        TextContent=TextContent,
    )
    return _cache
