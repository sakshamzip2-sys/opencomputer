"""Mid-run nudges via ``/steer`` — round 2a P-2.

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

Concurrency: the registry can be touched by any number of gateway
adapters (Telegram, Discord, wire-server JSON-RPC) plus the in-process
agent loop, all on different threads/event loops. We hold a single
process-wide :class:`threading.Lock` around every read or write —
contention is trivial (we're storing strings keyed by id) so a finer-
grained lock would be premature.

API surface intentionally tiny:

- :meth:`SteerRegistry.submit(session_id, nudge)` — store + override
- :meth:`SteerRegistry.consume(session_id)` — return + clear
- :meth:`SteerRegistry.has_pending(session_id)` — peek without clearing
- :meth:`SteerRegistry.clear(session_id)` — drop without consuming

The module exposes a process-singleton :data:`default_registry` that
all callers should use; tests construct fresh ``SteerRegistry()``
instances when they need isolation.
"""

from __future__ import annotations

import logging
import threading

_log = logging.getLogger("opencomputer.agent.steer")


class SteerRegistry:
    """Per-session-id pending-nudge store. Latest-wins, thread-safe."""

    def __init__(self) -> None:
        self._pending: dict[str, str] = {}
        self._lock = threading.Lock()

    def submit(self, session_id: str, nudge: str) -> None:
        """Store ``nudge`` for ``session_id``. Overrides any pending nudge.

        Latest-wins: if a nudge is already pending for this session, it
        is replaced and a warning is logged. The previous nudge is
        discarded (not queued, not concatenated) — the user's most
        recent intent always wins.

        Empty / whitespace-only nudges are dropped silently (no
        override is performed); a follow-up valid submit still works.
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
        if previous is not None:
            _log.warning(
                "steer override: previous nudge discarded for session %s",
                session_id,
            )

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


#: Process-wide singleton. Every adapter (CLI, Telegram, wire) and the
#: agent loop should reference this instance — tests construct private
#: ``SteerRegistry()`` instances when they need isolation.
default_registry = SteerRegistry()


def format_nudge_message(nudge: str) -> str:
    """Render a stored nudge into the synthetic user-message body.

    Centralised here so the prefix used by :class:`AgentLoop` and the
    Telegram acknowledgement / wire response stay in sync — tests pin
    the exact string.
    """
    return (
        f"<USER-NUDGE>: {nudge}\n"
        "(latest-wins; previous nudges discarded if any.)"
    )


__all__ = ["SteerRegistry", "default_registry", "format_nudge_message"]
