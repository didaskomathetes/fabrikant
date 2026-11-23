"""Helpers shared by the MCP server tests: calls through the SDK's in-process client."""

from __future__ import annotations

import asyncio

from mcp.client import Client
from mcp.types import CallToolResult, Tool

from fabrikant import mcp_server as mcp


def call(options: mcp.McpOptions, name: str, args: dict | None = None) -> CallToolResult:
    """One tool call through the SDK's in-process client (full protocol path)."""

    async def go():
        async with Client(mcp.build_server(options)) as c:
            return await c.call_tool(name, args or {})

    return asyncio.run(go())


def list_tools(options: mcp.McpOptions) -> list[Tool]:
    """``tools/list`` of a server built with ``options``."""

    async def go():
        async with Client(mcp.build_server(options)) as c:
            return (await c.list_tools()).tools

    return asyncio.run(go())


def text(result: CallToolResult) -> str:
    """Concatenated text content of a result."""
    return "".join(getattr(c, "text", "") for c in result.content)
