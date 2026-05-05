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


def _build_client() -> Any:
    """Build a langfuse client if config + SDK are present, else None."""
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
    """Forward one LLMCallEvent to langfuse as a generation trace."""
    if _client is None:
        return
    try:
        ts_iso = event.ts.isoformat() if hasattr(event.ts, "isoformat") else str(event.ts)
        usage = {
            "input": event.input_tokens,
            "output": event.output_tokens,
            "total": event.input_tokens + event.output_tokens,
            "unit": "TOKENS",
        }
        # langfuse's SDK shape: client.generation(...) creates a trace
        # implicitly when no trace_id is provided.
        _client.generation(
            name=f"oc-{event.site or 'agent_loop'}",
            model=event.model,
            start_time=ts_iso,
            usage_details=usage,
            metadata={
                "provider": event.provider,
                "site": event.site,
                "cache_creation_tokens": event.cache_creation_tokens,
                "cache_read_tokens": event.cache_read_tokens,
                "latency_ms": event.latency_ms,
                "cost_usd": event.cost_usd,
            },
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must not break the loop
        logger.warning("langfuse forward failed: %s", exc)


def register(api: Any) -> None:  # noqa: ANN001 — duck-typed PluginAPI
    """Wire the LLMCallEvent → langfuse subscriber if config is complete."""
    global _client, _subscriber_handle
    _client = _build_client()
    if _client is None:
        return  # inert mode

    # Lazy import to avoid importing opencomputer.* until we have a client.
    from opencomputer.inference.observability import register_subscriber

    register_subscriber(_send_event)
    _subscriber_handle = _send_event
    logger.info(
        "langfuse plugin: subscribing to LLMCallEvent stream "
        "(host=%s)",
        os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
    )


__all__ = ["register"]
