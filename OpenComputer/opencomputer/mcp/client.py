"""
MCP client — connects to MCP servers (stdio or HTTP) and exposes their
tools via our tool registry.

Each MCP tool becomes a thin BaseTool subclass that dispatches calls back
through the live MCP session. Servers are connected lazily in the
background (kimi-cli pattern) so startup stays fast.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from opencomputer.agent.config import MCPServerConfig
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

logger = logging.getLogger("opencomputer.mcp.client")


# ─── MCPTool — one tool exposed via MCP ────────────────────────────


class MCPTool(BaseTool):
    """Tool that dispatches calls to an MCP session."""

    parallel_safe = False  # conservative — each server has its own state

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        parameters: dict[str, Any],
        session: ClientSession,
    ) -> None:
        self.server_name = server_name
        self.tool_name = tool_name
        self.description = description
        self.parameters = parameters
        self.session = session

    @property
    def schema(self) -> ToolSchema:
        # Namespace MCP tools with the server name so there's no collision
        # between multiple servers exposing a tool with the same name.
        display_name = f"{self.server_name}__{self.tool_name}"
        return ToolSchema(
            name=display_name,
            description=self.description,
            parameters=self.parameters,
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            result = await self.session.call_tool(name=self.tool_name, arguments=call.arguments)
            # Convert MCP result to our string format — concatenate text blocks
            parts: list[str] = []
            is_error = bool(getattr(result, "isError", False))
            for block in result.content or []:
                if hasattr(block, "text") and block.text:
                    parts.append(block.text)
                elif hasattr(block, "type") and block.type == "image":
                    parts.append("[image]")
                else:
                    parts.append(str(block))
            return ToolResult(
                tool_call_id=call.id,
                content="\n".join(parts) or "[empty MCP response]",
                is_error=is_error,
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                tool_call_id=call.id,
                content=f"MCP error from {self.server_name}.{self.tool_name}: {type(e).__name__}: {e}",
                is_error=True,
            )


# ─── MCPConnection — one live server connection ───────────────────


@dataclass(slots=True)
class MCPConnection:
    config: MCPServerConfig
    session: ClientSession | None = None
    exit_stack: AsyncExitStack | None = None
    tools: list[MCPTool] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tools is None:
            self.tools = []

    async def connect(self) -> bool:
        """Spin up the server process / HTTP session, initialize, cache tool list."""
        self.exit_stack = AsyncExitStack()
        try:
            if self.config.transport == "stdio":
                params = StdioServerParameters(
                    command=self.config.command,
                    args=list(self.config.args),
                    env=self.config.env or None,
                )
                stdio_ctx = stdio_client(params)
                streams = await self.exit_stack.enter_async_context(stdio_ctx)
                read_stream, write_stream = streams
            elif self.config.transport == "sse":
                # Legacy MCP HTTP transport — Server-Sent Events.
                # Use for older MCP servers that haven't migrated to streamable HTTP.
                if not self.config.url:
                    raise ValueError(f"MCP server '{self.config.name}' transport=sse requires url")
                sse_ctx = sse_client(self.config.url, headers=self.config.headers or None)
                streams = await self.exit_stack.enter_async_context(sse_ctx)
                read_stream, write_stream = streams
            elif self.config.transport == "http":
                # Modern MCP transport — streamable HTTP per spec rev 2025-03+.
                # Returns (read, write, get_session_id); ignore the third element.
                if not self.config.url:
                    raise ValueError(f"MCP server '{self.config.name}' transport=http requires url")
                http_ctx = streamablehttp_client(
                    self.config.url, headers=self.config.headers or None
                )
                streams = await self.exit_stack.enter_async_context(http_ctx)
                read_stream, write_stream, _get_sid = streams
            else:
                raise ValueError(
                    f"unknown MCP transport: {self.config.transport!r} "
                    f"(supported: stdio, sse, http)"
                )

            session = await self.exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            self.session = session

            # List + cache tools
            tool_list = await session.list_tools()
            for t in tool_list.tools:
                self.tools.append(
                    MCPTool(
                        server_name=self.config.name,
                        tool_name=t.name,
                        description=t.description or "",
                        parameters=t.inputSchema or {"type": "object", "properties": {}},
                        session=session,
                    )
                )
            logger.info(
                "MCP server '%s' connected — %d tool(s)",
                self.config.name,
                len(self.tools),
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception("MCP server '%s' failed to connect: %s", self.config.name, e)
            await self.disconnect()
            return False

    async def disconnect(self) -> None:
        if self.exit_stack is not None:
            try:
                await self.exit_stack.aclose()
            except Exception:  # noqa: BLE001
                pass
        self.exit_stack = None
        self.session = None


# ─── MCPManager — orchestrates multiple connections ───────────────


class MCPManager:
    """Manages connections to all configured MCP servers."""

    def __init__(self, tool_registry: ToolRegistry) -> None:
        self.tool_registry = tool_registry
        self.connections: list[MCPConnection] = []

    async def connect_all(self, servers: list[MCPServerConfig]) -> int:
        """Connect to every enabled server + register its tools. Returns tool count."""
        total = 0
        for cfg in servers:
            if not cfg.enabled:
                continue
            conn = MCPConnection(config=cfg)
            ok = await conn.connect()
            if not ok:
                continue
            self.connections.append(conn)
            for tool in conn.tools:
                try:
                    self.tool_registry.register(tool)
                    total += 1
                except ValueError:
                    logger.warning("MCP tool name collision (skipped): %s", tool.schema.name)
        return total

    async def shutdown(self) -> None:
        """Disconnect all servers and remove their tools from the registry."""
        for conn in self.connections:
            for tool in conn.tools:
                self.tool_registry.unregister(tool.schema.name)
            await conn.disconnect()
        self.connections.clear()

    def schedule_deferred_connect(self, servers: list[MCPServerConfig]) -> asyncio.Task[int]:
        """Start connecting in the background (kimi-cli pattern) — returns the Task."""
        return asyncio.create_task(self.connect_all(servers))


__all__ = ["MCPTool", "MCPConnection", "MCPManager"]
