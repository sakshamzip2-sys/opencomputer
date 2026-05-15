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
import threading
import time
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

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

#: TypeVar used by :meth:`MCPManager.submit_sync` to preserve the
#: caller's coroutine return type. The :func:`_run_on_session_loop`
#: helper below uses PEP 695 ``[T]`` syntax for its single-call-site
#: parameter; this TypeVar form survives across submit_sync's
#: definition-vs-call separation.
_T = TypeVar("_T")


async def _run_on_session_loop[T](
    coro_factory: Callable[[], Coroutine[Any, Any, T]],
    session_loop: asyncio.AbstractEventLoop | None,
    *,
    timeout: float,
) -> T:
    """Run an MCP session coroutine, dispatching across event loops when needed.

    MCP sessions are owned by a dedicated background event loop
    (:class:`MCPManager`'s ``_bg_loop``). Tools created during
    :meth:`MCPConnection._owner_lifetime` capture that loop in their
    ``session_loop`` attribute. When :meth:`execute` runs on a *different*
    loop (per-turn ``asyncio.run`` in ``oc chat``), the underlying
    ``ClientSession.call_tool`` coroutine cannot simply be awaited —
    its anyio streams are bound to the session's owner loop.

    The dispatch path:

    * Same loop (or no session loop bound) → await directly.
    * Cross-loop → submit via :func:`asyncio.run_coroutine_threadsafe` and
      bridge back to the caller's loop via :func:`asyncio.wrap_future`.

    We deliberately take a *factory* (not a pre-built coroutine) because
    coroutines pick up their owning loop the first time they're awaited;
    constructing inside this helper keeps the cross-loop semantics
    correct regardless of where the caller built the call.
    """
    coro = coro_factory()
    current = asyncio.get_running_loop()
    if session_loop is None or session_loop is current:
        return await asyncio.wait_for(coro, timeout=timeout)
    fut = asyncio.run_coroutine_threadsafe(coro, session_loop)
    return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=timeout)


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
    #: G10 (Hermes parity, 2026-05-09) class-level default. Survives
    #: ``MCPTool.__new__(MCPTool)`` constructions used by some test
    #: doubles, so ``execute`` always finds a usable timeout value.
    timeout: float = 30.0
    #: Class-level default for the same reason as ``timeout`` — test
    #: doubles that bypass ``__init__`` still get a sane "same loop"
    #: dispatch path. ``None`` means :func:`_run_on_session_loop` skips
    #: cross-loop trampolining and awaits the session call directly.
    session_loop: asyncio.AbstractEventLoop | None = None

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        parameters: dict[str, Any],
        session: ClientSession,
        timeout: float = 30.0,
        session_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.server_name = server_name
        self.tool_name = tool_name
        self.description = description
        self.parameters = parameters
        self.session = session
        # G10 (Hermes parity, 2026-05-09): per-tool-call timeout cap.
        self.timeout = timeout
        #: Event loop that owns ``session``. ``None`` means "assume same
        #: loop as the caller" (test-double path). In production this is
        #: set to ``MCPManager._bg_loop`` so ``execute`` can dispatch
        #: across loops via :func:`_run_on_session_loop`.
        self.session_loop = session_loop

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

        # Gap C (mcp-openclaw-port follow-up) — validate args against
        # the tool's inputSchema BEFORE round-tripping to the server.
        # Surfaces field-path errors to the LLM instead of a generic
        # MCP -32602; saves an RTT when the LLM produces obviously bad
        # arguments. Permissive on missing/non-object schemas.
        try:
            from opencomputer.mcp.schema_validation import (
                SchemaValidationError,
                validate_tool_arguments,
            )

            validate_tool_arguments(call.arguments, self.parameters)
        except SchemaValidationError as e:
            return ToolResult(
                tool_call_id=call.id,
                content=redact_runtime_text(str(e)),
                is_error=True,
            )
        except Exception:  # noqa: BLE001 — schema-validation must never crash dispatch
            # Fall through to the MCP server; it'll surface any error.
            pass

        try:
            # G10 (Hermes parity, 2026-05-09): cap the per-tool-call wait
            # so a wedged MCP server can't block the agent loop forever.
            # 2026-05-14 — wrapped in :func:`_run_on_session_loop` to
            # bridge per-turn ``asyncio.run`` callers to the
            # ``MCPManager`` background loop that owns ``self.session``.
            result = await _run_on_session_loop(
                lambda: self.session.call_tool(
                    name=self.tool_name, arguments=call.arguments
                ),
                self.session_loop,
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

    def __init__(self, canonical: MCPTool) -> None:
        self._canonical = canonical

    @property
    def server_name(self) -> str:
        """Forward ``server_name`` so callers iterating mixed lists work."""
        return self._canonical.server_name

    @property
    def tool_name(self) -> str:
        """Forward ``tool_name`` so callers iterating mixed lists work."""
        return self._canonical.tool_name

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


#: Default timeout for MCP utility tools (list_resources / read_resource /
#: list_prompts / get_prompt). These are server-side reads that should
#: never hang the agent loop for more than a few seconds.
_MCP_UTILITY_TIMEOUT_S: float = 30.0


class _MCPListResourcesTool(BaseTool):
    """``<server>__list_resources`` — enumerate the server's resources."""

    parallel_safe = True

    def __init__(
        self,
        server_name: str,
        session: Any,
        session_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._server_name = server_name
        self._session = session
        self._session_loop = session_loop

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
            result = await _run_on_session_loop(
                lambda: self._session.list_resources(),
                self._session_loop,
                timeout=_MCP_UTILITY_TIMEOUT_S,
            )
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

    def __init__(
        self,
        server_name: str,
        session: Any,
        session_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._server_name = server_name
        self._session = session
        self._session_loop = session_loop

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
            result = await _run_on_session_loop(
                lambda: self._session.read_resource(uri),
                self._session_loop,
                timeout=_MCP_UTILITY_TIMEOUT_S,
            )
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

    def __init__(
        self,
        server_name: str,
        session: Any,
        session_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._server_name = server_name
        self._session = session
        self._session_loop = session_loop

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
            result = await _run_on_session_loop(
                lambda: self._session.list_prompts(),
                self._session_loop,
                timeout=_MCP_UTILITY_TIMEOUT_S,
            )
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

    def __init__(
        self,
        server_name: str,
        session: Any,
        session_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._server_name = server_name
        self._session = session
        self._session_loop = session_loop

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
            result = await _run_on_session_loop(
                lambda: self._session.get_prompt(name, arguments=arguments),
                self._session_loop,
                timeout=_MCP_UTILITY_TIMEOUT_S,
            )
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
    session_loop: asyncio.AbstractEventLoop | None = None,
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
        tools.append(_MCPListResourcesTool(
            server_name=server_name, session=session, session_loop=session_loop,
        ))
        tools.append(_MCPReadResourceTool(
            server_name=server_name, session=session, session_loop=session_loop,
        ))
    if prompts_enabled and capabilities.get("prompts") is not None:
        tools.append(_MCPListPromptsTool(
            server_name=server_name, session=session, session_loop=session_loop,
        ))
        tools.append(_MCPGetPromptTool(
            server_name=server_name, session=session, session_loop=session_loop,
        ))
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
    # ───── owner-task plumbing (2026-05-14 — anyio cross-task fix) ─────
    # The MCP SDK's stdio_client + ClientSession use anyio task groups
    # whose cancel scopes are pinned to the task that entered them.
    # ``connect()`` spawns a dedicated owner task that enters AND exits
    # those contexts in the same task. The connect() / disconnect()
    # public surface signals the owner via ``_owner_done`` so cleanup
    # always lands in the entering task — anyio's "same task" guard is
    # satisfied even when the caller lives on a different loop or task.
    #: Owner task that holds the MCP session's contexts open.
    _owner_task: asyncio.Task[None] | None = None
    #: Owner task signals ready (or error) by setting this; ``connect()``
    #: waits on it before returning. Re-created each ``connect()`` so
    #: stale events don't leak across reconnect cycles.
    _owner_ready: asyncio.Event | None = None
    #: ``disconnect()`` sets this; the owner task observes and unwinds.
    _owner_done: asyncio.Event | None = None
    #: Captures any exception raised inside the owner task before it
    #: signaled ready — so ``connect()`` can surface a useful error
    #: instead of returning a vague ``False``.
    _owner_error: BaseException | None = None
    #: Event loop that owns the connection — also tagged onto every
    #: tool's ``session_loop`` so cross-loop calls dispatch correctly.
    _owner_loop: asyncio.AbstractEventLoop | None = None
    #: Gap B (mcp-openclaw-port follow-up) — per-server stderr log
    #: handle. ``None`` for non-stdio transports + the fallback path
    #: where opening the log failed. Closed on disconnect to release
    #: the file descriptor.
    _stderr_log_handle: Any = None

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
        """Spawn the owner task and wait for it to signal ready.

        See :meth:`_owner_lifetime` for the actual transport / session
        plumbing. This method only owns the spawn → ready handshake.
        Returns ``True`` when the owner is ``connected``; ``False`` on
        any failure (state + ``last_error`` already set).

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
        if self._owner_task is not None and not self._owner_task.done():
            # Already connected (or connecting) — idempotent return.
            return self.state == "connected"

        self._owner_ready = asyncio.Event()
        self._owner_done = asyncio.Event()
        self._owner_error = None
        self._owner_loop = asyncio.get_running_loop()
        # Legacy ``exit_stack`` field — preserved as ``None`` so existing
        # introspection (tests, status snapshots) keeps a stable shape.
        # The owner task uses nested ``async with`` instead.
        self.exit_stack = None

        self._owner_task = asyncio.create_task(
            self._owner_lifetime(
                osv_check_enabled=osv_check_enabled,
                osv_check_fail_closed=osv_check_fail_closed,
            ),
            name=f"mcp-owner[{self.config.name}]",
        )

        await self._owner_ready.wait()

        if self._owner_error is not None:
            # Owner crashed before becoming usable. Drain the task so
            # we don't leak a still-pending Task object.
            try:
                await asyncio.wait_for(self._owner_task, timeout=5.0)
            except TimeoutError:
                self._owner_task.cancel()
                try:
                    await self._owner_task
                except BaseException:  # noqa: BLE001
                    pass
            except BaseException:  # noqa: BLE001
                pass
            self._owner_task = None
            return False

        return self.state == "connected"

    async def _owner_lifetime(
        self,
        *,
        osv_check_enabled: bool,
        osv_check_fail_closed: bool,
    ) -> None:
        """Body of the per-connection owner task.

        Enters the transport-specific context manager + ClientSession,
        initializes the session, captures tools / capabilities, then
        parks on ``_owner_done`` until :meth:`disconnect` releases it.
        Every ``__aenter__`` and matching ``__aexit__`` runs in **this
        single task** so anyio's cross-task cancel-scope guard never
        trips, even when the caller lives on a different event loop
        (the ``MCPManager`` background-loop pattern).
        """
        assert self._owner_ready is not None
        assert self._owner_done is not None
        try:
            # ── transport-specific entry ───────────────────────
            if self.config.transport == "stdio":
                if osv_check_enabled:
                    blocked = self._osv_pre_flight(fail_closed=osv_check_fail_closed)
                    if blocked is not None:
                        # OSV fail-closed refused the launch — surface
                        # via _owner_error so connect() returns False
                        # with the right last_error string set.
                        self.state = "error"
                        self.last_error = blocked
                        self._owner_error = RuntimeError(blocked)
                        self._owner_ready.set()
                        return
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

                    filtered_env = _build_mcp_subprocess_env(
                        dict(os.environ), self.config.env,
                    )
                    spawn_env = scope_subprocess_env(
                        filtered_env, profile=read_active_profile()
                    )
                except Exception:  # noqa: BLE001
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
                # Gap B (mcp-openclaw-port follow-up) — per-server stderr
                # capture to ``<profile>/logs/mcp/<server>.log``. Falls
                # back to inheriting parent's stderr on any error so a
                # broken log path never blocks MCP startup.
                try:
                    from opencomputer.mcp.stderr_capture import open_mcp_stderr_log

                    self._stderr_log_handle = open_mcp_stderr_log(self.config.name)
                    transport_ctx = stdio_client(
                        params, errlog=self._stderr_log_handle,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "MCP server '%s' stderr-capture failed (%s) — "
                        "falling back to parent stderr",
                        self.config.name, exc,
                    )
                    self._stderr_log_handle = None
                    transport_ctx = stdio_client(params)
            elif self.config.transport == "sse":
                if not self.config.url:
                    raise ValueError(
                        f"MCP server '{self.config.name}' transport=sse requires url"
                    )
                transport_ctx = sse_client(
                    self.config.url, headers=self.config.headers or None
                )
            elif self.config.transport == "http":
                if not self.config.url:
                    raise ValueError(
                        f"MCP server '{self.config.name}' transport=http requires url"
                    )
                transport_ctx = streamablehttp_client(
                    self.config.url, headers=self.config.headers or None
                )
            else:
                raise ValueError(
                    f"unknown MCP transport: {self.config.transport!r} "
                    f"(supported: stdio, sse, http)"
                )

            async with transport_ctx as streams_obj:
                # ``http`` transport yields a 3-tuple (read, write,
                # get_session_id); stdio + sse yield a 2-tuple. We index
                # rather than destructure so the static type checker
                # doesn't get tangled on the union.
                read_stream = streams_obj[0]
                write_stream = streams_obj[1]

                # T71 — sampling/createMessage host bridge. Lets MCP
                # servers ask US to run LLM completions; we route
                # through aux_llm so the configured provider (+
                # fallback chain) handles them.
                from opencomputer.mcp.sampling import make_sampling_callback

                async with ClientSession(
                    read_stream,
                    write_stream,
                    message_handler=self._handle_session_message,
                    sampling_callback=make_sampling_callback(),
                ) as session:
                    init_result = await session.initialize()
                    # Capture server version from InitializeResult.serverInfo
                    # when present. Defensive — custom servers / mocks may
                    # not expose the nested attribute.
                    try:
                        server_info = getattr(init_result, "serverInfo", None)
                        self.version = (
                            getattr(server_info, "version", None)
                            if server_info
                            else None
                        )
                    except Exception:  # noqa: BLE001
                        self.version = None
                    self.session = session

                    # List + cache tools. Internal tools (owner=system OR
                    # internal=true) are filtered here so the agent never
                    # sees them in its schema. P-16 sub-item (a).
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
                            parameters=t.inputSchema
                            or {"type": "object", "properties": {}},
                            session=session,
                            timeout=self.config.timeout,
                            session_loop=self._owner_loop,
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

                    # T1 — register Hermes-doc utility tools when the
                    # server advertises ``resources`` / ``prompts``.
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
                            session_loop=self._owner_loop,
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
                    self._owner_ready.set()
                    # Park here until disconnect() asks us to wind down.
                    # The two ``async with`` blocks above unwind IN THIS
                    # TASK when ``_owner_done`` is set or CancelledError
                    # is injected — satisfying anyio's same-task rule.
                    await self._owner_done.wait()
                    # Detach the session reference BEFORE the contexts
                    # unwind so concurrent observers don't see a stream
                    # in the middle of being torn down.
                    self.session = None
        except asyncio.CancelledError:
            # Cancellation is the standard signal during interpreter
            # shutdown / ``asyncio.run`` cleanup. The ``async with`` blocks
            # unwind here, in this task — no cross-task error possible.
            self.session = None
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "MCP server '%s' failed to connect: %s", self.config.name, e
            )
            self.state = "error"
            self.last_error = f"{type(e).__name__}: {e}"
            self._owner_error = e
            self.session = None
            # Unblock connect() if we crashed before signaling ready.
            if not self._owner_ready.is_set():
                self._owner_ready.set()

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
                session_loop=self._owner_loop,
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
        """Signal the owner task to wind down and await its exit.

        The owner task unwinds the ``stdio_client`` / ``ClientSession``
        contexts **in its own task** so anyio's same-task ``__aexit__``
        rule is honored — this is the production fix for the
        cross-task ``RuntimeError: Attempted to exit cancel scope in a
        different task than it was entered in`` wall that previously
        leaked from ``asyncio.run(connect_all(...))`` cleanup at the
        end of every ``oc chat`` start.

        Idempotent: safe to call when never connected or already
        disconnected.
        """
        task = self._owner_task
        done = self._owner_done
        if task is not None and done is not None and not task.done():
            done.set()
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except TimeoutError:
                logger.warning(
                    "MCP server '%s' owner task did not exit within 10s — cancelling",
                    self.config.name,
                )
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001
                    pass
            except BaseException as exc:  # noqa: BLE001
                logger.debug(
                    "MCP server '%s' owner task raised on disconnect: %s",
                    self.config.name,
                    exc,
                )
        # Reset owner-task state regardless of outcome so a subsequent
        # ``connect()`` starts cleanly.
        self._owner_task = None
        self._owner_ready = None
        self._owner_done = None
        self._owner_error = None
        self._owner_loop = None
        # Gap B — close per-server stderr log handle.
        if self._stderr_log_handle is not None:
            try:
                self._stderr_log_handle.close()
            except Exception:  # noqa: BLE001 — close on best-effort
                pass
            self._stderr_log_handle = None
        # Legacy ``exit_stack`` kept ``None`` for compat with status
        # introspection — owner-task pattern doesn't need it.
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
        # Servers currently being connected in the background. Tracked
        # separately from ``self.connections`` so ``/mcp`` can show
        # "connecting" status before the connection succeeds or errors.
        self._connecting: set[str] = set()
        # Future of the in-flight deferred-connect submission so callers
        # (tests, ``/mcp status --wait``) can await full completion.
        self._deferred_future: Any | None = None
        # ── Background-loop plumbing (2026-05-14) ─────────────────────
        # All MCPConnection async work — connect, disconnect, health
        # probes, tool calls — runs on a dedicated daemon-thread event
        # loop. This decouples MCP lifetimes from the caller's loop
        # (per-turn ``asyncio.run`` in ``oc chat``), letting MCP tools
        # actually survive across turns AND eliminating the cross-task
        # cancel-scope error wall that previously fired on every
        # ``asyncio.run(connect_all(...))`` cleanup.
        self._bg_loop: asyncio.AbstractEventLoop | None = None
        self._bg_thread: threading.Thread | None = None
        self._bg_ready: threading.Event = threading.Event()

    async def connect_all(
        self,
        servers: list[MCPServerConfig],
        *,
        osv_check_enabled: bool = True,
        osv_check_fail_closed: bool = False,
        include_bundle: bool = True,
    ) -> int:
        """Connect to every enabled server + register its tools. Returns tool count.

        Each server is connected in parallel via :func:`asyncio.gather`
        so a slow server doesn't gate the rest. Tools register into
        :attr:`tool_registry` per-server as each connection completes —
        callers using :meth:`start_in_background` see tools appear
        incrementally rather than in one batch at the end.

        ``osv_check_enabled`` and ``osv_check_fail_closed`` are passed
        straight through to each :meth:`MCPConnection.connect` call so
        callers can plumb :class:`MCPConfig` flags without per-server
        threading.

        ``include_bundle`` (default ``True``): also walk
        :data:`opencomputer.mcp.bundle.default_registry` to mount every
        plugin-shipped bundle MCP server. Set ``False`` in tests that
        want a clean process-global config baseline. mcp-openclaw-port M1.
        """
        merged: list[MCPServerConfig] = list(servers)
        if include_bundle:
            # Late import — keeps client.py's startup cheap when no
            # plugins have bundled MCPs.
            from opencomputer.mcp.bundle import default_registry

            bundle_configs = default_registry.all_server_configs()
            # Skip bundle entries whose ``name`` already appears in the
            # user-configured list. This is defensive: a user could (in
            # principle) hand-add a server named ``plug__mem`` that
            # collides with a bundle. User config wins — they explicitly
            # asked for it.
            existing_names = {cfg.name for cfg in merged}
            for cfg in bundle_configs:
                if cfg.name in existing_names:
                    logger.warning(
                        "bundle MCP %s shadowed by user-configured server "
                        "with same name; skipping bundle entry",
                        cfg.name,
                    )
                    continue
                merged.append(cfg)
        enabled = [cfg for cfg in merged if cfg.enabled]
        if not enabled:
            return 0

        async def _connect_one(cfg: MCPServerConfig) -> int:
            self._connecting.add(cfg.name)
            try:
                conn = MCPConnection(
                    config=cfg,
                    tools_changed_callback=self._on_connection_tools_changed,
                )
                try:
                    ok = await conn.connect(
                        osv_check_enabled=osv_check_enabled,
                        osv_check_fail_closed=osv_check_fail_closed,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "MCP server '%s' connect raised: %s", cfg.name, exc
                    )
                    return 0
                if not ok:
                    return 0
                self.connections.append(conn)
                added = 0
                for tool in conn.tools:
                    try:
                        self.tool_registry.register(tool)
                        added += 1
                    except ValueError:
                        logger.warning(
                            "MCP tool name collision (skipped): %s",
                            tool.schema.name,
                        )
                return added
            finally:
                self._connecting.discard(cfg.name)

        counts = await asyncio.gather(
            *(_connect_one(cfg) for cfg in enabled), return_exceptions=False
        )
        return sum(counts)

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
        # Gap A defence-in-depth — after the SDK's per-connection
        # kill-tree path runs, scan once for any straggler MCP
        # subprocess that survived (crash-path, owner-task force
        # cancellation, etc.) and terminate it. No-op when clean.
        try:
            from opencomputer.mcp.process_tree import kill_mcp_descendants

            n_term, n_kill = kill_mcp_descendants(os.getpid())
            if n_term or n_kill:
                logger.info(
                    "MCP shutdown orphan sweep: terminated=%d killed=%d",
                    n_term, n_kill,
                )
        except Exception as exc:  # noqa: BLE001 — sweep must never block shutdown
            logger.warning("MCP shutdown orphan sweep raised: %s", exc)

    # ── background-loop lifecycle (2026-05-14 anyio fix) ──────────────

    @property
    def background_loop(self) -> asyncio.AbstractEventLoop | None:
        """The dedicated MCP event loop, ``None`` if not started.

        Exposed for tools (:class:`MCPTool`, utility tools) that need
        to dispatch session calls back to the owner loop via
        :func:`asyncio.run_coroutine_threadsafe`. In practice callers
        read this off ``tool.session_loop`` instead, which is captured
        during :meth:`MCPConnection._owner_lifetime`.
        """
        return self._bg_loop

    def start_background_loop(self) -> None:
        """Start the dedicated daemon-thread event loop, if not already running.

        Idempotent. Blocks until the loop is ready (or 5s elapses, in
        which case a :class:`RuntimeError` is raised — the daemon
        thread is unrecoverable at that point).
        """
        if self._bg_thread is not None and self._bg_thread.is_alive():
            return
        self._bg_ready.clear()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            self._bg_loop = loop
            asyncio.set_event_loop(loop)
            self._bg_ready.set()
            try:
                loop.run_forever()
            finally:
                # On-thread cleanup. shutdown_asyncgens is best-effort;
                # connections should already have been disconnected via
                # :meth:`stop_background_loop` before we get here.
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:  # noqa: BLE001
                    pass
                loop.close()

        self._bg_thread = threading.Thread(
            target=_run,
            daemon=True,
            name="opencomputer-mcp-loop",
        )
        self._bg_thread.start()
        if not self._bg_ready.wait(timeout=5.0):
            raise RuntimeError(
                "MCP background loop failed to start within 5s — "
                "the daemon thread is unrecoverable."
            )

    def stop_background_loop(self, *, timeout: float = 10.0) -> None:
        """Disconnect all servers + stop the dedicated event loop.

        Best-effort. Callers (chat shutdown, oneshot end) should put
        this in a ``finally`` block so the daemon thread terminates
        cleanly even when the chat loop crashes.
        """
        if self._bg_loop is None or self._bg_thread is None:
            return
        bg_loop = self._bg_loop
        bg_thread = self._bg_thread
        try:
            # Run shutdown() on the bg loop so disconnect() lands in the
            # right task. ``submit_sync`` raises on bg-loop death; we
            # log + continue so loop-stop still fires.
            self.submit_sync(self.shutdown(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP background-loop shutdown raised: %s", exc)
        try:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
        except RuntimeError:
            # Loop already stopped — fine.
            pass
        bg_thread.join(timeout=5.0)
        self._bg_thread = None
        self._bg_loop = None
        self._bg_ready.clear()

    def submit_sync(
        self,
        coro: Coroutine[Any, Any, _T],
        *,
        timeout: float = 60.0,
    ) -> _T:
        """Run an MCPManager coroutine on the bg loop from sync code.

        Blocks the calling thread until the coroutine completes (or
        ``timeout`` elapses). Use this whenever you'd reach for
        ``asyncio.run(mgr.connect_all(...))`` in sync entry points
        — the bg-loop version preserves MCP session lifetimes across
        subsequent per-turn ``asyncio.run`` calls and never trips the
        cross-task cancel-scope error on cleanup.
        """
        if self._bg_loop is None:
            raise RuntimeError(
                "MCPManager background loop is not running — "
                "call ``start_background_loop()`` first."
            )
        fut = asyncio.run_coroutine_threadsafe(coro, self._bg_loop)
        return fut.result(timeout=timeout)

    def connect_all_sync(
        self,
        servers: list[MCPServerConfig],
        *,
        osv_check_enabled: bool = True,
        osv_check_fail_closed: bool = False,
        timeout: float = 120.0,
        include_bundle: bool = True,
    ) -> int:
        """Sync wrapper around :meth:`connect_all` for non-async callers.

        Auto-starts the background loop if not running. Use this in
        ``oc chat`` and other sync entry points instead of
        ``asyncio.run(mgr.connect_all(...))``.
        """
        self.start_background_loop()
        return self.submit_sync(
            self.connect_all(
                servers,
                osv_check_enabled=osv_check_enabled,
                osv_check_fail_closed=osv_check_fail_closed,
                include_bundle=include_bundle,
            ),
            timeout=timeout,
        )

    def start_in_background(
        self,
        servers: list[MCPServerConfig],
        *,
        osv_check_enabled: bool = True,
        osv_check_fail_closed: bool = False,
        include_bundle: bool = True,
    ) -> None:
        """Fire-and-forget connect_all on the bg loop. Non-blocking.

        Matches Claude Code's MCP UX: chat startup never waits on MCP;
        tools register into the registry as each server comes online.
        The user sees a "connecting" status via ``/mcp`` until each
        connection resolves (``connected`` or ``error``).

        Use ``connect_all_sync`` if you need to block until every
        server is up (one-shot scripts, tests).
        """
        self.start_background_loop()
        assert self._bg_loop is not None  # set by start_background_loop
        self._deferred_future = asyncio.run_coroutine_threadsafe(
            self.connect_all(
                servers,
                osv_check_enabled=osv_check_enabled,
                osv_check_fail_closed=osv_check_fail_closed,
                include_bundle=include_bundle,
            ),
            self._bg_loop,
        )

    def wait_for_deferred(self, *, timeout: float = 120.0) -> int:
        """Block until the most recent :meth:`start_in_background` call finishes.

        Returns the number of tools registered. Raises if the future
        raised or timed out. Idempotent / safe to call when no deferred
        future is in flight (returns 0).
        """
        fut = self._deferred_future
        if fut is None:
            return 0
        try:
            return int(fut.result(timeout=timeout) or 0)
        finally:
            # Only clear if we got the result we waited for — don't
            # clobber a newer in-flight submission from a /reload-mcp
            # racing against this call.
            if self._deferred_future is fut:
                self._deferred_future = None

    def connect_one_sync(
        self,
        cfg: MCPServerConfig,
        *,
        osv_check_enabled: bool = True,
        osv_check_fail_closed: bool = False,
        timeout: float = 60.0,
    ) -> bool:
        """Connect a single server on the bg loop. Used by ``/mcp connect``.

        If a connection for ``cfg.name`` already exists, it's
        disconnected first so the call is idempotent. Returns True on
        successful connect.
        """
        self.start_background_loop()

        async def _do() -> bool:
            # If a deferred connect for this name is already in flight,
            # wait for it to finish instead of spawning a duplicate.
            # The bg loop runs everything sequentially per task, so by
            # the time control returns here, the in-flight connect has
            # either landed in ``self.connections`` (we'll find it
            # below and disconnect/replace it) or it failed (no entry
            # exists, we proceed to spawn fresh). Polling is cheap
            # because this awaits ``asyncio.sleep(0)`` — no busy loop.
            poll_deadline = asyncio.get_running_loop().time() + 30.0
            while (
                cfg.name in self._connecting
                and asyncio.get_running_loop().time() < poll_deadline
            ):
                await asyncio.sleep(0.05)
            # Remove any stale connection with the same name first.
            existing = next(
                (c for c in self.connections if c.config.name == cfg.name), None
            )
            if existing is not None:
                for tool in existing.tools:
                    try:
                        self.tool_registry.unregister(tool.schema.name)
                    except KeyError:
                        pass
                await existing.disconnect()
                self.connections.remove(existing)
            self._connecting.add(cfg.name)
            try:
                conn = MCPConnection(
                    config=cfg,
                    tools_changed_callback=self._on_connection_tools_changed,
                )
                ok = await conn.connect(
                    osv_check_enabled=osv_check_enabled,
                    osv_check_fail_closed=osv_check_fail_closed,
                )
                if not ok:
                    return False
                self.connections.append(conn)
                for tool in conn.tools:
                    try:
                        self.tool_registry.register(tool)
                    except ValueError:
                        logger.warning(
                            "MCP tool name collision (skipped): %s",
                            tool.schema.name,
                        )
                return True
            finally:
                self._connecting.discard(cfg.name)

        return bool(self.submit_sync(_do(), timeout=timeout))

    def disconnect_one_sync(self, name: str, *, timeout: float = 30.0) -> bool:
        """Disconnect a single server on the bg loop. Used by ``/mcp disconnect``.

        Returns True if a connection was found and disconnected,
        False if no such server is currently connected.
        """
        if self._bg_loop is None:
            return False

        async def _do() -> bool:
            conn = next(
                (c for c in self.connections if c.config.name == name), None
            )
            if conn is None:
                return False
            for tool in conn.tools:
                try:
                    self.tool_registry.unregister(tool.schema.name)
                except KeyError:
                    pass
            await conn.disconnect()
            self.connections.remove(conn)
            return True

        return bool(self.submit_sync(_do(), timeout=timeout))

    def is_connecting(self, name: str) -> bool:
        """Return True if a background connect is in flight for ``name``."""
        return name in self._connecting

    def connecting_names(self) -> list[str]:
        """List of server names currently being connected (read-only snapshot)."""
        return sorted(self._connecting)

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
            # ``conn.tools`` is a mixed list: ``MCPTool`` canonicals,
            # ``MCPAliasTool`` Hermes-shape aliases, plus utility tools
            # (``_MCPListResourcesTool`` etc.) which expose their name
            # only via ``schema.name``, NOT a ``tool_name`` attribute.
            # Prefer the raw ``tool_name`` when present (preserves the
            # "alpha"/"beta" shape that test_mcp_status expects); fall
            # back to ``schema.name`` for utility tools, stripping the
            # ``<server>__`` prefix so the display stays consistent.
            tool_names: list[str] = []
            prefix = f"{cfg.name}__"
            for t in conn.tools:
                name = getattr(t, "tool_name", None)
                if name is None:
                    try:
                        schema_name = t.schema.name
                    except AttributeError:
                        # Misbuilt test double — skip it rather than
                        # poisoning the snapshot.
                        continue
                    name = (
                        schema_name[len(prefix):]
                        if schema_name.startswith(prefix)
                        else schema_name
                    )
                tool_names.append(name)
            snap.append(
                {
                    "name": cfg.name,
                    "url": target,
                    "version": conn.version,
                    "tool_count": len(conn.tools),
                    "tools": tool_names,
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
