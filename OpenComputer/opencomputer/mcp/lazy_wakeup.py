"""First-tool-call lazy wakeup for bundle MCPs (Gap G).

mcp-openclaw-port follow-up. Plugin manifests declare a tool catalog
via :class:`BundleMcpServer.tools`. At plugin activation the loader
registers :class:`LazyBundleStubTool` instances by their composed
names; the LLM sees the tools as available immediately.

First dispatch through a stub:

1. Acquire a wakeup lock (per stub instance).
2. Call ``wakeup_fn()`` — typically routes through the MCPManager to
   connect the bundle's MCPServerConfig (which the registry currently
   has ``enabled=False`` because lazy=True maps that way per
   :mod:`opencomputer.mcp.bundle`).
3. After connect, the real :class:`MCPTool` registered itself in the
   tool registry. Look it up by name via ``registry_lookup``.
4. Cache the real tool reference on the stub. Subsequent dispatches
   bypass step 2 entirely.
5. Dispatch the call to the real tool. Return its result.

Failure paths surface as ``ToolResult(is_error=True)`` — wakeup_fn
raising :class:`BundleWakeupError` becomes a clear LLM-visible error.

This module is deliberately thin — the heavy lifting (subprocess
spawn, ClientSession init) is owned by ``MCPManager.connect_one``;
this module only owns the agent-side stub + wakeup-route bookkeeping.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

from plugin_sdk.core import BundleMcpToolDecl, ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

logger = logging.getLogger("opencomputer.mcp.lazy_wakeup")


class BundleWakeupError(RuntimeError):
    """Raised by ``wakeup_fn`` when the bundle MCP fails to wake.

    Reasons: subprocess spawn failure (ENOENT), MCP handshake timeout,
    OSV pre-flight block, transport error. The stub catches this and
    returns a structured ToolResult so the LLM gets a clear error
    message instead of a Python traceback.
    """


class LazyBundleStubTool(BaseTool):
    """Placeholder tool registered when a bundle MCP is in lazy state.

    Schema exposes the declared name + description + input_schema so
    the LLM treats it as a normal tool. On first execute, triggers
    wakeup + routes through to the real :class:`MCPTool` once it
    surfaces in the registry.

    Thread-safe: a per-instance lock serialises concurrent first calls
    so wakeup only fires once even under parallel dispatch.

    ``parallel_safe = False`` matches MCPTool — each bundle server has
    its own state, so we don't want parallel tool calls in flight on
    the same connection.
    """

    parallel_safe = False

    def __init__(
        self,
        plugin_id: str,
        server_name: str,
        decl: BundleMcpToolDecl,
        wakeup_fn: Callable[[], None],
        registry_lookup: Callable[[str], BaseTool | None],
    ) -> None:
        self.plugin_id = plugin_id
        self.server_name = server_name
        self.decl = decl
        self.wakeup_fn = wakeup_fn
        self.registry_lookup = registry_lookup
        self._wakeup_lock = threading.Lock()
        self._wakeup_done = False
        self._cached_real_tool: BaseTool | None = None

    @property
    def schema(self) -> ToolSchema:
        # Compose name matches what the real tool will register under
        # after wakeup; the registry lookup uses this name to find the
        # real tool post-wakeup.
        from opencomputer.mcp.naming import compose_mcp_tool_name

        display = compose_mcp_tool_name(
            self.plugin_id,
            self.server_name,
            self.decl.name,
            existing=set(),  # stub-side composition; registry-level
                              # collision handling happens at register
                              # time in MCPManager._connect_one.
        )
        return ToolSchema(
            name=display,
            description=self.decl.description,
            parameters=self.decl.input_schema,
        )

    def _ensure_woken(self) -> None:
        """Idempotent wakeup. Caller holds the asyncio loop; we hop
        through ``threading.Lock`` to serialise concurrent dispatches.
        """
        with self._wakeup_lock:
            if self._wakeup_done:
                return
            try:
                self.wakeup_fn()
            except BundleWakeupError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise BundleWakeupError(
                    f"unexpected wakeup error: {type(exc).__name__}: {exc}"
                ) from exc
            self._wakeup_done = True

    async def execute(self, call: ToolCall) -> ToolResult:
        # Lookup cached real tool first — hot path after first call.
        real = self._cached_real_tool
        if real is None:
            try:
                # Run wakeup in a thread so a sync wakeup_fn that does
                # real work (subprocess spawn, MCP handshake) doesn't
                # block the asyncio loop.
                await asyncio.get_event_loop().run_in_executor(
                    None, self._ensure_woken,
                )
            except BundleWakeupError as exc:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"bundle MCP wakeup failed: {exc}",
                    is_error=True,
                )
            display_name = self.schema.name
            real = self.registry_lookup(display_name)
            if real is None:
                return ToolResult(
                    tool_call_id=call.id,
                    content=(
                        f"bundle MCP wakeup ran but no live tool with name "
                        f"{display_name!r} is registered — the MCP server "
                        "may not have published this tool."
                    ),
                    is_error=True,
                )
            # Don't cache a reference to ourself (would infinite-loop).
            if real is self:
                return ToolResult(
                    tool_call_id=call.id,
                    content=(
                        f"bundle MCP wakeup self-loop detected for "
                        f"{display_name!r}; MCPManager may not have replaced "
                        "the stub in the registry."
                    ),
                    is_error=True,
                )
            self._cached_real_tool = real
        return await real.execute(call)


__all__ = [
    "BundleWakeupError",
    "LazyBundleStubTool",
]
