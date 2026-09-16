"""Shared in-process MCP client helpers for the tests/mcp suites.

Both test modules drive the same SDK client (Client(server)) and assert the
same two shapes -- a structured, JSON-serialisable result and an actionable
is_error message -- so the helpers live here once instead of as a copy in
each file.
"""

from __future__ import annotations

import asyncio
import json

from mcp import Client
from mcp.server import MCPServer


async def list_tool_names(server: MCPServer) -> set[str]:
    async with Client(server) as client:
        result = await client.list_tools()
        return {tool.name for tool in result.tools}


async def call(server: MCPServer, name: str, arguments: dict | None = None):
    async with Client(server) as client:
        return await client.call_tool(name, arguments or {})


def names(server: MCPServer) -> set[str]:
    return asyncio.run(list_tool_names(server))


def payload(server: MCPServer, name: str, arguments: dict | None = None):
    """Call *name* and return its structured value.

    A tool returning a dict yields that dict; a tool returning list[dict] yields
    {"result": [...]} (the SDK's structured-output convention). Both survive a
    JSON round-trip, which is what the agent actually reads.
    """
    result = asyncio.run(call(server, name, arguments))
    assert not result.is_error, result.content
    data = json.loads(json.dumps(result.structured_content))
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def error_text(server: MCPServer, name: str, arguments: dict | None = None) -> str:
    """Call *name*, assert it failed, and return the error message."""
    result = asyncio.run(call(server, name, arguments))
    assert result.is_error, f"{name} unexpectedly succeeded: {result.content}"
    return result.content[0].text


__all__ = ["call", "error_text", "list_tool_names", "names", "payload"]
