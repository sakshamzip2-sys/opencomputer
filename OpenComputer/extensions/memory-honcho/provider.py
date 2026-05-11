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

import asyncio
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
    # T4 — Hermes-doc query-adaptive dialectic reasoning. Server-side
    # consumers may ignore the field on older versions — best-effort
    # forward.
    dialectic_reasoning_level: Literal["low", "medium", "high"] = "low"
    reasoning_level_cap: Literal["low", "medium", "high"] = "high"


# T4 — query-length-driven reasoning-level scaling.
_REASONING_LEVELS: tuple[str, ...] = ("low", "medium", "high")


def _adapt_reasoning_level(base: str, query: str, cap: str) -> str:
    """Bump the dialectic reasoning level by query length.

    Heuristic from the Hermes reference doc: ≥120 chars → +1 step,
    ≥400 chars → +2 steps. Clamped at ``cap``. If either ``base`` or
    ``cap`` is not a known level, returns ``base`` unchanged.
    """
    try:
        base_idx = _REASONING_LEVELS.index(base)
        cap_idx = _REASONING_LEVELS.index(cap)
    except ValueError:
        return base
    boost = 0
    if len(query) >= 120:
        boost += 1
    if len(query) >= 400:
        boost += 1
    return _REASONING_LEVELS[min(base_idx + boost, cap_idx)]


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

    # ─── Phase 0 outcome-aware learning subscription ────────────────

    def subscribe_to_outcome_events(self, bus):
        """Register a handler for ``TurnCompletedEvent`` on the typed
        event bus.

        Honcho is always-on per profile (Sub-project A), so we always
        want to observe outcome events the dispatch layer publishes
        after each turn. The handler converts each ``TurnCompletedEvent``
        into a Honcho ``conclude(observation_mode=inferred)`` call so
        the upstream user-model accumulates real signal from every
        turn, not just from explicit user statements.

        2026-05-11 — replaced the v0 log-only handler with the real
        ``/v1/conclude`` POST. Fire-and-forget via
        :func:`opencomputer.hooks.runner.fire_and_forget` so a slow or
        unreachable Honcho server cannot block bus fanout.

        Failure semantics:

        * HTTP failures, non-2xx responses, network errors, and JSON
          decode errors are logged at WARNING but never raised — the
          contract is that bus handlers never re-raise.
        * If ``signals`` is empty (e.g. a turn that recorded no
          outcome metrics), the handler skips the POST. There is
          nothing useful to observe.
        * If the HTTP client has been closed (shutdown race), the
          handler logs at DEBUG and returns.

        Returns the :class:`Subscription` handle. Caller (or tests)
        invokes ``.unsubscribe()`` to tear down.
        """
        # Inline fire-and-forget scheduling — schedule the conclude
        # coroutine onto the running event loop if one is active, else
        # close it cleanly (test env / sync caller). Equivalent to
        # ``opencomputer.hooks.runner.fire_and_forget`` but avoids
        # importing from ``opencomputer.*`` so this extension stays
        # inside the SDK boundary rule
        # (``tests/test_plugin_extension_boundary.py``).

        def _fire(coro) -> None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop in this caller's context — drop the
                # coroutine cleanly (close so the runtime doesn't
                # complain about a never-awaited coroutine).
                coro.close()
                logger.debug(
                    "honcho fire-and-forget dropped — no running loop"
                )
                return
            task = loop.create_task(coro)
            # Attach a no-op done-callback so the task isn't GC'd
            # before it runs (asyncio's strong-reference rule).
            task.add_done_callback(lambda _t: None)

        async def _async_conclude(fact: str) -> None:
            """POST /v1/conclude with 2-attempt retry on transient failures.

            Retry policy (mirrors skill-evolution's judge call):

            * Retryable: network errors (timeout, connect, read) and
              5xx responses — attempt twice, then give up.
            * Non-retryable: 4xx responses — log once and stop;
              retrying won't help.
            * Closed client: skip silently.

            2 attempts max; failure after retries is logged at WARNING
            with the last error. Honcho's conclude is the lossy
            outcome-aware signal — a few dropped facts won't break
            the user-model.
            """
            if getattr(self._client, "is_closed", False):
                logger.debug(
                    "honcho conclude skipped — client closed"
                )
                return

            last_err: str = ""
            for attempt in (1, 2):
                try:
                    resp = await self._client.post(
                        "/v1/conclude",
                        json={
                            "workspace": self._config.workspace,
                            "host_key": self._config.host_key,
                            "peer": "user",
                            "fact": fact,
                            "observation_mode": "inferred",
                        },
                        timeout=5.0,
                    )
                except (
                    httpx.TimeoutException,
                    httpx.NetworkError,
                ) as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    if attempt < 2:
                        continue
                    logger.warning(
                        "honcho conclude failed after %d attempts: %s",
                        attempt,
                        last_err,
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    # Non-network exception — bug, don't retry.
                    logger.warning(
                        "honcho conclude non-retryable exception: %s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    return

                if resp.status_code < 400:
                    # Success.
                    return

                # Body trimmed to 200 chars — never echo a full server
                # response into the log (may carry credentials in
                # error messages from buggy upstreams).
                body_excerpt = ""
                try:
                    body_excerpt = resp.text[:200]
                except Exception:  # noqa: BLE001
                    pass

                if 500 <= resp.status_code < 600 and attempt < 2:
                    # Retryable 5xx — try again.
                    last_err = f"HTTP {resp.status_code}: {body_excerpt}"
                    continue

                logger.warning(
                    "honcho conclude returned HTTP %d (attempt %d): %s",
                    resp.status_code,
                    attempt,
                    body_excerpt,
                )
                return

        def _handler(evt) -> None:
            try:
                signals = dict(getattr(evt, "signals", {}) or {})
                session_id = str(getattr(evt, "session_id", "") or "")
                turn_index = int(getattr(evt, "turn_index", 0) or 0)
                # INFO line so operators (and existing wiring tests) can
                # confirm the subscription fired even when signals is
                # empty / Honcho is unreachable. Single line per event;
                # kept deliberately small so high-throughput agents
                # don't flood the log.
                logger.info(
                    "honcho turn_completed received: session=%s turn=%d signals=%d",
                    session_id,
                    turn_index,
                    len(signals),
                )
                if not signals:
                    logger.debug(
                        "honcho outcome handler — empty signals; skipping conclude"
                    )
                    return
                # Render the fact text. Capped at 480 chars matching
                # SessionDB summary cap so the upstream doesn't reject
                # over-length facts. Signals are ordered for
                # deterministic dedup downstream. Values are coerced
                # to ``str()`` so non-JSON-serializable values (datetime,
                # numpy scalars, custom objects) cannot break the
                # downstream ``json.dumps`` in httpx.
                signal_parts = [
                    f"{_safe_str(k)}={_safe_str(v)}"
                    for k, v in sorted(signals.items(), key=lambda kv: str(kv[0]))
                ]
                fact = (
                    f"Turn {turn_index} (session {session_id[:8]}): "
                    + ", ".join(signal_parts)
                )
                if len(fact) > 480:
                    fact = fact[:479] + "…"

                _fire(_async_conclude(fact))
            except Exception as exc:  # noqa: BLE001 — bus handlers never re-raise
                logger.warning("honcho outcome handler failed: %s", exc)

        return bus.subscribe("turn_completed", _handler)

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
                        # T67 — Honcho-doc identity tag scopes the
                        # search to a particular user identity.
                        "identity": {
                            "type": "string",
                            "description": "Optional identity tag (e.g. email).",
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
                        # T65 — multi-pass dialectic reasoning. Each
                        # pass refines the answer against the previous.
                        # Capped at 5 to bound cost.
                        "dialectic_depth": {
                            "type": "integer",
                            "default": 1,
                            "minimum": 1,
                            "maximum": 5,
                        },
                        # T67 — request shape uplift.
                        "mode": {
                            "type": "string",
                            "description": "Reasoning mode hint (e.g. concise, deep).",
                        },
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
                name="honcho_conclude",
                description=("Persist a fact or conclusion about the user. Non-LLM."),
                parameters={
                    "type": "object",
                    "properties": {
                        "fact": {"type": "string"},
                        "peer": {"type": "string", "default": "user"},
                        # T66 — observation provenance: explicit (user
                        # said it), inferred (deduced from behavior),
                        # hypothetical (best-guess for testing).
                        "observation_mode": {
                            "type": "string",
                            "enum": ["explicit", "inferred", "hypothetical"],
                            "default": "explicit",
                        },
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
        # T4 — Hermes-doc query-adaptive scaling. Boost reasoning level
        # by query length; honoured server-side if supported, ignored
        # silently on older versions.
        reasoning_level = _adapt_reasoning_level(
            self._config.dialectic_reasoning_level,
            query or "",
            self._config.reasoning_level_cap,
        )
        try:
            resp = await self._client.post(
                "/v1/context",
                json={
                    "workspace": self._config.workspace,
                    "host_key": self._config.host_key,
                    "query": query,
                    "reasoning_level": reasoning_level,
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

    async def shutdown(self) -> None:
        """Close the httpx client + flush any pending work.

        Called by ``MemoryBridge.shutdown_all`` from the CLI's atexit
        handler (II.5). Must be idempotent — atexit may fire alongside an
        explicit cleanup path, and a second ``aclose`` on an already-
        closed client raises ``RuntimeError`` in newer httpx.

        Honcho's HTTP API has no batched /flush endpoint — sync_turn is
        already one-POST-per-call, so "flush pending writes" here reduces
        to awaiting any in-flight client requests. ``aclose`` handles
        that by draining the client's internal connection pool.
        """
        if getattr(self._client, "is_closed", False):
            return
        try:
            await self._client.aclose()
        except RuntimeError as e:
            # Tolerate "client has already been closed" races without
            # crashing atexit — we're on a best-effort path at shutdown.
            logger.debug("honcho shutdown aclose tolerated: %s", e)

    # ─── PR-6 T2.1 / T2.2 / T2.3 ambient lifecycle hooks ──────────

    async def system_prompt_block(self, *, session_id: str | None = None) -> str | None:
        """T2.1: return a brief summary of relevant Honcho insights for this session.

        Uses the existing /v1/context endpoint — the same source as ``prefetch``
        — to pull the user's current Honcho context and render it as a compact
        ambient block that lands in '## Memory context' every session.

        Returns None if the client is closed, session_id is unknown, or the
        Honcho call fails (failures are absorbed; bridge logs them).
        Caps output to ~800 chars — the bridge will hard-truncate too.
        """
        if getattr(self._client, "is_closed", False):
            return None
        try:
            resp = await self._client.get(
                "/v1/context-full",
                params={
                    "workspace": self._config.workspace,
                    "host_key": self._config.host_key,
                    "peer": "user",
                },
            )
            resp.raise_for_status()
            text = _as_text(resp.json())
        except Exception as e:  # noqa: BLE001
            logger.debug("honcho system_prompt_block failed: %s", e)
            return None
        if not text:
            return None
        # Trim to a compact ambient block; bridge enforces the hard cap.
        return text[:800]

    async def on_pre_compress(self, messages: list) -> str | None:
        """T2.2: pull key facts from Honcho so they survive compaction.

        Compaction is a chokepoint where context is discarded. This hook
        is called by :class:`opencomputer.agent.compaction.CompactionEngine`
        BEFORE summarisation runs; the returned text is injected as a
        pinned system-prompt block in the compacted message stream so
        the agent retains user-modeling facts the summariser would
        otherwise smear or drop.

        Implementation (2026-05-11): one synchronous ``/v1/context-full``
        GET against the per-profile peer. The same endpoint as
        :meth:`prefetch` and :meth:`system_prompt_block`, but consumed
        at the compaction boundary specifically so the *full* peer card
        + user representation is preserved (not just the per-turn slice
        prefetch returns).

        Failure modes (all return ``None`` so compaction proceeds
        unaffected):

        * HTTP client closed — race during agent shutdown.
        * Network error / Honcho unreachable.
        * Non-2xx response.
        * Empty / non-string payload.
        * Response text shorter than 16 chars — too little signal to
          justify a pinned block.

        The returned text is hard-capped at 2000 chars so the pinned
        block cannot itself dominate the compaction budget; the caller
        (compaction engine) may further trim.
        """
        if getattr(self._client, "is_closed", False):
            logger.debug(
                "honcho on_pre_compress: client closed; returning None"
            )
            return None

        # 2-attempt retry policy mirroring _async_conclude. Compaction
        # already only runs every N turns; one slow Honcho retry is
        # cheaper than letting the user-model context get lost on a
        # transient 5xx / connect-timeout.
        resp = None
        last_err = ""
        for attempt in (1, 2):
            try:
                resp = await self._client.get(
                    "/v1/context-full",
                    params={
                        "workspace": self._config.workspace,
                        "host_key": self._config.host_key,
                        "peer": "user",
                    },
                    timeout=_DEFAULT_REQUEST_TIMEOUT_S,
                )
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
            ) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                resp = None
                if attempt < 2:
                    continue
                logger.warning(
                    "honcho on_pre_compress failed after %d attempts: %s",
                    attempt,
                    last_err,
                )
                return None
            except Exception as exc:  # noqa: BLE001 — non-network bug, don't retry
                logger.warning(
                    "honcho on_pre_compress non-retryable exception: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                return None

            if resp.status_code < 400:
                break  # success
            if 500 <= resp.status_code < 600 and attempt < 2:
                last_err = f"HTTP {resp.status_code}"
                continue
            logger.warning(
                "honcho on_pre_compress returned HTTP %d (attempt %d)",
                resp.status_code,
                attempt,
            )
            return None

        if resp is None:
            return None

        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — non-JSON response
            logger.warning(
                "honcho on_pre_compress JSON decode failed: %s", exc
            )
            return None

        text = _as_text(payload)
        if not isinstance(text, str) or len(text.strip()) < 16:
            return None

        truncated = text[:2000].strip()
        return (
            "## Honcho user-model facts (pinned across compaction)\n\n"
            + truncated
        )

    async def on_session_end(self, session_id: str) -> None:
        """T2.3: flush any pending Honcho writes when the session closes.

        Honcho's HTTP API has no batched /flush endpoint; sync_turn is already
        one-POST-per-call. The client's connection pool is drained at process
        exit by ``shutdown`` (via MemoryBridge.shutdown_all). Here we just log
        the event and return so the bridge's fire_session_end loop can confirm
        the hook fired.
        """
        logger.debug("honcho on_session_end: session %s ended", session_id)

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
        body: dict[str, Any] = {
            "workspace": self._config.workspace,
            "host_key": self._config.host_key,
            "peer": str(args.get("peer", "user")),
            "query": str(args["query"]),
            "max_tokens": int(args.get("max_tokens", 800)),
        }
        # T67 — optional identity scope. Forwarded only when set so
        # older Honcho servers that don't recognize it stay happy.
        identity = args.get("identity")
        if identity:
            body["identity"] = str(identity)
        resp = await self._client.post("/v1/search", json=body)
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
        # T65 — clamp dialectic_depth to [1, 5]. Out-of-range values
        # silently snap to the cap so a misbehaving caller can't
        # drive multi-pass reasoning into runaway cost.
        try:
            depth_raw = int(args.get("dialectic_depth", 1))
        except (TypeError, ValueError):
            depth_raw = 1
        dialectic_depth = max(1, min(5, depth_raw))

        body: dict[str, Any] = {
            "workspace": self._config.workspace,
            "host_key": self._config.host_key,
            "peer": str(args.get("peer", "user")),
            "query": str(args["query"]),
            "dialectic_depth": dialectic_depth,
        }
        # T67 — optional mode + tokens forwarded only when set.
        if args.get("mode"):
            body["mode"] = str(args["mode"])
        if args.get("max_tokens") is not None:
            try:
                body["max_tokens"] = int(args["max_tokens"])
            except (TypeError, ValueError):
                pass
        resp = await self._client.post("/v1/chat", json=body)
        resp.raise_for_status()
        return _as_text(resp.json())

    async def _conclude(self, args: dict[str, Any]) -> str:
        # T66 — observation_mode: explicit (default) | inferred |
        # hypothetical. Unknown values fall back to explicit so the
        # downstream Honcho server always sees a known enum value.
        _OBS_MODES = {"explicit", "inferred", "hypothetical"}
        mode = str(args.get("observation_mode", "explicit"))
        if mode not in _OBS_MODES:
            mode = "explicit"

        resp = await self._client.post(
            "/v1/conclude",
            json={
                "workspace": self._config.workspace,
                "host_key": self._config.host_key,
                "peer": str(args.get("peer", "user")),
                "fact": str(args["fact"]),
                "observation_mode": mode,
            },
        )
        resp.raise_for_status()
        return _as_text(resp.json())


def _safe_str(value: Any) -> str:
    """Coerce ``value`` to a short str safe for fact rendering.

    Non-string values are rendered via ``repr()`` truncated at 80
    chars so a malicious / buggy producer can't slip an unbounded
    object into the fact text. Empty string returned on any failure.
    """
    try:
        if isinstance(value, str):
            return value[:80]
        return repr(value)[:80]
    except Exception:  # noqa: BLE001
        return ""


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
