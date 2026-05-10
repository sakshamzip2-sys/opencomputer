"""Mem0Provider — alternative memory backend (Hermes deep-comparison A3).

Mirrors the structural shape of ``HonchoSelfHostedProvider`` but talks to
the Mem0 SDK (cloud or self-hosted) instead of Honcho's HTTP API. The
overall failure semantics are identical: prefetch/health failures
disable the provider for the session, sync_turn failures are silently
swallowed, and ``handle_tool_call`` never raises.

This file is intentionally light on Mem0-specific intelligence — the
goal is "feature parity with the MemoryProvider contract using Mem0 as
the backend," not "expose every Mem0 feature." A user who needs deep
Mem0 features can extend this provider in their own plugin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.memory import MemoryProvider
from plugin_sdk.tool_contract import ToolSchema

logger = logging.getLogger("memory-mem0")


@dataclass(frozen=True, slots=True)
class Mem0Config:
    """Resolved configuration for the Mem0 provider."""

    api_key: str = ""
    base_url: str = ""  # empty = use SDK's default (Mem0 Cloud)
    user_id: str = "opencomputer"
    enabled: bool = True


class Mem0Provider(MemoryProvider):
    """Vector-ranked memory provider backed by the ``mem0ai`` SDK.

    The SDK is imported lazily so the rest of OpenComputer doesn't pay
    the import cost (and the test suite doesn't have to install mem0ai).
    If the SDK is not installed, the provider falls back to a no-op
    implementation that returns empty results — the agent stays
    functional, just without Mem0 recall.
    """

    provider_priority: int = 110  # runs after Honcho's default 100

    def __init__(self, config: Mem0Config) -> None:
        self._config = config
        self._client: Any | None = None
        self._client_ready: bool = False
        self._sdk_missing: bool = False
        self._init_attempted: bool = False

    @property
    def provider_id(self) -> str:
        return "memory-mem0:client"

    # ─── lazy client setup ─────────────────────────────────────────

    def _ensure_client(self) -> Any | None:
        """Lazy SDK import + client construction.

        Returns the client object, or ``None`` if the SDK isn't
        installed or initialisation failed. Idempotent — only
        attempts initialisation once per process.
        """
        if self._client_ready:
            return self._client
        if self._init_attempted:
            return None
        self._init_attempted = True

        try:
            from mem0 import Memory, MemoryClient  # type: ignore[import-not-found]
        except ImportError:
            self._sdk_missing = True
            logger.info(
                "mem0ai SDK not installed; provider degraded to no-op. "
                "Install with `pip install mem0ai` to activate."
            )
            return None

        try:
            if self._config.api_key:
                # Cloud client
                self._client = MemoryClient(api_key=self._config.api_key)
            else:
                # Self-hosted via Memory.from_config (default OSS path).
                # Caller can wire base_url through environment via mem0's
                # own config story — we just feed an empty config and let
                # Mem0 pick up MEM0_BASE_URL from env.
                self._client = Memory()
            self._client_ready = True
            return self._client
        except Exception:  # noqa: BLE001
            logger.exception("Mem0 client initialisation failed")
            return None

    # ─── tool surface (3 namespaced tools) ─────────────────────────

    def tool_schemas(self) -> list[ToolSchema]:
        if not self._config.enabled:
            return []
        return [
            ToolSchema(
                name="mem0_search",
                description=(
                    "Search Mem0 memory for facts relevant to a query. "
                    "Returns up to ``limit`` matches ranked by semantic "
                    "similarity. Use to recall persistent user facts "
                    "across sessions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural-language query.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (1-20).",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolSchema(
                name="mem0_remember",
                description=(
                    "Persist a fact to Mem0 memory. Idempotent — Mem0 "
                    "deduplicates against existing memories."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The fact to remember.",
                        },
                    },
                    "required": ["content"],
                },
            ),
            ToolSchema(
                name="mem0_forget",
                description=(
                    "Delete a memory by id. The id is returned by "
                    "``mem0_search`` results."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "memory_id": {
                            "type": "string",
                            "description": "Mem0 memory id from a search result.",
                        },
                    },
                    "required": ["memory_id"],
                },
            ),
        ]

    async def handle_tool_call(self, call: ToolCall) -> ToolResult:
        client = self._ensure_client()
        if client is None:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "mem0 SDK is not installed. Run "
                    "`pip install mem0ai` (or `pip install opencomputer[mem0]`) "
                    "to enable memory recall. Rest of the system is unaffected."
                    if self._sdk_missing
                    else "mem0 client failed to initialise; check logs."
                ),
                is_error=True,
            )
        try:
            if call.name == "mem0_search":
                query = str(call.arguments.get("query", ""))
                limit = int(call.arguments.get("limit", 5))
                results = client.search(
                    query=query,
                    user_id=self._config.user_id,
                    limit=max(1, min(limit, 20)),
                )
                if isinstance(results, dict):
                    results = results.get("results", [])
                return ToolResult(
                    tool_call_id=call.id,
                    content=str(results),
                )
            if call.name == "mem0_remember":
                content = str(call.arguments.get("content", "")).strip()
                if not content:
                    return ToolResult(
                        tool_call_id=call.id,
                        content="content cannot be empty",
                        is_error=True,
                    )
                client.add(
                    messages=[{"role": "user", "content": content}],
                    user_id=self._config.user_id,
                )
                return ToolResult(
                    tool_call_id=call.id,
                    content="ok",
                )
            if call.name == "mem0_forget":
                memory_id = str(call.arguments.get("memory_id", "")).strip()
                if not memory_id:
                    return ToolResult(
                        tool_call_id=call.id,
                        content="memory_id is required",
                        is_error=True,
                    )
                client.delete(memory_id=memory_id)
                return ToolResult(
                    tool_call_id=call.id,
                    content="deleted",
                )
            return ToolResult(
                tool_call_id=call.id,
                content=f"unknown tool: {call.name}",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("mem0 tool call %s failed", call.name)
            return ToolResult(
                tool_call_id=call.id,
                content=f"mem0 error: {type(exc).__name__}: {exc}",
                is_error=True,
            )

    # ─── per-turn lifecycle (Memory contract) ──────────────────────

    async def prefetch(self, query: str, turn_index: int) -> str | None:
        """Inject a short ``## Memory context`` block each turn.

        Pulls top-3 semantically-matching memories for the user's most
        recent message. Returns ``None`` when:
        - the SDK isn't installed (graceful degrade);
        - the search returns zero results;
        - we hit a client-side error (logged at INFO).
        """
        client = self._ensure_client()
        if client is None:
            return None
        try:
            results = client.search(
                query=query,
                user_id=self._config.user_id,
                limit=3,
            )
            if isinstance(results, dict):
                results = results.get("results", [])
            if not results:
                return None
            lines: list[str] = []
            for r in results:
                if isinstance(r, dict):  # noqa: SIM108 — readability
                    txt = r.get("memory") or r.get("text") or str(r)
                else:
                    txt = str(r)
                lines.append(f"- {txt}")
            return "\n".join(lines[:3])
        except Exception:  # noqa: BLE001
            logger.exception("mem0 prefetch failed")
            return None

    async def sync_turn(
        self, user: str, assistant: str, turn_index: int
    ) -> None:
        """Best-effort: tell Mem0 to extract facts from a turn.

        Fire-and-forget per Memory contract. Mem0's ``add`` call does
        its own async fact extraction; we don't await any further
        bookkeeping here. Errors are swallowed so a flaky network
        doesn't wedge the loop.
        """
        client = self._ensure_client()
        if client is None:
            return
        try:
            client.add(
                messages=[
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": assistant},
                ],
                user_id=self._config.user_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("mem0 sync_turn failed")

    async def health_check(self) -> bool:
        return self._ensure_client() is not None

    async def system_prompt_block(
        self, *, session_id: str | None = None
    ) -> str | None:
        """Return a short prompt-side memory snippet, or ``None``.

        Mem0 doesn't have a dedicated "summarise everything you know"
        endpoint at the SDK layer; we approximate by searching for
        general user-profile facts. Returns ``None`` if no results or
        SDK absent.
        """
        client = self._ensure_client()
        if client is None:
            return None
        try:
            results = client.search(
                query="user profile preferences identity",
                user_id=self._config.user_id,
                limit=5,
            )
            if isinstance(results, dict):
                results = results.get("results", [])
            if not results:
                return None
            lines = []
            for r in results:
                if isinstance(r, dict):  # noqa: SIM108 — readability
                    txt = r.get("memory") or r.get("text") or str(r)
                else:
                    txt = str(r)
                lines.append(f"- {txt}")
            return "## Mem0 user profile (semantic recall)\n" + "\n".join(
                lines[:5]
            )
        except Exception:  # noqa: BLE001
            logger.exception("mem0 system_prompt_block failed")
            return None


__all__ = ["Mem0Provider", "Mem0Config"]
