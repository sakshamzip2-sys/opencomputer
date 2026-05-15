"""langfuse plugin — bridge OC's LLMCallEvent stream to a langfuse trace.

When ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` are set,
registers a subscriber on ``opencomputer.inference.observability``
that forwards every recorded LLM call to langfuse via its SDK.

When env vars are unset OR the langfuse SDK is not installed, the
plugin loads as inert (registers nothing).

Configuration env:

- ``LANGFUSE_PUBLIC_KEY`` (required)
- ``LANGFUSE_SECRET_KEY`` (required)
- ``LANGFUSE_BASE_URL`` (optional; default ``https://cloud.langfuse.com``)
- ``LANGFUSE_FLUSH_AT`` (optional; default 15)

Self-host via ``oc langfuse up`` to run langfuse locally on
``http://localhost:3000`` and point the env vars at it.

Per-turn parent span (2026-05-11)
---------------------------------
:func:`open_turn_span` is a context manager called from
:class:`opencomputer.agent.loop.AgentLoop.run_conversation` at the
top of every user turn. It opens a langfuse span representing the
whole turn; all child observations (generations, tool spans, Honcho
calls) created during the with-block nest under the parent via
langfuse v4's OTel context propagation, so the langfuse UI renders
one tree per turn rather than a flat list of generations.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger("opencomputer.ext.langfuse")

# Module-level state — set during ``register`` if config is complete.
_client: Any = None
_subscriber_handle: Any = None


def _is_host_reachable(base_url: str, *, timeout: float = 1.5) -> bool:
    """Quick TCP-level sanity check before handing a host to Langfuse(...).

    Without this, an unreachable host (typical case: ``LANGFUSE_BASE_URL``
    points at a self-hosted ``http://localhost:3000`` stack the user
    forgot to start) makes Langfuse's background OTEL exporter spam
    "Connection refused — retrying in N.NNs" into the user's terminal
    every couple seconds, indefinitely. The exporter runs after the
    constructor returns, so the existing try/except can't catch it.

    A fast pre-check sidesteps the issue: if we can't even open a TCP
    connection, treat the plugin as inert and log once.
    """
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(base_url)
        host = parsed.hostname or ""
        if not host:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _build_client() -> Any:
    """Build a langfuse client if config + SDK + reachable host, else None."""
    pub = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    sec = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not pub or not sec:
        logger.debug(
            "langfuse plugin inert — LANGFUSE_PUBLIC_KEY/SECRET_KEY not set"
        )
        return None

    try:
        from langfuse import Langfuse  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "langfuse plugin enabled (env keys present) but the langfuse SDK "
            "is not installed. Run `pip install langfuse` to enable."
        )
        return None

    base_url = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").strip()

    # Pre-flight reachability check — without this, Langfuse(...) returns
    # an object whose background OTEL exporter retries forever and spams
    # the user's terminal with "Connection refused" every 1-2 seconds.
    # 2026-05-10 — closes Saksham's spam-after-langfuse-install incident.
    if not _is_host_reachable(base_url):
        logger.warning(
            "langfuse plugin inert — host %s is not reachable. "
            "Start the langfuse stack (`oc langfuse up`) or set "
            "LANGFUSE_BASE_URL to a healthy endpoint. The plugin will "
            "register itself on next OC start once the host is up.",
            base_url,
        )
        return None

    flush_at = int(os.environ.get("LANGFUSE_FLUSH_AT", "15") or "15")
    try:
        return Langfuse(
            public_key=pub,
            secret_key=sec,
            host=base_url,
            flush_at=flush_at,
        )
    except Exception as exc:  # noqa: BLE001 — must not break plugin load
        logger.warning("langfuse client init failed: %s", exc)
        return None


def _send_event(event: Any) -> None:
    """Forward one LLMCallEvent to langfuse as a generation observation.

    Uses the langfuse v4 SDK ``start_observation(as_type="generation")``
    surface. v3's standalone ``client.generation()`` was removed in v4
    in favour of OTel-style observations.

    2026-05-11 — also threads OC's per-turn ``trace_id`` (from the
    contextvar populated at the top of ``AgentLoop.run_conversation``)
    into the langfuse metadata. The langfuse server doesn't honor it
    as a trace grouping key directly — that requires an OTel parent
    span at the start of the turn — but it makes per-turn correlation
    queryable from the langfuse UI (filter generations by metadata
    ``oc_trace_id``) AND from any external query against langfuse's
    public API.
    """
    if _client is None:
        return
    try:
        usage_details = {
            "input": event.input_tokens,
            "output": event.output_tokens,
            "cache_read_input_tokens": event.cache_read_tokens,
            "cache_creation_input_tokens": event.cache_creation_tokens,
            "total": event.input_tokens + event.output_tokens,
        }
        cost_details: dict[str, Any] = {}
        if event.cost_usd is not None:
            cost_details["total"] = event.cost_usd
        metadata: dict[str, Any] = {
            "provider": event.provider,
            "site": event.site,
            "latency_ms": event.latency_ms,
        }
        oc_trace_id = getattr(event, "trace_id", None)
        if oc_trace_id:
            metadata["oc_trace_id"] = oc_trace_id
        gen = _client.start_observation(
            as_type="generation",
            name=f"oc-{event.site or 'agent_loop'}",
            model=event.model,
            usage_details=usage_details,
            cost_details=cost_details or None,
            input=getattr(event, "input_preview", None),
            output=getattr(event, "output_preview", None),
            metadata=metadata,
        )
        # End the observation immediately — we already have all the data
        # from the LLMCallEvent; no need to keep the span open.
        gen.end()
        # Index the langfuse trace id behind OC's correlation key so
        # later score-writes can land on the right server-side trace.
        if oc_trace_id:
            try:
                langfuse_trace_id = (
                    getattr(gen, "trace_id", None)
                    or getattr(getattr(gen, "trace", None), "id", None)
                )
                if langfuse_trace_id:
                    _trace_index[oc_trace_id] = str(langfuse_trace_id)
                    _enforce_trace_index_cap()
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "langfuse trace-id index update failed: %s", exc
                )
    except Exception as exc:  # noqa: BLE001 — telemetry must not break the loop
        logger.warning("langfuse forward failed: %s", exc)


# OC-trace-id → langfuse-trace-id index, populated lazily on first
# observation per turn. Bounded to avoid unbounded growth in
# long-running gateway processes.
_trace_index: dict[str, str] = {}
_TRACE_INDEX_CAP = 4096


def _enforce_trace_index_cap() -> None:
    """Trim ``_trace_index`` to at most ``_TRACE_INDEX_CAP`` entries.

    Eviction is FIFO via ``dict`` insertion order — Python guarantees
    insertion-order iteration since 3.7. Cheap; runs O(overflow).
    """
    overflow = len(_trace_index) - _TRACE_INDEX_CAP
    if overflow <= 0:
        return
    # Pop the N oldest keys.
    oldest_keys = list(_trace_index.keys())[:overflow]
    for key in oldest_keys:
        _trace_index.pop(key, None)


@contextlib.contextmanager
def open_turn_span(
    *,
    session_id: str,
    oc_trace_id: str,
    turn_label: str = "oc-turn",
) -> Iterator[Any]:
    """Open a parent langfuse span for one OC agent turn.

    Yields the span object (or ``None`` when langfuse is inert / the
    span open fails). All child observations created during the
    with-block nest under this span via langfuse v4's OTel context
    propagation — generations, tool spans, and any future span types
    are grouped automatically as long as they're emitted from the
    same asyncio task that opened this context.

    Side effect: when the span opens successfully and ``oc_trace_id``
    is non-empty, we index the langfuse trace id behind the OC trace
    id in ``_trace_index`` so :func:`score_trace` can later post a
    user-decision score against the right server-side trace.

    Failure semantics: any exception during open / end is logged at
    WARNING and the caller continues — observability never blocks
    the turn.

    Args:
        session_id: OC session id; surfaced as metadata + langfuse
            session_id so the langfuse UI can filter by session.
        oc_trace_id: The contextvar trace id set by
            ``run_conversation``. Empty string disables the trace-id
            indexing.
        turn_label: Span name; defaults to ``"oc-turn"`` for grep-ability
            in the langfuse UI. Delegate / sub-agent callers override
            with ``"oc-delegate"`` etc.

    Yields:
        The langfuse span object, or ``None`` when langfuse is inert
        or span construction failed.
    """
    if _client is None:
        yield None
        return

    try:
        span = _client.start_observation(
            as_type="span",
            name=turn_label,
            metadata={
                "session_id": session_id,
                "oc_trace_id": oc_trace_id,
            },
        )
    except Exception as exc:  # noqa: BLE001 — observability never blocks the turn
        logger.warning("langfuse open_turn_span failed: %s", exc)
        yield None
        return

    # Index for downstream score_trace lookups before the span ends,
    # so a fast accept/reject doesn't race the span teardown.
    try:
        langfuse_tid = (
            getattr(span, "trace_id", None)
            or getattr(getattr(span, "trace", None), "id", None)
        )
        if langfuse_tid and oc_trace_id:
            _trace_index[oc_trace_id] = str(langfuse_tid)
            _enforce_trace_index_cap()
    except Exception as exc:  # noqa: BLE001
        logger.debug("langfuse trace-id index update at span-open failed: %s", exc)

    try:
        yield span
    finally:
        try:
            span.end()
        except Exception as exc:  # noqa: BLE001
            logger.warning("langfuse turn span end failed: %s", exc)


def score_trace(oc_trace_id: str, decision: str) -> None:
    """Write a langfuse score against the trace identified by ``oc_trace_id``.

    Called by :class:`opencomputer.agent.evolution_orchestrator.EvolutionOrchestrator`
    when a user accepts / rejects / edits a proposed skill. Maps the
    user decision to a 0-1 numeric score so langfuse aggregation queries
    can compute accept-rates over time.

    Score semantics (locked to keep historical comparisons coherent):

    * ``"accepted"`` → 1.0
    * ``"edited"`` → 0.5
    * ``"rejected"`` → 0.0
    * any other value (incl. ``"deferred"``) → no score written

    Failures swallowed; logged at WARNING. The score side-channel must
    never block the orchestrator.
    """
    if _client is None or not oc_trace_id:
        return
    scores = {"accepted": 1.0, "edited": 0.5, "rejected": 0.0}
    if decision not in scores:
        return
    langfuse_trace_id = _trace_index.get(oc_trace_id)
    if not langfuse_trace_id:
        # We never saw a generation for this trace — nothing to score.
        # Could happen if the proposal was extracted offline or if
        # langfuse came up after the turn ran.
        logger.debug(
            "langfuse score_trace: no langfuse trace mapped for "
            "oc_trace_id=%s; skipping",
            oc_trace_id,
        )
        return
    try:
        # langfuse v4: client.create_score (synchronous; batched
        # internally via the SDK's flush queue).
        _client.create_score(
            trace_id=langfuse_trace_id,
            name="skill_review_decision",
            value=scores[decision],
            comment=decision,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("langfuse create_score failed: %s", exc)


async def _on_pre_tool_use(ctx: Any) -> Any:  # noqa: ANN001
    """Open a tool span on each PRE_TOOL_USE hook fire.

    Stashes the start time + the langfuse span on the ctx so
    ``_on_post_tool_use`` can close it. Best-effort: any exception
    is logged but never blocks the agent loop.
    """
    if _client is None:
        return None
    try:
        tc = getattr(ctx, "tool_call", None)
        if tc is None:
            return None
        span = _client.start_observation(
            as_type="span",
            name=f"tool:{getattr(tc, 'name', 'unknown')}",
            input=getattr(tc, "arguments", None),
            metadata={
                "session_id": getattr(ctx, "session_id", ""),
                "tool_call_id": getattr(tc, "id", None),
            },
        )
        # Stash the span on the tool_call for the POST hook to find.
        # Using a module-level dict keyed by id() avoids mutating the
        # immutable dataclass.
        _open_tool_spans[id(tc)] = span
    except Exception as exc:  # noqa: BLE001 — telemetry must not block tools
        logger.warning("langfuse PRE_TOOL_USE hook failed: %s", exc)
    return None


async def _on_post_tool_use(ctx: Any) -> Any:  # noqa: ANN001
    """Close the tool span opened by ``_on_pre_tool_use``."""
    if _client is None:
        return None
    try:
        tc = getattr(ctx, "tool_call", None)
        tr = getattr(ctx, "tool_result", None)
        if tc is None:
            return None
        span = _open_tool_spans.pop(id(tc), None)
        if span is None:
            return None
        # Truncate output to 1500 chars (same cap as LLM previews).
        out_str: str | None = None
        if tr is not None:
            content = getattr(tr, "content", None)
            if content is not None:
                out_str = str(content)[:1500]
        span.update(
            output=out_str,
            metadata={
                "is_error": bool(getattr(tr, "is_error", False)),
                "duration_ms": getattr(ctx, "duration_ms", None),
            },
        )
        span.end()
    except Exception as exc:  # noqa: BLE001
        logger.warning("langfuse POST_TOOL_USE hook failed: %s", exc)
    return None


# Map id(ToolCall) → langfuse span. Bounded by the agent loop's inflight
# tool calls (typically 1-8 in parallel), so unbounded growth isn't a
# concern.
_open_tool_spans: dict[int, Any] = {}


def register(api: Any) -> None:  # noqa: ANN001 — duck-typed PluginAPI
    """Wire the LLMCallEvent → langfuse subscriber + tool-span hooks
    if config is complete.
    """
    global _client, _subscriber_handle
    _client = _build_client()
    if _client is None:
        return  # inert mode

    # Lazy import to avoid importing opencomputer.* until we have a client.
    from opencomputer.inference.observability import register_subscriber
    from plugin_sdk.hooks import HookEvent, HookSpec

    register_subscriber(_send_event)
    _subscriber_handle = _send_event

    # Tool spans — open on PRE_TOOL_USE, close on POST_TOOL_USE.
    api.register_hook(HookSpec(
        event=HookEvent.PRE_TOOL_USE,
        handler=_on_pre_tool_use,
        fire_and_forget=True,
    ))
    api.register_hook(HookSpec(
        event=HookEvent.POST_TOOL_USE,
        handler=_on_post_tool_use,
        fire_and_forget=True,
    ))

    logger.info(
        "langfuse plugin: subscribing to LLMCallEvent stream + tool hooks "
        "(host=%s)",
        os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
    )

    # §9.8 profile-handoff: rebuild langfuse client against the new
    # profile's LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY /
    # LANGFUSE_BASE_URL on profile swap. The dotenv handler at
    # priority 20 has already updated os.environ.
    def _rebind_langfuse(new_home, old_home):  # noqa: ANN001, ARG001
        global _client
        try:
            new_client = _build_client()
        except Exception:
            logger.warning(
                "langfuse rebind: _build_client raised — keeping prior client",
                exc_info=True,
            )
            return
        _client = new_client
        if new_client is None:
            logger.info(
                "langfuse rebind: new profile has no credentials — "
                "langfuse went INERT for this session",
            )
        else:
            logger.info(
                "langfuse rebind: client rebuilt against new profile (host=%s)",
                os.environ.get(
                    "LANGFUSE_BASE_URL", "https://cloud.langfuse.com",
                ),
            )

    if hasattr(api, "register_profile_rebind_handler"):
        try:
            api.register_profile_rebind_handler(
                "langfuse", _rebind_langfuse, priority=158,
            )
        except Exception:
            pass


__all__ = ["open_turn_span", "register", "score_trace"]
