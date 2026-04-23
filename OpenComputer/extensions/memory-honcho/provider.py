"""HonchoSelfHostedProvider — the memory plugin's actual MemoryProvider impl.

Wraps a running self-hosted Honcho instance via httpx. The five agent-facing
tools mirror Hermes's Honcho integration (profile / search / context /
reasoning / conclude).

Failure semantics (per plugin_sdk/memory.py contract):
  - health_check + prefetch failures → None + let the bridge disable us.
  - sync_turn failures → fire-and-forget, swallowed.
  - handle_tool_call failures → ToolResult(is_error=True), never raise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.memory import MemoryProvider
from plugin_sdk.tool_contract import ToolSchema

logger = logging.getLogger("memory-honcho")

_DEFAULT_BASE_URL = "http://localhost:8000"
_DEFAULT_HEALTH_TIMEOUT_S = 2.0
_DEFAULT_REQUEST_TIMEOUT_S = 10.0

#: Valid values for ``HonchoSelfHostedProvider(mode=...)``. The Literal on
#: the kwarg catches typos at type-check time; this frozenset is the
#: runtime safety net for dynamic instantiation from config files / env.
_VALID_MODES: frozenset[str] = frozenset({"context", "tools", "hybrid"})


@dataclass(frozen=True, slots=True)
class HonchoConfig:
    """Provider-side config loaded from ~/.opencomputer/honcho/.env or env vars."""

    base_url: str = _DEFAULT_BASE_URL
    api_key: str = ""
    workspace: str = "opencomputer"
    host_key: str = "opencomputer"  # Phase 14.J override target
    context_cadence: int = 1
    dialectic_cadence: int = 3


@dataclass(slots=True)
class _HonchoState:
    """Mutable per-session state (cadence counters, health flag)."""

    last_prefetch_turn: int = -1
    last_sync_turn: int = -1
    headers: dict[str, str] = field(default_factory=dict)


class HonchoSelfHostedProvider(MemoryProvider):
    """Deep user-understanding overlay backed by a local Honcho instance.

    The ``mode`` kwarg selects how Honcho is surfaced to the agent loop:

    * ``"context"`` — inject Honcho's context-cache text into the system
      prompt each turn (cheaper per-turn; default).
    * ``"tools"`` — expose Honcho as agent-facing tools (profile / search /
      context / reasoning / conclude) and let the model decide when to query.
    * ``"hybrid"`` — both: inject context AND expose tools.

    Mirrors Hermes' ``recall_mode`` at
    ``sources/hermes-agent/plugins/memory/honcho/__init__.py:155-200``.

    A2 stores the field only — ``prefetch`` / ``sync_turn`` / ``tool_schemas``
    behavior is unchanged. A5 (wizard) and A7 (AgentLoop wiring) will
    consume ``self.mode`` in follow-up tasks.
    """

    def __init__(
        self,
        config: HonchoConfig | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        mode: Literal["context", "tools", "hybrid"] = "context",
    ) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(
                f"mode must be one of {sorted(_VALID_MODES)}, got {mode!r}"
            )
        self.mode: str = mode
        self._config = config or HonchoConfig()
        self._state = _HonchoState()
        if self._config.api_key:
            self._state.headers["Authorization"] = f"Bearer {self._config.api_key}"
        # Tests inject a mock client via http_client=httpx.AsyncClient(transport=...)
        self._client = http_client or httpx.AsyncClient(
            base_url=self._config.base_url,
            headers=self._state.headers,
            timeout=_DEFAULT_REQUEST_TIMEOUT_S,
        )

    # ─── MemoryProvider protocol ───────────────────────────────────

    @property
    def provider_id(self) -> str:
        return "memory-honcho:self-hosted"

    def tool_schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(
                name="honcho_profile",
                description=(
                    "Get the structured peer-card summary of what Honcho "
                    "has learned about a user. Non-LLM, fast."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "peer": {
                            "type": "string",
                            "description": "Peer id (default 'user').",
                            "default": "user",
                        }
                    },
                },
            ),
            ToolSchema(
                name="honcho_search",
                description=(
                    "Semantic search over Honcho's stored context for this "
                    "user. Returns ranked excerpts."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "peer": {"type": "string", "default": "user"},
                        "max_tokens": {
                            "type": "integer",
                            "default": 800,
                            "maximum": 2000,
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolSchema(
                name="honcho_context",
                description=(
                    "Full session context: summary + user representation + "
                    "peer card + recent messages. Non-LLM."
                ),
                parameters={
                    "type": "object",
                    "properties": {"peer": {"type": "string", "default": "user"}},
                },
            ),
            ToolSchema(
                name="honcho_reasoning",
                description=(
                    "LLM-synthesised dialectic answer about a user. Use for "
                    "'what would this user prefer' questions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "peer": {"type": "string", "default": "user"},
                    },
                    "required": ["query"],
                },
            ),
            ToolSchema(
                name="honcho_conclude",
                description=("Persist a fact or conclusion about the user. Non-LLM."),
                parameters={
                    "type": "object",
                    "properties": {
                        "fact": {"type": "string"},
                        "peer": {"type": "string", "default": "user"},
                    },
                    "required": ["fact"],
                },
            ),
        ]

    async def handle_tool_call(self, call: ToolCall) -> ToolResult:
        handler = {
            "honcho_profile": self._profile,
            "honcho_search": self._search,
            "honcho_context": self._context,
            "honcho_reasoning": self._reasoning,
            "honcho_conclude": self._conclude,
        }.get(call.name)
        if handler is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: unknown tool '{call.name}'",
                is_error=True,
            )
        try:
            content = await handler(call.arguments or {})
            return ToolResult(tool_call_id=call.id, content=content, is_error=False)
        except Exception as e:  # noqa: BLE001 — must not raise out
            logger.warning("honcho tool %s failed: %s", call.name, e)
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: Honcho request failed: {e}",
                is_error=True,
            )

    async def prefetch(self, query: str, turn_index: int) -> str | None:
        # Cadence gate: run only every N turns.
        cadence = max(1, self._config.context_cadence)
        if turn_index % cadence != 0:
            return None
        try:
            resp = await self._client.post(
                "/v1/context",
                json={
                    "workspace": self._config.workspace,
                    "host_key": self._config.host_key,
                    "query": query,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("context") if isinstance(data, dict) else None
            return text if isinstance(text, str) and text else None
        except Exception as e:  # noqa: BLE001
            logger.debug("honcho prefetch failed: %s", e)
            return None

    async def sync_turn(self, user: str, assistant: str, turn_index: int) -> None:
        # Sync is fire-and-forget; cadence gates how often we POST.
        cadence = max(1, self._config.dialectic_cadence)
        if turn_index % cadence != 0:
            return
        try:
            await self._client.post(
                "/v1/messages",
                json={
                    "workspace": self._config.workspace,
                    "host_key": self._config.host_key,
                    "user": user,
                    "assistant": assistant,
                    "turn_index": turn_index,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("honcho sync_turn failed (ignored): %s", e)

    async def health_check(self) -> bool:
        try:
            resp = await self._client.get("/health", timeout=_DEFAULT_HEALTH_TIMEOUT_S)
            return resp.status_code == 200
        except Exception as e:  # noqa: BLE001
            logger.debug("honcho health_check failed: %s", e)
            return False

    async def aclose(self) -> None:
        await self._client.aclose()

    # ─── internal HTTP helpers (one per tool) ──────────────────────

    async def _profile(self, args: dict[str, Any]) -> str:
        peer = str(args.get("peer", "user"))
        resp = await self._client.get(
            "/v1/profile",
            params={
                "workspace": self._config.workspace,
                "host_key": self._config.host_key,
                "peer": peer,
            },
        )
        resp.raise_for_status()
        return _as_text(resp.json())

    async def _search(self, args: dict[str, Any]) -> str:
        resp = await self._client.post(
            "/v1/search",
            json={
                "workspace": self._config.workspace,
                "host_key": self._config.host_key,
                "peer": str(args.get("peer", "user")),
                "query": str(args["query"]),
                "max_tokens": int(args.get("max_tokens", 800)),
            },
        )
        resp.raise_for_status()
        return _as_text(resp.json())

    async def _context(self, args: dict[str, Any]) -> str:
        resp = await self._client.get(
            "/v1/context-full",
            params={
                "workspace": self._config.workspace,
                "host_key": self._config.host_key,
                "peer": str(args.get("peer", "user")),
            },
        )
        resp.raise_for_status()
        return _as_text(resp.json())

    async def _reasoning(self, args: dict[str, Any]) -> str:
        resp = await self._client.post(
            "/v1/chat",
            json={
                "workspace": self._config.workspace,
                "host_key": self._config.host_key,
                "peer": str(args.get("peer", "user")),
                "query": str(args["query"]),
            },
        )
        resp.raise_for_status()
        return _as_text(resp.json())

    async def _conclude(self, args: dict[str, Any]) -> str:
        resp = await self._client.post(
            "/v1/conclude",
            json={
                "workspace": self._config.workspace,
                "host_key": self._config.host_key,
                "peer": str(args.get("peer", "user")),
                "fact": str(args["fact"]),
            },
        )
        resp.raise_for_status()
        return _as_text(resp.json())


def _as_text(payload: Any) -> str:
    """Best-effort flatten of a Honcho JSON response into a text string."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        # Prefer obvious string-carrying fields.
        for key in ("context", "text", "answer", "summary", "message", "result"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                return v
    # Fallback: JSON-stringify so the caller at least sees the shape.
    import json

    return json.dumps(payload, ensure_ascii=False)


__all__ = ["HonchoSelfHostedProvider", "HonchoConfig"]
