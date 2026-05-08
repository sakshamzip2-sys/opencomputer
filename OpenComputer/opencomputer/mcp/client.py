"""
MCP client — connects to MCP servers (stdio or HTTP) and exposes their
tools via our tool registry.

Each MCP tool becomes a thin BaseTool subclass that dispatches calls back
through the live MCP session. Servers are connected lazily in the
background (kimi-cli pattern) so startup stays fast.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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


#: Hermes-parity whitelist of parent env vars that MCP stdio subprocesses
#: are allowed to inherit. Any other key — including everything that looks
#: like a secret (API keys, tokens, OAuth credentials) — is stripped before
#: spawn. ``XDG_*`` keys are admitted as a regex (see
#: :func:`_build_mcp_subprocess_env`).
#:
#: Per-server ``env:`` declarations in ``mcp_servers.<name>.env`` config
#: still pass through (caller intent — that's the whole point of the
#: explicit declaration).
_MCP_SAFE_ENV_KEYS: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR",
})


def _build_mcp_subprocess_env(
    parent_env: dict[str, str],
    declared_env: dict[str, str] | None,
) -> dict[str, str]:
    """Return the env dict an MCP stdio subprocess should receive.

    Hermes-parity strict filter: only ``_MCP_SAFE_ENV_KEYS`` and any
    ``XDG_*`` key from the parent env are admitted; everything else
    (API keys, OAuth tokens, gateway credentials) is stripped.
    Per-server ``declared_env`` (from ``mcp_servers.<name>.env``)
    layers on top — this is explicit caller intent and IS allowed
    through, since the user typed it into config.yaml deliberately.

    Args:
        parent_env: snapshot of the parent process's env (typically
            ``os.environ.copy()``).
        declared_env: explicit env list from the MCP server's config
            (e.g., ``GITHUB_PERSONAL_ACCESS_TOKEN: ghp_...``). May be
            ``None`` or empty.

    Returns:
        A new dict with the filtered env. Safe to pass directly to
        ``StdioServerParameters(env=...)``.
    """
    out: dict[str, str] = {
        k: v
        for k, v in parent_env.items()
        if k in _MCP_SAFE_ENV_KEYS or k.startswith("XDG_")
    }
    if declared_env:
        out.update(declared_env)
    return out


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


def _passes_tool_filter(tool_name: str, cfg: MCPServerConfig) -> bool:
    """Apply the per-server ``tools_allow`` / ``tools_deny`` filter (Wave 3).

    Allow-list semantics:
    - ``tools_allow=None`` (default) — no filter, every name passes.
    - ``tools_allow=()`` (empty tuple) — deny all (no name matches an
      empty allow-list; this is the intuitive reading).
    - ``tools_allow=("a", "b")`` — only those names pass.

    Deny-list applies AFTER allow-list. ``tools_deny=()`` (default) is a
    no-op.
    """
    if cfg.tools_allow is not None and tool_name not in cfg.tools_allow:
        return False
    return not (cfg.tools_deny and tool_name in cfg.tools_deny)


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
        timeout: float = 30.0,
    ) -> None:
        self.server_name = server_name
        self.tool_name = tool_name
        self.description = description
        self.parameters = parameters
        self.session = session
        # G10 (Hermes parity, 2026-05-09): per-tool-call timeout cap.
        self.timeout = timeout

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
        # Hermes-parity MCP credential redaction. MCP server output and
        # exception strings can contain GitHub PATs, OpenAI-style keys,
        # bearer tokens, postgres URLs etc. Pipe everything through the
        # central redaction module BEFORE returning to the LLM.
        from opencomputer.security.redact import redact_runtime_text

        try:
            # G10 (Hermes parity, 2026-05-09): cap the per-tool-call wait
            # so a wedged MCP server can't block the agent loop forever.
            result = await asyncio.wait_for(
                self.session.call_tool(
                    name=self.tool_name, arguments=call.arguments
                ),
                timeout=self.timeout,
            )
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
            content = "\n".join(parts) or "[empty MCP response]"
            return ToolResult(
                tool_call_id=call.id,
                content=redact_runtime_text(content),
                is_error=is_error,
            )
        except Exception as e:  # noqa: BLE001
            err = (
                f"MCP error from {self.server_name}.{self.tool_name}: "
                f"{type(e).__name__}: {e}"
            )
            return ToolResult(
                tool_call_id=call.id,
                content=redact_runtime_text(err),
                is_error=True,
            )


def hermes_alias_name(server_name: str, tool_name: str) -> str:
    """Hermes-spec MCP tool name: ``mcp_<server>_<tool>`` (G8 — 2026-05-09).

    OpenComputer's canonical form is ``<server>__<tool>`` (double underscore,
    set in :class:`MCPTool.schema`). This helper produces the Hermes-spec
    form for clients that key off it. Both names are registered side-by-side
    via :class:`MCPAliasTool`, which keeps third-party tools written against
    either spec discovering the toolset correctly.
    """
    return f"mcp_{server_name}_{tool_name}"


class MCPAliasTool(BaseTool):
    """Hermes-spec name alias for an :class:`MCPTool` (G8 — 2026-05-09).

    Re-publishes a canonical :class:`MCPTool` under the Hermes-spec name
    (``mcp_<server>_<tool>``) without duplicating the underlying MCP
    session call. ``execute`` is a thin pass-through to the canonical
    tool's dispatch path; both names invoke the same MCP server tool.

    Avoids :class:`ToolRegistry` ``schema_name`` collision because each
    alias has a distinct schema name from its canonical sibling.
    """

    parallel_safe = False  # mirrors MCPTool's conservative posture

    def __init__(self, canonical: "MCPTool") -> None:
        self._canonical = canonical

    @property
    def schema(self) -> ToolSchema:
        base = self._canonical.schema
        return ToolSchema(
            name=hermes_alias_name(
                self._canonical.server_name, self._canonical.tool_name
            ),
            description=base.description,
            parameters=base.parameters,
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        return await self._canonical.execute(call)


# ─── T1 — MCP utility tools (resources / prompts) ─────────────────
#
# Hermes-doc parity: when an MCP server's ``initialize`` reply
# advertises the ``resources`` capability, expose two helper tools
# (``<server>__list_resources`` + ``<server>__read_resource``) so the
# agent can enumerate + fetch resources without the server having to
# wrap them as tools. Same for ``prompts``.


def _serialize_resource(r: Any) -> dict[str, Any]:
    """Lift an mcp.types.Resource into a JSON-safe dict."""
    return {
        "uri": getattr(r, "uri", None),
        "name": getattr(r, "name", None),
        "description": getattr(r, "description", None),
        "mimeType": getattr(r, "mimeType", None),
    }


def _serialize_prompt(p: Any) -> dict[str, Any]:
    return {
        "name": getattr(p, "name", None),
        "description": getattr(p, "description", None),
        "arguments": [
            {
                "name": getattr(a, "name", None),
                "description": getattr(a, "description", None),
                "required": getattr(a, "required", False),
            }
            for a in (getattr(p, "arguments", None) or [])
        ],
    }


class _MCPListResourcesTool(BaseTool):
    """``<server>__list_resources`` — enumerate the server's resources."""

    parallel_safe = True

    def __init__(self, server_name: str, session: Any) -> None:
        self._server_name = server_name
        self._session = session

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=f"{self._server_name}__list_resources",
            description=(
                f"List resources exposed by MCP server '{self._server_name}'. "
                "Returns a JSON array of {uri, name, description, mimeType}."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        from opencomputer.security.redact import redact_runtime_text

        try:
            result = await self._session.list_resources()
            payload = [_serialize_resource(r) for r in (getattr(result, "resources", None) or [])]
            return ToolResult(
                tool_call_id=call.id,
                content=redact_runtime_text(json.dumps(payload)),
                is_error=False,
            )
        except Exception as e:  # noqa: BLE001
            err = f"MCP utility error from {self._server_name}.list_resources: {type(e).__name__}: {e}"
            return ToolResult(
                tool_call_id=call.id,
                content=redact_runtime_text(err),
                is_error=True,
            )


class _MCPReadResourceTool(BaseTool):
    """``<server>__read_resource(uri)`` — fetch one resource's contents."""

    parallel_safe = True

    def __init__(self, server_name: str, session: Any) -> None:
        self._server_name = server_name
        self._session = session

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=f"{self._server_name}__read_resource",
            description=f"Read a resource by URI from MCP server '{self._server_name}'.",
            parameters={
                "type": "object",
                "properties": {
                    "uri": {"type": "string", "description": "Resource URI"},
                },
                "required": ["uri"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        from opencomputer.security.redact import redact_runtime_text

        uri = call.arguments.get("uri")
        if not uri:
            return ToolResult(
                tool_call_id=call.id,
                content="Missing required argument 'uri'.",
                is_error=True,
            )
        try:
            result = await self._session.read_resource(uri)
            contents = getattr(result, "contents", None) or []
            payload = {
                "contents": [
                    {"uri": getattr(c, "uri", None), "text": getattr(c, "text", None)}
                    for c in contents
                ],
            }
            return ToolResult(
                tool_call_id=call.id,
                content=redact_runtime_text(json.dumps(payload)),
                is_error=False,
            )
        except Exception as e:  # noqa: BLE001
            err = f"MCP utility error from {self._server_name}.read_resource: {type(e).__name__}: {e}"
            return ToolResult(
                tool_call_id=call.id,
                content=redact_runtime_text(err),
                is_error=True,
            )


class _MCPListPromptsTool(BaseTool):
    """``<server>__list_prompts`` — enumerate prompts the server offers."""

    parallel_safe = True

    def __init__(self, server_name: str, session: Any) -> None:
        self._server_name = server_name
        self._session = session

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=f"{self._server_name}__list_prompts",
            description=(
                f"List prompts exposed by MCP server '{self._server_name}'. "
                "Returns a JSON array of {name, description, arguments}."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        from opencomputer.security.redact import redact_runtime_text

        try:
            result = await self._session.list_prompts()
            payload = [_serialize_prompt(p) for p in (getattr(result, "prompts", None) or [])]
            return ToolResult(
                tool_call_id=call.id,
                content=redact_runtime_text(json.dumps(payload)),
                is_error=False,
            )
        except Exception as e:  # noqa: BLE001
            err = f"MCP utility error from {self._server_name}.list_prompts: {type(e).__name__}: {e}"
            return ToolResult(
                tool_call_id=call.id,
                content=redact_runtime_text(err),
                is_error=True,
            )


class _MCPGetPromptTool(BaseTool):
    """``<server>__get_prompt(name, arguments?)`` — render a server prompt."""

    parallel_safe = True

    def __init__(self, server_name: str, session: Any) -> None:
        self._server_name = server_name
        self._session = session

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=f"{self._server_name}__get_prompt",
            description=f"Get a prompt by name from MCP server '{self._server_name}'.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Prompt name"},
                    "arguments": {
                        "type": "object",
                        "description": "Prompt template arguments (optional).",
                    },
                },
                "required": ["name"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        from opencomputer.security.redact import redact_runtime_text

        name = call.arguments.get("name")
        if not name:
            return ToolResult(
                tool_call_id=call.id,
                content="Missing required argument 'name'.",
                is_error=True,
            )
        arguments = call.arguments.get("arguments")
        try:
            result = await self._session.get_prompt(name, arguments=arguments)
            messages = getattr(result, "messages", None) or []
            payload = {"messages": [m if isinstance(m, dict) else dict(m) for m in messages]}
            return ToolResult(
                tool_call_id=call.id,
                content=redact_runtime_text(json.dumps(payload, default=str)),
                is_error=False,
            )
        except Exception as e:  # noqa: BLE001
            err = f"MCP utility error from {self._server_name}.get_prompt: {type(e).__name__}: {e}"
            return ToolResult(
                tool_call_id=call.id,
                content=redact_runtime_text(err),
                is_error=True,
            )


def _build_utility_tools(
    server_name: str,
    session: Any,
    capabilities: dict[str, Any] | None,
    *,
    prompts_enabled: bool = True,
    resources_enabled: bool = True,
) -> list[BaseTool]:
    """Return MCP resource/prompt utility tools, capability-gated.

    ``capabilities`` is the ``capabilities`` block of the MCP server's
    ``initialize`` reply (or ``None`` / empty dict when nothing was
    advertised). Each present capability adds 2 tools.

    G9 (Hermes parity, 2026-05-09): per-server ``prompts_enabled`` /
    ``resources_enabled`` suppress the corresponding utility tools even
    when the server advertises the capability. Default is to register
    both, matching the prior behavior.
    """
    tools: list[BaseTool] = []
    if not capabilities:
        return tools
    # An MCP capability is "advertised" when its key is present with a
    # non-None value (the spec uses an empty object ``{}`` to mean
    # "supported, no sub-features").
    if resources_enabled and capabilities.get("resources") is not None:
        tools.append(_MCPListResourcesTool(server_name=server_name, session=session))
        tools.append(_MCPReadResourceTool(server_name=server_name, session=session))
    if prompts_enabled and capabilities.get("prompts") is not None:
        tools.append(_MCPListPromptsTool(server_name=server_name, session=session))
        tools.append(_MCPGetPromptTool(server_name=server_name, session=session))
    return tools


# ─── MCPConnection — one live server connection ───────────────────


@dataclass(slots=True)
class MCPConnection:
    config: MCPServerConfig
    session: ClientSession | None = None
    exit_stack: AsyncExitStack | None = None
    tools: list[BaseTool] = field(default_factory=list)
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
    #: T3 (tier-2 trio, 2026-05-04). Monotonic ts of last health probe.
    last_health_check_at: float | None = None
    #: T3 — count of reconnect attempts within current 60s window. Cap 3.
    reconnect_attempts: int = 0
    #: T3 — start of current 60s reconnect window (monotonic).
    reconnect_window_start: float | None = None
    #: Hermes-doc dynamic tool discovery — fired when the server pushes
    #: ``notifications/tools/list_changed`` and reconciliation has updated
    #: ``self.tools``. Callback receives ``(added: list[BaseTool],
    #: removed: list[BaseTool])`` so the registry can sync. ``MCPManager``
    #: wires this in :meth:`MCPManager.connect_all`. Synchronous — must
    #: not block on the asyncio loop.
    tools_changed_callback: Any = None
    #: T3 (dynamic discovery) — true while reconciliation is in flight to
    #: prevent re-entrant reconcile races on rapid notification bursts.
    _reconcile_in_flight: bool = False

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
                # Strict env filter (Hermes parity): only safe parent
                # vars + XDG_* + per-MCP declared env reach the
                # subprocess. Then profile-scope HOME / XDG_* so MCP
                # servers get per-profile credential isolation
                # (git/ssh/npm caches).
                try:
                    from opencomputer.profiles import (
                        read_active_profile,
                        scope_subprocess_env,
                    )

                    filtered = _build_mcp_subprocess_env(
                        dict(os.environ), self.config.env,
                    )
                    spawn_env = scope_subprocess_env(
                        filtered, profile=read_active_profile()
                    )
                except Exception:
                    # Defensive — never block an MCP launch on profile
                    # lookup edge cases. Fall back to the strict filter
                    # alone (still secret-safe).
                    spawn_env = _build_mcp_subprocess_env(
                        dict(os.environ), self.config.env,
                    )
                params = StdioServerParameters(
                    command=self.config.command,
                    args=list(self.config.args),
                    env=spawn_env,
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

            # T71 — sampling/createMessage host bridge. Lets MCP servers
            # ask US to run LLM completions; we route through aux_llm so
            # the configured provider (+ fallback chain) handles them.
            from opencomputer.mcp.sampling import make_sampling_callback

            session = await self.exit_stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    message_handler=self._handle_session_message,
                    sampling_callback=make_sampling_callback(),
                )
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
            filtered = 0
            for t in tool_list.tools:
                if _tool_is_internal(t):
                    hidden += 1
                    logger.debug(
                        "MCP server '%s' tool '%s' hidden (internal/system)",
                        self.config.name,
                        t.name,
                    )
                    continue
                # Wave 3 (2026-05-08) — per-server tools_allow / tools_deny.
                if not _passes_tool_filter(t.name, self.config):
                    filtered += 1
                    logger.debug(
                        "MCP server '%s' tool '%s' filtered by tools_allow/tools_deny",
                        self.config.name,
                        t.name,
                    )
                    continue
                tool_obj = MCPTool(
                    server_name=self.config.name,
                    tool_name=t.name,
                    description=t.description or "",
                    parameters=t.inputSchema or {"type": "object", "properties": {}},
                    session=session,
                    timeout=self.config.timeout,
                )
                self.tools.append(tool_obj)
                # G8 (Hermes parity, 2026-05-09): also register the
                # spec-named ``mcp_<server>_<tool>`` alias as a sibling.
                self.tools.append(MCPAliasTool(tool_obj))
            if hidden:
                logger.info(
                    "MCP server '%s' suppressed %d internal tool(s)",
                    self.config.name,
                    hidden,
                )
            if filtered:
                logger.info(
                    "MCP server '%s' filtered %d tool(s) per tools_allow/tools_deny",
                    self.config.name,
                    filtered,
                )

            # T1 — register Hermes-doc utility tools when the server
            # advertises ``resources`` / ``prompts`` capabilities. The
            # ServerCapabilities object exposes one attribute per
            # capability; presence (non-None) means "supported."
            try:
                caps_obj = getattr(init_result, "capabilities", None)
                cap_dict: dict[str, Any] = {
                    "resources": getattr(caps_obj, "resources", None),
                    "prompts": getattr(caps_obj, "prompts", None),
                }
                for utility in _build_utility_tools(
                    self.config.name,
                    session,
                    cap_dict,
                    prompts_enabled=self.config.prompts_enabled,
                    resources_enabled=self.config.resources_enabled,
                ):
                    self.tools.append(utility)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "MCP server '%s' utility-tool registration skipped (defensive)",
                    self.config.name,
                    exc_info=True,
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

    async def _handle_session_message(self, message: Any) -> None:
        """Receive notifications from the MCP server.

        Hermes-doc dynamic discovery: when a ``notifications/tools/list_changed``
        arrives, schedule a tool-list reconciliation in the background.
        Other notifications (logging/progress/etc.) are ignored — the
        SDK's default handler already logs them at debug level.
        """
        # Lazy-import to avoid a hard dep on mcp.types in unit tests that
        # mock MCPConnection's internals.
        try:
            from mcp.types import (
                ServerNotification,
                ToolListChangedNotification,
            )
        except Exception:  # noqa: BLE001
            return
        # Notifications arrive wrapped in a ServerNotification union; the
        # inner ``root`` carries the typed notification.
        notif = getattr(message, "root", message)
        if isinstance(message, ServerNotification):
            notif = message.root
        if isinstance(notif, ToolListChangedNotification):
            if self._reconcile_in_flight:
                return
            self._reconcile_in_flight = True
            asyncio.create_task(self._reconcile_tools_safely())

    async def _reconcile_tools_safely(self) -> None:
        try:
            await self._reconcile_tools()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "MCP server '%s' reconcile failed: %s",
                self.config.name,
                exc,
            )
        finally:
            self._reconcile_in_flight = False

    async def _reconcile_tools(self) -> None:
        """Re-fetch tools from the live session and diff against ``self.tools``.

        Notifies ``tools_changed_callback`` with ``(added, removed)`` so
        the registry can sync. Internal tools and per-server filter
        rules are honored.
        """
        if self.session is None:
            return
        tool_list = await self.session.list_tools()
        # Build the desired tool set with the same filter rules as connect().
        new_tools_by_name: dict[str, BaseTool] = {}
        allow = self.config.tools_allow
        deny = self.config.tools_deny or ()
        for t in tool_list.tools:
            if _tool_is_internal(t):
                continue
            if allow is not None and t.name not in allow:
                continue
            if t.name in deny:
                continue
            new_tools_by_name[t.name] = MCPTool(
                server_name=self.config.name,
                tool_name=t.name,
                description=t.description or "",
                parameters=t.inputSchema or {"type": "object", "properties": {}},
                session=self.session,
                timeout=self.config.timeout,
            )
        old_tools_by_name = {tool.tool_name: tool for tool in self.tools if isinstance(tool, MCPTool)}
        added_names = set(new_tools_by_name) - set(old_tools_by_name)
        removed_names = set(old_tools_by_name) - set(new_tools_by_name)
        added = [new_tools_by_name[n] for n in added_names]
        removed = [old_tools_by_name[n] for n in removed_names]
        # Update self.tools — preserve utility tools (resources/prompts)
        # because list_changed only signals tool-list changes.
        # G8 (2026-05-09): MCPAliasTool entries are regenerated below
        # so they always point at the live canonical MCPTool.
        utility_tools = [
            t
            for t in self.tools
            if not isinstance(t, (MCPTool, MCPAliasTool))
        ]
        # Keep tools that survived (in old + new) plus the new ones.
        survivors = [
            old_tools_by_name[n] for n in (set(old_tools_by_name) & set(new_tools_by_name))
        ]
        canonical_after = survivors + added
        # G8: re-issue Hermes-spec aliases for every canonical tool.
        aliases = [MCPAliasTool(t) for t in canonical_after]
        self.tools = canonical_after + aliases + utility_tools
        if added or removed:
            logger.info(
                "MCP server '%s' tools/list_changed: +%d -%d",
                self.config.name,
                len(added),
                len(removed),
            )
            cb = self.tools_changed_callback
            if cb is not None:
                try:
                    cb(self, added, removed)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "MCP server '%s' tools_changed_callback failed: %s",
                        self.config.name,
                        exc,
                    )

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

    # T3 (tier-2 trio, 2026-05-04) — health probe + auto-reconnect

    async def _probe_alive(self) -> None:
        """Cheap liveness probe — calls list_tools() on the active session.

        Raises if the server isn't responsive. Override in tests via
        ``monkeypatch.setattr(MCPConnection, "_probe_alive", ...)``.
        """
        if self.session is None:
            raise RuntimeError("no active session")
        await self.session.list_tools()

    async def health_check(self) -> bool:
        """Probe the server. Marks state='error' on failure. Returns alive bool.

        Always sets :attr:`last_health_check_at`. Skips the probe (returns
        False) when the connection isn't currently in ``connected`` state.
        """
        self.last_health_check_at = time.monotonic()
        if self.state != "connected":
            return False
        try:
            await self._probe_alive()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "MCP %s health probe failed: %s", self.config.name, exc
            )
            self.state = "error"
            self.last_error = str(exc)
            return False

    async def attempt_reconnect(
        self,
        *,
        osv_check_enabled: bool = True,
        osv_check_fail_closed: bool = False,
    ) -> bool:
        """Try one reconnect with exponential backoff. Caps at 3/60s/server.

        Returns True if reconnect succeeded, False if rate-limited or the
        underlying ``connect()`` returned False.
        """
        now = time.monotonic()

        if (
            self.reconnect_window_start is None
            or now - self.reconnect_window_start > 60.0
        ):
            self.reconnect_window_start = now
            self.reconnect_attempts = 0

        if self.reconnect_attempts >= 3:
            logger.warning(
                "MCP %s reconnect rate-limited (3/min)", self.config.name
            )
            return False

        self.reconnect_attempts += 1
        backoff = 2 ** self.reconnect_attempts  # 2s, 4s, 8s
        logger.info(
            "MCP %s reconnect attempt %d (backoff %ds)",
            self.config.name,
            self.reconnect_attempts,
            backoff,
        )
        await asyncio.sleep(backoff)

        try:
            await self.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return await self.connect(
            osv_check_enabled=osv_check_enabled,
            osv_check_fail_closed=osv_check_fail_closed,
        )


# ─── MCPManager — orchestrates multiple connections ───────────────


class MCPManager:
    """Manages connections to all configured MCP servers."""

    def __init__(self, tool_registry: ToolRegistry) -> None:
        self.tool_registry = tool_registry
        self.connections: list[MCPConnection] = []
        # T1 (mcp-deferrals-v2, 2026-05-04) — periodic background health probe.
        # Started explicitly via :meth:`start_health_loop`; not implicit on
        # construction so CLI one-shot mode skips it.
        self._health_loop_task: asyncio.Task[None] | None = None

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
            conn = MCPConnection(
                config=cfg,
                tools_changed_callback=self._on_connection_tools_changed,
            )
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

    def _on_connection_tools_changed(
        self,
        conn: MCPConnection,
        added: list[BaseTool],
        removed: list[BaseTool],
    ) -> None:
        """Sync the tool registry when an MCP server pushes tools/list_changed.

        Called synchronously from the connection's reconcile loop. Both
        registry mutations (unregister + register) are best-effort;
        unknown-name unregister is logged but not raised.
        """
        for tool in removed:
            try:
                self.tool_registry.unregister(tool.schema.name)
            except KeyError:
                logger.debug(
                    "MCP server '%s' removed tool %s already absent from registry",
                    conn.config.name,
                    tool.schema.name,
                )
        for tool in added:
            try:
                self.tool_registry.register(tool)
            except ValueError:
                logger.warning(
                    "MCP server '%s' tool name collision on dynamic add: %s",
                    conn.config.name,
                    tool.schema.name,
                )

    async def shutdown(self) -> None:
        """Disconnect all servers and remove their tools from the registry."""
        for conn in self.connections:
            for tool in conn.tools:
                self.tool_registry.unregister(tool.schema.name)
            await conn.disconnect()
        self.connections.clear()

    async def health_check_all(self) -> None:
        """Probe every currently-connected MCP server.

        T3 (tier-2 trio, 2026-05-04). Iterates :attr:`connections`,
        skipping any that aren't in ``connected`` state. A failed probe
        flips that connection's ``state`` to ``error`` (via
        :meth:`MCPConnection.health_check`). Callers that want to retry
        unhealthy servers should follow up with
        :meth:`MCPConnection.attempt_reconnect`.
        """
        for conn in self.connections:
            if conn.state != "connected":
                continue
            await conn.health_check()

    def start_health_loop(self, interval_seconds: float = 30.0) -> asyncio.Task[None]:
        """Spawn a background task that calls :meth:`health_check_all`
        every ``interval_seconds``.

        T1 (mcp-deferrals-v2, 2026-05-04). Idempotent: returns the
        existing task if already started. Exceptions inside the loop body
        are caught + logged so one bad probe doesn't crash the loop.

        Callers (typically the gateway daemon) should pair with
        :meth:`stop_health_loop` on shutdown.
        """
        if self._health_loop_task is not None and not self._health_loop_task.done():
            return self._health_loop_task

        async def _loop() -> None:
            while True:
                try:
                    await asyncio.sleep(interval_seconds)
                    await self.health_check_all()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "MCP health-loop iteration raised: %s — continuing", exc
                    )

        self._health_loop_task = asyncio.create_task(_loop())
        return self._health_loop_task

    def stop_health_loop(self) -> None:
        """Cancel the periodic health probe started by :meth:`start_health_loop`.

        Idempotent — no-op if the loop was never started or already stopped.
        """
        task = self._health_loop_task
        if task is None or task.done():
            self._health_loop_task = None
            return
        task.cancel()
        self._health_loop_task = None

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
