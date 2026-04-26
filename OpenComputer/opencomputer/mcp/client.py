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
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from opencomputer.agent.config import MCPServerConfig
from opencomputer.mcp.osv_check import check_package, has_high_severity
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.ingestion import SignalEvent
from plugin_sdk.tool_contract import BaseTool, ToolSchema

#: Narrowed connection lifecycle used by ``status_snapshot``. Mirrors
#: Kimi CLI's ``MCPServerSnapshot.status`` values.
ConnectionState = Literal["connected", "disconnected", "error"]

logger = logging.getLogger("opencomputer.mcp.client")


class MCPLaunchBlockedError(RuntimeError):
    """Raised when an OSV pre-flight scan blocks an MCP launch.

    Only thrown when :attr:`MCPConfig.osv_check_fail_closed` is set —
    the default fail-open posture logs + warns instead. Carries the
    triggering vuln list so callers can surface a useful error
    message.
    """

    def __init__(self, package: str, ecosystem: str, vulns: list[Any]) -> None:
        self.package = package
        self.ecosystem = ecosystem
        self.vulns = vulns
        ids = [v.get("id", "?") for v in vulns if isinstance(v, dict)]
        super().__init__(
            f"OSV block: {ecosystem}/{package} flagged HIGH/CRITICAL "
            f"(advisories: {', '.join(ids) or 'unknown'})"
        )


@dataclass(frozen=True, slots=True)
class _OSVSecurityEvent(SignalEvent):
    """F2 bus event emitted when OSV finds a vuln on an MCP launch path.

    Discriminator ``mcp_security.osv_hit`` lets audit subscribers
    glob-match ``mcp_security.*`` for any future security signals.
    Privacy posture: carries the package coordinates + advisory IDs +
    severity flag — never raw dependency manifests.
    """

    event_type: str = "mcp_security.osv_hit"
    package: str = ""
    ecosystem: str = ""
    server_name: str = ""
    high_severity: bool = False
    vuln_ids: tuple[str, ...] = ()
    blocked: bool = False


def _tool_is_internal(tool: Any) -> bool:
    """Return ``True`` when an MCP tool is flagged ``owner=system`` or ``internal=true``.

    P-16 sub-item (a) — internal-tool gating. MCP servers can mark a
    tool as off-limits to the agent loop by setting either flag in
    one of two MCP-spec carrier fields:

    * ``Tool._meta`` (``meta`` attribute on the pydantic model) — the
      first-class MCP extension carrier. Preferred location.
    * ``Tool.annotations`` (extra fields allowed on
      :class:`mcp.types.ToolAnnotations`) — checked too because some
      servers stash custom metadata here.

    Default behavior unchanged: tools that don't set either field
    surface to the agent like always.
    """
    for carrier_attr in ("meta", "annotations"):
        carrier = getattr(tool, carrier_attr, None)
        if carrier is None:
            continue
        if isinstance(carrier, dict):
            extras = carrier
        else:
            # pydantic models — prefer model_dump (round-trips extra="allow"
            # fields), fall back to __dict__ for plain dataclasses.
            try:
                extras = carrier.model_dump()
            except Exception:  # noqa: BLE001
                extras = getattr(carrier, "__dict__", {}) or {}
        if not isinstance(extras, dict):
            continue
        if extras.get("owner") == "system":
            return True
        if extras.get("internal") is True:
            return True
    return False


def _extract_package(cfg: MCPServerConfig) -> tuple[str, str] | None:
    """Best-effort (package, ecosystem) extraction for a stdio MCP launch.

    npx args land in shapes like ``("-y", "@scope/pkg", "...rest")`` —
    the first non-flag argument is the package. uvx args look like
    ``("pkg-name", ...)`` — the first arg is the package, ecosystem
    PyPI. Returns ``None`` when the command isn't a recognised
    package-runner so the launcher skips the check (e.g.
    user-supplied ``python my-server.py``).
    """
    cmd = (cfg.command or "").strip().lower()
    if cmd not in {"npx", "uvx"}:
        return None
    ecosystem = "npm" if cmd == "npx" else "PyPI"
    for arg in cfg.args:
        if arg.startswith("-"):
            continue
        return arg, ecosystem
    return None


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
    tools: list[MCPTool] = field(default_factory=list)
    #: Lifecycle state used by :meth:`MCPManager.status_snapshot` (IV.4).
    #: Starts ``disconnected``; flips to ``connected`` after a successful
    #: ``connect()``, ``error`` on failure, and back to ``disconnected``
    #: after ``disconnect()``.
    state: ConnectionState = "disconnected"
    #: Server's self-reported version from MCP ``initialize`` response.
    version: str | None = None
    #: Monotonic timestamp of last successful connect (for uptime math).
    connect_time: float | None = None
    #: Latest connect-time error message, ``None`` when healthy.
    last_error: str | None = None

    def _osv_pre_flight(self, *, fail_closed: bool) -> str | None:
        """Run the OSV pre-flight check; return an error string if blocking.

        Returns ``None`` when the launch should proceed (clean OR
        fail-open warn-and-allow). Returns a short error message when
        ``fail_closed`` is set and a HIGH-severity advisory matched.

        Always emits ``mcp_security.osv_hit`` on the F2 bus when any
        vulns are found, regardless of severity, so audit subscribers
        get visibility on every signal — not just the blocking ones.
        """
        package_info = _extract_package(self.config)
        if package_info is None:
            return None
        package, ecosystem = package_info
        try:
            result = check_package(package, ecosystem)
        except Exception as exc:  # noqa: BLE001 — must not break launch
            logger.warning(
                "OSV pre-flight raised for %s/%s: %s — fail-open",
                ecosystem,
                package,
                exc,
            )
            return None
        vulns = result.get("vulns", []) or []
        if not vulns:
            return None
        high = has_high_severity(vulns)
        ids = tuple(v.get("id", "?") for v in vulns if isinstance(v, dict))
        # Lazy bus import — keeps a broken bus singleton from poisoning
        # MCP module imports during pytest collection.
        try:
            from opencomputer.ingestion.bus import default_bus

            default_bus.publish(
                _OSVSecurityEvent(
                    source="mcp.client",
                    package=package,
                    ecosystem=ecosystem,
                    server_name=self.config.name,
                    high_severity=high,
                    vuln_ids=ids,
                    blocked=bool(high and fail_closed),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("OSV bus publish failed (continuing): %s", exc)
        if high and fail_closed:
            return (
                f"OSV blocked launch: {ecosystem}/{package} "
                f"({', '.join(ids) or 'unknown'})"
            )
        if high:
            logger.warning(
                "OSV pre-flight HIGH severity for %s/%s (%s) — allowing "
                "(fail-open posture; set MCPConfig.osv_check_fail_closed "
                "to refuse)",
                ecosystem,
                package,
                ", ".join(ids),
            )
        else:
            logger.info(
                "OSV pre-flight non-high advisory for %s/%s (%s) — allowing",
                ecosystem,
                package,
                ", ".join(ids),
            )
        return None

    async def connect(
        self,
        *,
        osv_check_enabled: bool = True,
        osv_check_fail_closed: bool = False,
    ) -> bool:
        """Spin up the server process / HTTP session, initialize, cache tool list.

        Parameters
        ----------
        osv_check_enabled:
            When ``True`` (default) and the server is launched via
            ``npx``/``uvx``, run an OSV pre-flight scan of the package.
            HIGH/CRITICAL hits emit a ``mcp_security.osv_hit`` event on
            the F2 bus.
        osv_check_fail_closed:
            When ``True``, a HIGH-severity OSV hit refuses the launch
            (returns ``False`` after recording an error). Default
            ``False`` (warn-and-allow) keeps a transient OSV outage
            from breaking MCP startup.
        """
        self.exit_stack = AsyncExitStack()
        try:
            if self.config.transport == "stdio":
                if osv_check_enabled:
                    blocked = self._osv_pre_flight(fail_closed=osv_check_fail_closed)
                    if blocked is not None:
                        # blocked == True means fail-closed refused the
                        # launch; surface as a connect error and bail.
                        self.state = "error"
                        self.last_error = blocked
                        await self.disconnect(_preserve_error_state=True)
                        return False
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
            init_result = await session.initialize()
            # Capture server version from the InitializeResult.serverInfo block
            # when present. Kept defensive — custom servers or mocks may not
            # expose the nested attribute.
            try:
                server_info = getattr(init_result, "serverInfo", None)
                self.version = getattr(server_info, "version", None) if server_info else None
            except Exception:  # noqa: BLE001
                self.version = None
            self.session = session

            # List + cache tools. Internal tools (owner=system OR
            # internal=true) are filtered here so the agent never sees
            # them in its schema. P-16 sub-item (a).
            tool_list = await session.list_tools()
            hidden = 0
            for t in tool_list.tools:
                if _tool_is_internal(t):
                    hidden += 1
                    logger.debug(
                        "MCP server '%s' tool '%s' hidden (internal/system)",
                        self.config.name,
                        t.name,
                    )
                    continue
                self.tools.append(
                    MCPTool(
                        server_name=self.config.name,
                        tool_name=t.name,
                        description=t.description or "",
                        parameters=t.inputSchema or {"type": "object", "properties": {}},
                        session=session,
                    )
                )
            if hidden:
                logger.info(
                    "MCP server '%s' suppressed %d internal tool(s)",
                    self.config.name,
                    hidden,
                )
            self.state = "connected"
            self.connect_time = time.monotonic()
            self.last_error = None
            logger.info(
                "MCP server '%s' connected — %d tool(s)",
                self.config.name,
                len(self.tools),
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception("MCP server '%s' failed to connect: %s", self.config.name, e)
            self.state = "error"
            self.last_error = f"{type(e).__name__}: {e}"
            await self.disconnect(_preserve_error_state=True)
            return False

    async def disconnect(self, *, _preserve_error_state: bool = False) -> None:
        if self.exit_stack is not None:
            try:
                await self.exit_stack.aclose()
            except Exception:  # noqa: BLE001
                pass
        self.exit_stack = None
        self.session = None
        # Keep ``error`` state visible to snapshot consumers so a failed
        # connect can still be diagnosed. A clean disconnect flips to
        # ``disconnected``.
        if not _preserve_error_state:
            self.state = "disconnected"
        self.connect_time = None


# ─── MCPManager — orchestrates multiple connections ───────────────


class MCPManager:
    """Manages connections to all configured MCP servers."""

    def __init__(self, tool_registry: ToolRegistry) -> None:
        self.tool_registry = tool_registry
        self.connections: list[MCPConnection] = []

    async def connect_all(
        self,
        servers: list[MCPServerConfig],
        *,
        osv_check_enabled: bool = True,
        osv_check_fail_closed: bool = False,
    ) -> int:
        """Connect to every enabled server + register its tools. Returns tool count.

        ``osv_check_enabled`` and ``osv_check_fail_closed`` are passed
        straight through to each :meth:`MCPConnection.connect` call so
        callers can plumb :class:`MCPConfig` flags without per-server
        threading.
        """
        total = 0
        for cfg in servers:
            if not cfg.enabled:
                continue
            conn = MCPConnection(config=cfg)
            ok = await conn.connect(
                osv_check_enabled=osv_check_enabled,
                osv_check_fail_closed=osv_check_fail_closed,
            )
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

    def schedule_deferred_connect(
        self,
        servers: list[MCPServerConfig],
        *,
        osv_check_enabled: bool = True,
        osv_check_fail_closed: bool = False,
    ) -> asyncio.Task[int]:
        """Start connecting in the background (kimi-cli pattern) — returns the Task."""
        return asyncio.create_task(
            self.connect_all(
                servers,
                osv_check_enabled=osv_check_enabled,
                osv_check_fail_closed=osv_check_fail_closed,
            )
        )

    def status_snapshot(self) -> list[dict[str, Any]]:
        """Return a diagnostic snapshot of every tracked MCP connection (IV.4).

        Shape per entry::

            {
                "name": str,
                "url": str,
                "version": str | None,
                "tool_count": int,
                "tools": list[str],
                "connection_state": "connected" | "disconnected" | "error",
                "last_error": str | None,
                "uptime_sec": float | None,
            }

        Mirrors Kimi CLI's ``mcp_status_snapshot`` at
        ``sources/kimi-cli/src/kimi_cli/soul/toolset.py`` line 277 — same
        intent (read-only diagnostic view), adapted to our dict return
        shape so the CLI layer can render it with Rich.

        ``url`` for stdio servers is synthesized from ``command + args``
        since those servers have no real URL — lets the CLI table show
        something useful for every transport.
        """
        snap: list[dict[str, Any]] = []
        now = time.monotonic()
        for conn in self.connections:
            cfg = conn.config
            if cfg.transport == "stdio":
                target = (
                    f"{cfg.command} {' '.join(cfg.args)}".strip()
                    if cfg.command
                    else ""
                )
            else:
                target = cfg.url
            uptime: float | None
            if conn.connect_time is not None and conn.state == "connected":
                uptime = max(0.0, now - conn.connect_time)
            else:
                uptime = None
            snap.append(
                {
                    "name": cfg.name,
                    "url": target,
                    "version": conn.version,
                    "tool_count": len(conn.tools),
                    "tools": [t.tool_name for t in conn.tools],
                    "connection_state": conn.state,
                    "last_error": conn.last_error,
                    "uptime_sec": uptime,
                }
            )
        return snap


__all__ = [
    "MCPTool",
    "MCPConnection",
    "MCPManager",
    "ConnectionState",
    "MCPLaunchBlockedError",
]
