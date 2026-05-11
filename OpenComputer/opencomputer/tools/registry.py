"""
Tool registry — a dict + dispatch.

Inspired by hermes's ToolEntry pattern. A singleton registry holds
ToolEntries; tools register themselves via `@register_tool`. The
agent loop asks the registry for all schemas and dispatches calls.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

logger = logging.getLogger("opencomputer.tools.registry")


class ToolRegistry:
    """Singleton registry. Import from elsewhere as `from opencomputer.tools.registry import registry`."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._denylist: set[str] = set()
        #: Hermes-v2 ``agent.disabled_toolsets``: prefix-match denylist.
        #: Matches when tool name == prefix OR starts with prefix + ``_``.
        self._deny_prefixes: tuple[str, ...] = ()

    def set_denylist(self, names: list[str] | tuple[str, ...]) -> None:
        """Replace the current denylist. Pass an empty iterable to clear.

        Mirrors openclaw's ``tools.deny`` config. Tools whose
        ``schema.name`` is in the denylist are silently skipped at
        :meth:`register` time. Callers with expensive optional-tool
        factories should call :meth:`is_denied` BEFORE constructing
        the tool to short-circuit factory work.
        """
        self._denylist = set(names)

    def set_deny_prefixes(self, prefixes: list[str] | tuple[str, ...]) -> None:
        """Hermes-v2 ``agent.disabled_toolsets`` — replace the prefix denylist.

        A tool's ``schema.name`` matches a prefix when:

        - the name equals the prefix exactly, OR
        - the name starts with ``prefix + "_"`` (word-boundary form).

        ``memorial_helper`` is NOT matched by ``memory`` — only ``memory``
        and ``memory_*`` are. This is conservative on purpose; users can
        add an extra prefix if they need broader matches.
        """
        self._deny_prefixes = tuple(prefixes)

    def is_denied(self, name: str) -> bool:
        """True if the named tool would be skipped at :meth:`register` time."""
        return name in self._denylist or self.is_denied_prefix(name)

    def is_denied_prefix(self, name: str) -> bool:
        """True if ``name`` matches any prefix from :meth:`set_deny_prefixes`."""
        for prefix in self._deny_prefixes:
            if name == prefix or name.startswith(prefix + "_"):
                return True
        return False

    def register(self, tool: BaseTool) -> None:
        name = tool.schema.name
        if name in self._denylist:
            logger.debug("Tool %r skipped: in denylist", name)
            return  # silent skip — caller can check is_denied() first
        if self.is_denied_prefix(name):
            logger.debug("Tool %r skipped: matches deny prefix", name)
            return
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered")
        self._tools[name] = tool

    def all_tools(self) -> list[BaseTool]:
        """Return all registered tools (post-filter). Hermes-v2 helper."""
        return list(self._tools.values())

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def schemas(self) -> list[ToolSchema]:
        return [t.schema for t in self._tools.values()]

    def tool_summaries(
        self, max_description_len: int = 80
    ) -> list[dict[str, str]]:
        """Return minimal ``{"name", "description"}`` dicts per tool.

        Used by providers operating in CC §4 lazy-schema-loading mode:
        the system prompt advertises only the tool catalog (names +
        short descriptions) and the agent fetches full schemas on
        demand via the ``ToolSearch`` tool.

        Args:
            max_description_len: cap on each description string.
                Strings longer than this are truncated and suffixed
                with ``"…"`` (single character) so the rendered size
                stays predictable. Clamped to ``[0, 4096]``;
                ``0`` produces name-only entries (description empty
                string).

        Returns:
            A list (NOT generator) of plain dicts — JSON-serialisable
            and provider-agnostic. Empty list for an empty registry.

        Spec: docs/OC-FROM-CLAUDE-CODE.md §4.
        """
        cap = max(0, min(int(max_description_len), 4096))
        out: list[dict[str, str]] = []
        for tool in self._tools.values():
            schema = tool.schema
            desc = schema.description or ""
            if cap == 0:
                short = ""
            elif len(desc) > cap:
                short = desc[:cap] + "…"
            else:
                short = desc
            out.append({"name": schema.name, "description": short})
        return out

    def summary_schemas(
        self, max_description_len: int = 80
    ) -> list[ToolSchema]:
        """Return ``ToolSchema`` instances with empty parameters blocks.

        Same intent as :meth:`tool_summaries` but typed as the provider-
        contract ``ToolSchema`` so providers can pass them through
        their existing serialisation pipeline. The parameters block is
        the minimum-valid JSON Schema (``{"type": "object", "properties": {}}``)
        — agents that need the real arguments call ``ToolSearch``.

        Spec: docs/OC-FROM-CLAUDE-CODE.md §4.
        """
        cap = max(0, min(int(max_description_len), 4096))
        out: list[ToolSchema] = []
        for tool in self._tools.values():
            schema = tool.schema
            desc = schema.description or ""
            if cap == 0:
                short = ""
            elif len(desc) > cap:
                short = desc[:cap] + "…"
            else:
                short = desc
            out.append(
                ToolSchema(
                    name=schema.name,
                    description=short,
                    parameters={"type": "object", "properties": {}},
                )
            )
        return out

    def names(self) -> Iterable[str]:
        return self._tools.keys()

    async def dispatch(
        self,
        call: ToolCall,
        *,
        session_id: str | None = None,
        turn_index: int | None = None,
        demand_tracker: Any | None = None,
    ) -> ToolResult:
        """Dispatch a tool call to its handler. Never raises — always returns a ToolResult.

        Phase 12b.5 Task E3: on the tool-not-found path, if a demand
        tracker is provided (duck-typed — we use ``Any`` to avoid the
        ``opencomputer.plugins.demand_tracker`` import cycle), record the
        miss best-effort. Exceptions from the tracker are swallowed so
        dispatch never fails because of demand-tracking infrastructure.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            if demand_tracker is not None and session_id is not None:
                try:
                    demand_tracker.record_tool_not_found(
                        call.name,
                        session_id,
                        turn_index or 0,
                    )
                except Exception:  # noqa: BLE001
                    # Best-effort — never let the demand tracker break dispatch.
                    logger.debug(
                        "demand_tracker.record_tool_not_found raised; swallowing",
                        exc_info=True,
                    )
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: tool '{call.name}' not found",
                is_error=True,
            )
        try:
            return await tool.execute(call)
        except Exception as e:  # defensive — tool.execute should handle its own errors
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {type(e).__name__}: {e}",
                is_error=True,
            )


registry = ToolRegistry()


def register_tool(tool: BaseTool) -> BaseTool:
    """Convenience: register and return the tool (so it can be used as a module-level call)."""
    registry.register(tool)
    return tool


__all__ = ["ToolRegistry", "registry", "register_tool"]
