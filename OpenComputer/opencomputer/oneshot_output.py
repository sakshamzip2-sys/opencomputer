"""Non-interactive output formatting for ``oc oneshot`` / ``oc chat -q``.

v1.1 plan-1 M2.2 (2026-05-09). Pulls the per-mode emission logic out of
:func:`opencomputer.cli._run_oneshot_turn` so:

1. The CLI helper stays focused on agent-loop wiring.
2. Each output mode (text, json, stream-json) has one tested
   implementation instead of being scattered through a 100-line
   function.
3. Tests can drive the formatters directly without spinning up a
   provider.

Modes are defined in :class:`opencomputer.headless.OutputMode`. The
formatters all share a common :class:`OneshotResult` shape so callers
don't have to know which mode produced the output.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from opencomputer.headless import OutputMode


@dataclass
class OneshotResult:
    """Aggregated summary of a single non-interactive turn.

    ``llm_events`` holds the per-call events captured during the run
    (one entry per provider request). The aggregate fields are filled
    in :meth:`finalize` after the loop finishes.

    ``error_code`` is set when the run failed in a structured way the
    caller wants to communicate (e.g. ``"provider_error"`` or
    ``"keyboard_interrupt"``); ``None`` on a clean exit.
    """

    session_id: str = ""
    final_message: str = ""
    num_turns: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cost_usd: float = 0.0
    llm_events: list[dict[str, Any]] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    def record_event(self, event_dict: dict[str, Any]) -> None:
        """Add one :class:`LLMCallEvent` (already serialized to dict).

        Mutates the running aggregate so callers can read totals
        immediately after the last event arrives without a separate
        finalize pass.
        """
        self.llm_events.append(event_dict)
        self.num_turns += 1
        self.total_input_tokens += int(event_dict.get("input_tokens", 0) or 0)
        self.total_output_tokens += int(event_dict.get("output_tokens", 0) or 0)
        self.total_cache_creation_tokens += int(
            event_dict.get("cache_creation_tokens", 0) or 0
        )
        self.total_cache_read_tokens += int(event_dict.get("cache_read_tokens", 0) or 0)
        cost = event_dict.get("cost_usd")
        if cost is not None:
            try:
                self.total_cost_usd += float(cost)
            except (TypeError, ValueError):
                pass

    def to_summary_dict(self) -> dict[str, Any]:
        """Return the JSON-mode payload (one object at end of run)."""
        out: dict[str, Any] = {
            "session_id": self.session_id,
            "num_turns": self.num_turns,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cache_creation_tokens": self.total_cache_creation_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "final_message": self.final_message,
        }
        if self.error_code:
            out["error"] = {
                "code": self.error_code,
                "message": self.error_message or "",
            }
        return out


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Serialize an ``LLMCallEvent`` (or any dataclass) to a JSON-safe dict.

    Handles the ``ts: datetime`` field by isoformatting it. Centralized
    so stream-json + json modes both emit the same shape.
    """
    try:
        d = dataclasses.asdict(event)
    except TypeError:
        # Already a dict (test seam) — pass through.
        d = dict(event) if isinstance(event, dict) else {"raw": str(event)}
    ts = d.get("ts")
    if isinstance(ts, datetime):
        d["ts"] = ts.isoformat()
    return d


@contextmanager
def stream_subscriber(result: OneshotResult, mode: OutputMode):
    """Register an LLMCallEvent subscriber for the duration of a oneshot run.

    Two responsibilities:

    1. **Always** capture each event into ``result.llm_events`` so
       ``json`` mode has the data it needs at finalize time.
    2. When ``mode == STREAM_JSON``, also emit each event as a single
       NDJSON line on stdout, prefixed with ``"event": "llm_call"``
       so consumers can tell event lines from the final summary line.

    Subscriber registration happens through the existing
    :mod:`opencomputer.inference.observability` mechanism so we don't
    duplicate the JSONL-file write — that file write stays intact via
    ``record_llm_call`` regardless of stream mode.
    """
    from opencomputer.inference.observability import (
        register_subscriber,
        unregister_subscriber,
    )

    def _subscriber(event: Any) -> None:
        d = _event_to_dict(event)
        result.record_event(d)
        if mode is OutputMode.STREAM_JSON:
            line = {"event": "llm_call", **d}
            sys.stdout.write(json.dumps(line) + "\n")
            sys.stdout.flush()

    register_subscriber(_subscriber)
    try:
        yield
    finally:
        unregister_subscriber(_subscriber)


def emit_final(
    result: OneshotResult,
    mode: OutputMode,
    *,
    out=None,
) -> None:
    """Render the end-of-run output for ``mode``.

    * ``TEXT`` — print the final assistant message (current behavior).
    * ``JSON`` — print one JSON object summary.
    * ``STREAM_JSON`` — print one final summary line tagged
      ``"event": "summary"``. Per-call events were already streamed
      via :func:`stream_subscriber` during the run.

    ``out`` defaults to ``sys.stdout``; tests pass an in-memory buffer.
    """
    sink = out if out is not None else sys.stdout
    if mode is OutputMode.TEXT:
        if result.final_message:
            sink.write(result.final_message + "\n")
        return
    if mode is OutputMode.JSON:
        sink.write(json.dumps(result.to_summary_dict()) + "\n")
        return
    if mode is OutputMode.STREAM_JSON:
        line = {"event": "summary", **result.to_summary_dict()}
        sink.write(json.dumps(line) + "\n")
        return
    # Defensive — should be unreachable since OutputMode is closed.
    raise ValueError(f"unknown OutputMode: {mode!r}")


__all__ = [
    "OneshotResult",
    "emit_final",
    "stream_subscriber",
]
