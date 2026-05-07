"""Mid-run nudges via ``/steer`` — round 2a P-2 + PR-A Feature 1.

The ``SteerRegistry`` is a tiny thread-safe per-session-id store of
"pending nudges" — short user prompts the agent should incorporate at
the next turn boundary. The agent loop calls ``consume(session_id)``
between tool dispatch and the next LLM request; if a nudge is present
it is prepended to the next provider call as a synthetic user message::

    <USER-NUDGE>: {nudge_text}
    (latest-wins; previous nudges discarded if any.)

Latest-wins semantics: a fresh ``submit`` while one is already pending
*replaces* the previous nudge and emits a WARNING-level log. The
previous nudge is silently discarded (not queued, not concatenated) so
the user always knows the agent will act on the most recent intent.

PR-A Feature 1 — Steer Replan-with-Context (2026-05-07):

- Each ``submit`` also signals a per-session ``asyncio.Event`` (the
  *cancel event*) that the agent loop's tool dispatcher watches. When
  the event fires mid-flight, in-progress async-yielding tools are
  cancelled cooperatively and a synthetic ``<INTERRUPTED-BY-STEER>``
  result is emitted in their place. The next iteration's between-turn
  consume sees the cancel state and uses ``<USER-INTERRUPT>`` instead
  of ``<USER-NUDGE>`` as the prefix so the model knows tools didn't
  finish.
- :class:`SteerBuffer` accumulates inbound messages that arrive while a
  cancel is pending so they all get merged into the replan rather than
  being processed as separate sequential turns. Cap=5; drop-oldest;
  drops are logged.

Cancellation scope (honest documentation):

- **Async-yielding tools** (Bash, WebFetch, WebSearch, browser-control,
  MCP) cancel at the next ``await`` checkpoint, typically <100 ms.
- **Synchronous tools** (Read, Glob, Grep) finish their current
  blocking syscall before honoring ``CancelledError``; for these,
  cancellation behaves as "skip the next call in the batch" rather
  than "interrupt the in-progress call."
- **Bash partial stdout** is captured and returned as part of the
  cancelled ``ToolResult`` (existing Bash-tool behaviour). Other tools
  emit only ``<INTERRUPTED-BY-STEER>`` markers without partial output.

Concurrency: the registry can be touched by any number of gateway
adapters (Telegram, Discord, wire-server JSON-RPC) plus the in-process
agent loop, all on different threads/event loops. We hold a single
process-wide :class:`threading.Lock` around every read or write —
contention is trivial (we're storing strings + asyncio.Event refs
keyed by id) so a finer-grained lock would be premature.

API surface:

- :meth:`SteerRegistry.submit(session_id, nudge)` — store + override + signal cancel
- :meth:`SteerRegistry.consume(session_id)` — return + clear pending nudge
- :meth:`SteerRegistry.has_pending(session_id)` — peek without clearing
- :meth:`SteerRegistry.clear(session_id)` — drop nudge without consuming
- :meth:`SteerRegistry.cancel_event(session_id)` — lazy-create the cancel Event
- :meth:`SteerRegistry.has_cancel_listener(session_id)` — peek event presence
- :meth:`SteerRegistry.reset_cancel(session_id)` — clear the cancel Event flag
- :class:`SteerBuffer` — per-session inbound-message buffer for replan-merge

Process singletons :data:`default_registry` and :data:`default_buffer`
are what every adapter + the agent loop should reference; tests
construct fresh instances when they need isolation.
"""

from __future__ import annotations

import asyncio
import logging
import threading

_log = logging.getLogger("opencomputer.agent.steer")


class SteerRegistry:
    """Per-session-id pending-nudge store. Latest-wins, thread-safe."""

    def __init__(self) -> None:
        self._pending: dict[str, str] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._lock = threading.Lock()

    def submit(self, session_id: str, nudge: str) -> None:
        """Store ``nudge`` for ``session_id``. Overrides any pending nudge.

        Latest-wins: if a nudge is already pending for this session, it
        is replaced and a warning is logged. The previous nudge is
        discarded (not queued, not concatenated) — the user's most
        recent intent always wins.

        Empty / whitespace-only nudges are dropped silently (no
        override is performed); a follow-up valid submit still works.

        PR-A Feature 1: also sets the per-session cancel event if one
        has been allocated, so an in-flight tool dispatch can react.
        """
        if not session_id:
            raise ValueError("steer.submit: session_id must be non-empty")
        if nudge is None:
            raise ValueError("steer.submit: nudge must not be None")
        normalized = nudge.strip()
        if not normalized:
            _log.debug(
                "steer.submit ignored: empty nudge for session %s", session_id
            )
            return
        with self._lock:
            previous = self._pending.get(session_id)
            self._pending[session_id] = normalized
            event = self._cancel_events.get(session_id)
            # PR-A Feature 1: allocate-if-missing so the cancel signal is
            # never silently lost. The agent loop watches the same lazily-
            # created instance via cancel_event(); a submit-before-watch
            # case (rare but real) ends up with the event already set
            # when the watcher allocates.
            if event is None:
                event = asyncio.Event()
                self._cancel_events[session_id] = event
        if previous is not None:
            _log.warning(
                "steer override: previous nudge discarded for session %s",
                session_id,
            )
        if not event.is_set():
            event.set()

    def consume(self, session_id: str) -> str | None:
        """Pop the pending nudge for ``session_id`` (or ``None`` if absent).

        After this call, ``has_pending(session_id)`` is False until the
        next ``submit`` for that id. Designed to be called from the
        agent loop's between-turn checkpoint.
        """
        if not session_id:
            return None
        with self._lock:
            return self._pending.pop(session_id, None)

    def has_pending(self, session_id: str) -> bool:
        """Return True if a nudge is pending. Does not clear it."""
        if not session_id:
            return False
        with self._lock:
            return session_id in self._pending

    def clear(self, session_id: str) -> None:
        """Drop any pending nudge for ``session_id`` without consuming."""
        if not session_id:
            return
        with self._lock:
            self._pending.pop(session_id, None)

    # ------------------------------------------------------------------
    # PR-A Feature 1 — cancel-event API
    # ------------------------------------------------------------------

    def cancel_event(self, session_id: str) -> asyncio.Event:
        """Return (lazy-creating) the per-session cancel event.

        Allocated on first call; the same Event instance is returned on
        subsequent calls. The agent loop's tool dispatcher allocates +
        watches; ``submit`` sets; ``reset_cancel`` clears.
        """
        if not session_id:
            raise ValueError("steer.cancel_event: session_id must be non-empty")
        with self._lock:
            event = self._cancel_events.get(session_id)
            if event is None:
                event = asyncio.Event()
                self._cancel_events[session_id] = event
            return event

    def has_cancel_listener(self, session_id: str) -> bool:
        """Return True if a cancel event has been allocated for this session.

        Public API to avoid private-attribute access from external callers
        (CLI slash handler, gateway dispatch).
        """
        if not session_id:
            return False
        with self._lock:
            return session_id in self._cancel_events

    def reset_cancel(self, session_id: str) -> None:
        """Clear the cancel event flag without removing the listener.

        Called by the agent loop after a steer-driven cancel has been
        consumed and the replan turn is starting. The Event instance is
        kept so subsequent submits can re-set it without re-allocation.
        """
        if not session_id:
            return
        with self._lock:
            event = self._cancel_events.get(session_id)
        if event is not None:
            event.clear()


#: Process-wide singleton. Every adapter (CLI, Telegram, wire) and the
#: agent loop should reference this instance — tests construct private
#: ``SteerRegistry()`` instances when they need isolation.
default_registry = SteerRegistry()


def format_nudge_message(nudge: str, *, was_interrupted: bool = False) -> str:
    """Render a stored nudge into the synthetic user-message body.

    Centralised here so the prefix used by :class:`AgentLoop` and the
    Telegram acknowledgement / wire response stay in sync — tests pin
    the exact string.

    PR-A Feature 1: ``was_interrupted=True`` switches the prefix to
    ``<USER-INTERRUPT>`` so the model knows tools were cancelled rather
    than completing normally.
    """
    prefix = "<USER-INTERRUPT>" if was_interrupted else "<USER-NUDGE>"
    return (
        f"{prefix}: {nudge}\n"
        "(latest-wins; previous nudges discarded if any.)"
    )


# ---------------------------------------------------------------------------
# PR-A Feature 1 — SteerBuffer
# ---------------------------------------------------------------------------


class SteerBuffer:
    """Per-session message buffer for steer-cancel replan merge.

    When a message arrives during a cancel-pending dispatch (i.e. the
    cancel event has fired but the agent loop hasn't yet consumed it),
    the gateway dispatcher appends it here instead of triggering a new
    agent run. On between-turn consume in the loop, the drained buffer
    is concatenated to any explicit /steer text so the next-turn replan
    sees all accumulated context together.

    Cap: 5 messages per session. Drop-oldest. Drops are logged.

    Concurrency: same single-lock pattern as :class:`SteerRegistry` —
    contention is trivial.
    """

    MAX: int = 5

    def __init__(self) -> None:
        self._buffers: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def append(self, session_id: str, text: str) -> int:
        """Append text. Returns count of older messages dropped (0 if none)."""
        if not session_id or not text:
            return 0
        with self._lock:
            buf = self._buffers.setdefault(session_id, [])
            buf.append(text)
            dropped = max(0, len(buf) - self.MAX)
            if dropped > 0:
                del buf[:dropped]
                _log.warning(
                    "steer-buffer: dropped %d oldest message(s) for session %s "
                    "(cap=%d)",
                    dropped,
                    session_id,
                    self.MAX,
                )
        return dropped

    def drain(self, session_id: str) -> str:
        """Return concatenated buffer (separator '\\n---\\n'); clear."""
        if not session_id:
            return ""
        with self._lock:
            buf = self._buffers.pop(session_id, [])
        if not buf:
            return ""
        return "\n---\n".join(buf)

    def has_pending(self, session_id: str) -> bool:
        """Return True if any buffered messages exist for this session."""
        if not session_id:
            return False
        with self._lock:
            return bool(self._buffers.get(session_id))


#: Process-wide buffer singleton — used by gateway dispatch + agent loop.
default_buffer = SteerBuffer()


__all__ = [
    "SteerBuffer",
    "SteerRegistry",
    "default_buffer",
    "default_registry",
    "format_nudge_message",
]
