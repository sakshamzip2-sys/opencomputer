"""Pre→post-task bridge — module-level session state for the
social-traces plugin.

## Why this exists

Phase 4 surfaced an architectural finding: ``runtime.custom["trace_used"]``
set by the BEFORE_TASK hook does NOT propagate back to the post-task
subscriber. Two reasons:

1. The agent loop calls ``dataclasses.replace(runtime, custom={...})``
   on entry to ``run_conversation``, creating a new dict that
   subsequent mutations land on — the original caller's runtime never
   sees them.
2. ``SessionEndEvent`` (the bus payload subscribers receive) strips
   the runtime entirely. The subscriber has no way to read
   ``runtime.custom`` at session-end time.

This module is **the bridge**: BEFORE_TASK calls
:func:`set_trace_used` per-turn; the post-task subscriber calls
:func:`pop_session` on session-end to read + clear.

## Design

Module-level :class:`dict` keyed by session_id, guarded by an
:class:`threading.RLock`. Lives in-process for the lifetime of the
agent process — same lifecycle as ``runtime.custom`` would have had,
without the propagation gap.

* **Per-session, NOT per-turn.** A session can have multiple turns;
  each BEFORE_TASK fire updates the entry. The latest write wins —
  the subscriber's "did this session use any trace?" question is
  answered by "is the slot non-None at session-end time?".
  Refinement: store a count too (``trace_used_count``) for richer
  novelty judgment in Phase 6.
* **Bounded.** A daemon-mode agent that runs forever can't leak
  memory: ``pop_session`` deletes the entry, and a soft cap
  (:data:`_MAX_TRACKED_SESSIONS`) drops the oldest entries first if
  the dict grows past it. Pathological case: ~1KB per entry × 1000
  entries = ~1MB — trivial.
* **Thread-safe.** ``RLock`` so a hook firing in the loop's task
  can co-exist with a subscriber callback running on a different
  task — both can be in the bus's task graph simultaneously.

## Why not store in SessionDB?

A persistent store survives process restarts, but adds schema
migration work and lengthens the unit-test path. For v1.0 the
in-process dict is enough. If daemon-mode plugins start observing
"trace_used unknown after restart" we can graduate to a SessionDB
column then — option (b) from the plan.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass


#: Soft cap on tracked sessions. A daemon that handles 10K sessions
#: would otherwise grow this dict unbounded; ``set_trace_used`` evicts
#: the oldest entries when the cap is reached. Tunable per-test via
#: :func:`reset_for_testing`.
_DEFAULT_MAX_TRACKED_SESSIONS = 1024
_MAX_TRACKED_SESSIONS = _DEFAULT_MAX_TRACKED_SESSIONS


@dataclass(slots=True)
class _SessionEntry:
    """Per-session state the bridge tracks.

    ``trace_used`` is the most recently injected trace_id (or None if
    a turn fired BEFORE_TASK with no inbox match). ``hit_count`` is
    how many turns in this session used a trace — useful for the
    novelty judge to decide "did the agent actually rely on network
    help?".

    ``trace_card`` is the actual ``TraceCard`` that was injected (if
    any) — added in Phase 6 so the novelty judge can compare what
    the agent did to what the trace prescribed without re-querying
    the network. Typed as ``Any`` to avoid a hard plugin_sdk import
    in the bridge module's public surface.
    """

    trace_used: str | None
    hit_count: int
    trace_card: object | None = None  # plugin_sdk.TraceCard or None


# OrderedDict so the LRU eviction is cheap — pop the first key when
# we exceed the cap.
_state: "OrderedDict[str, _SessionEntry]" = OrderedDict()
_lock = threading.RLock()


# ─── public API ───────────────────────────────────────────────────────


def set_trace_used(
    session_id: str,
    trace_id: str | None,
    *,
    trace_card: object | None = None,
) -> None:
    """Record what the BEFORE_TASK hook decided this turn.

    ``trace_id=None`` means "the hook fired but no trace cleared the
    relevance bar" — distinct from "the hook never fired" (which
    leaves the session absent from the dict entirely). The subscriber
    uses that distinction.

    ``trace_card`` is the actual TraceCard that was injected — Phase 6
    novelty judge consumes it. Pass ``None`` when ``trace_id`` is None
    (or omit; the default).

    When the same session_id is updated multiple times within a
    single conversation:

    * The latest ``trace_id`` and ``trace_card`` win (subscribers
      care about the freshest signal).
    * ``hit_count`` increments only on non-None updates so it counts
      actual injections, not "checked, found nothing" noise.
    """
    with _lock:
        existing = _state.get(session_id)
        if existing is None:
            entry = _SessionEntry(
                trace_used=trace_id,
                hit_count=1 if trace_id is not None else 0,
                trace_card=trace_card,
            )
        else:
            entry = _SessionEntry(
                trace_used=trace_id,
                hit_count=existing.hit_count + (1 if trace_id is not None else 0),
                trace_card=trace_card,
            )
        # Touch — move to MRU end of OrderedDict.
        _state.pop(session_id, None)
        _state[session_id] = entry

        # Evict oldest if past the cap. Bounded loop — at most one
        # eviction per write so this stays O(1) amortised.
        while len(_state) > _MAX_TRACKED_SESSIONS:
            _state.popitem(last=False)


def peek_trace_used(session_id: str) -> str | None:
    """Read ``trace_used`` without removing the session entry.

    Returns ``None`` for both "no trace was injected this turn" AND
    "this session was never tracked" — those cases are
    indistinguishable to a peek caller. Use :func:`session_known` for
    the disambiguation.
    """
    with _lock:
        entry = _state.get(session_id)
        return entry.trace_used if entry is not None else None


def session_known(session_id: str) -> bool:
    """Return True iff BEFORE_TASK ever fired for this session.

    Lets the subscriber distinguish "no BEFORE_TASK ever ran"
    (e.g. the plugin was disabled at hook time) from "BEFORE_TASK ran
    but found no trace". The first is "no opinion"; the second is
    "agent explored from scratch, candidate emit material".
    """
    with _lock:
        return session_id in _state


def hit_count(session_id: str) -> int:
    """Return how many turns in this session injected a trace.

    Used by the Phase 6 novelty judge — sessions with
    ``hit_count == 0`` are emit-by-default (rule d binary path);
    ``hit_count > 0`` go through the LLM novelty judge.
    """
    with _lock:
        entry = _state.get(session_id)
        return entry.hit_count if entry is not None else 0


def pop_session(session_id: str) -> _SessionEntry | None:
    """Atomically read + remove the session's entry.

    Called by the post-task subscriber on ``SessionEndEvent``. After
    this returns, subsequent ``peek_trace_used`` / ``session_known``
    for this id return as if the session never existed — releases
    memory the daemon would otherwise hold forever.

    Returns ``None`` if the session was never tracked. Caller should
    treat that the same as "BEFORE_TASK never ran" (most likely the
    plugin was disabled at hook time).
    """
    with _lock:
        return _state.pop(session_id, None)


def tracked_session_count() -> int:
    """Return the number of currently-tracked sessions. Diagnostic
    only — exposed via ``oc traces status`` so operators can spot a
    leak (subscriber crashed, sessions accumulating)."""
    with _lock:
        return len(_state)


# ─── test helpers ─────────────────────────────────────────────────────


def reset_for_testing(*, max_tracked: int | None = None) -> None:
    """Clear all tracked state. Tests that exercise the bridge MUST
    call this in setup/teardown to avoid cross-test leakage from the
    module-level dict.

    Calling with no arguments restores the default cap — important so
    a test that lowered the cap (e.g. eviction test) doesn't poison
    later tests in the same session. Pass ``max_tracked=N`` to set
    a one-off cap for that test.
    """
    global _MAX_TRACKED_SESSIONS  # noqa: PLW0603 — test-only reconfig
    with _lock:
        _state.clear()
        _MAX_TRACKED_SESSIONS = (
            int(max_tracked)
            if max_tracked is not None
            else _DEFAULT_MAX_TRACKED_SESSIONS
        )


__all__ = [
    "hit_count",
    "peek_trace_used",
    "pop_session",
    "reset_for_testing",
    "session_known",
    "set_trace_used",
    "tracked_session_count",
]
