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
"""

from __future__ import annotations

import logging
import os
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
        gen = _client.start_observation(
            as_type="generation",
            name=f"oc-{event.site or 'agent_loop'}",
            model=event.model,
            usage_details=usage_details,
            cost_details=cost_details or None,
            input=getattr(event, "input_preview", None),
            output=getattr(event, "output_preview", None),
            metadata={
                "provider": event.provider,
                "site": event.site,
                "latency_ms": event.latency_ms,
            },
        )
        # End the observation immediately — we already have all the data
        # from the LLMCallEvent; no need to keep the span open.
        gen.end()
    except Exception as exc:  # noqa: BLE001 — telemetry must not break the loop
        logger.warning("langfuse forward failed: %s", exc)


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


__all__ = ["register"]
