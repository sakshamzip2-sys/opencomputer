"""Per-turn trace correlation id — public SDK primitive.

A single ``contextvars.ContextVar`` carries one OC-generated trace UUID
across every async-await boundary that happens inside one
``AgentLoop.run_conversation`` invocation. Providers, hooks, channel
adapters, memory plugins, and any future observability subscriber read
the same id off this contextvar so all per-turn observations (LLM
calls, tool spans, Honcho calls, compaction events) can be correlated
by a single key.

Why this lives in ``plugin_sdk``
-------------------------------
Per the SDK boundary contract (see ``plugin_sdk/CLAUDE.md``):

* Type contracts and general primitives live here so plugins can import
  them without breaking the no-``opencomputer.*`` rule.
* This module has zero ``opencomputer`` dependencies — it's a pure
  contextvars + uuid wrapper. Belongs in the public surface so memory
  plugins (memory-honcho), evolution subscribers (skill-evolution), and
  any third-party plugin can correlate observations without coupling to
  internals.

Design rationale
----------------
*   **One trace per ``run_conversation``** — not per-LLM-call. A single
    user turn fans out to many provider invocations (tool-call loops);
    they all belong under one trace.
*   **ContextVar, not threading.local** — asyncio's task-local context
    is propagated automatically through ``await`` chains. A
    :class:`threading.local` does not survive an ``asyncio.create_task``
    hop.
*   **No coupling to langfuse / honcho / any backend** — the trace id
    is a plain UUID; whether or not a langfuse / langsmith / OTel trace
    ever materialises for it is an observability-backend concern. The
    id remains useful as a JSONL correlation key when no backend is
    configured.

Usage
-----
::

    from plugin_sdk.trace import (
        new_trace_id, set_trace_id, get_trace_id, reset_trace_id,
    )

    async def run_one_turn():
        trace_id = new_trace_id()
        token = set_trace_id(trace_id)
        try:
            ...  # provider calls inside here see get_trace_id() == trace_id
        finally:
            reset_trace_id(token)

The ``trace_scope()`` context manager wraps the try/finally pattern.
"""

from __future__ import annotations

import contextlib
import contextvars
import uuid
from collections.abc import Iterator

#: Module-global contextvar. Default ``None`` means "no active trace" —
#: every read site must tolerate a ``None`` return. Tests reset between
#: cases by ``set_trace_id(None)`` then ``reset_trace_id``.
_active_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "oc_trace_id", default=None
)


def new_trace_id() -> str:
    """Generate a fresh trace id.

    Returns a UUID4 string. UUIDs are used so the id is collision-safe
    across processes (gateway + CLI in the same profile) without a
    shared sequencer.
    """
    return str(uuid.uuid4())


def set_trace_id(trace_id: str | None) -> contextvars.Token:
    """Push ``trace_id`` onto the contextvar; return the reset token.

    The token is the caller's responsibility — pass it to
    :func:`reset_trace_id` to undo this push. Mismatched
    set/reset pairs leak state across awaits.

    Accepts ``None`` so callers can explicitly clear the trace during a
    short subroutine (e.g. an out-of-band logging job that should not
    inherit the surrounding trace).
    """
    return _active_trace_id.set(trace_id)


def get_trace_id() -> str | None:
    """Return the currently-active trace id, or ``None`` if unset."""
    return _active_trace_id.get()


def reset_trace_id(token: contextvars.Token) -> None:
    """Pop the contextvar back to whatever was active before ``set_trace_id``.

    Safe to call with a stale or already-consumed token — the
    contextvars runtime raises ``RuntimeError`` ("Token has already
    been used once") on a re-used token and ``ValueError`` on a
    cross-context token. Both are swallowed so a misbalanced
    try/finally never crashes the agent loop. On any error we
    explicitly set the var to ``None`` so the value can't leak into
    the next turn either.
    """
    try:
        _active_trace_id.reset(token)
    except (RuntimeError, ValueError):
        # Either: token already consumed (RuntimeError), or created
        # in a different context (ValueError, e.g. crossed an
        # event-loop boundary). Best-effort clear.
        _active_trace_id.set(None)


@contextlib.contextmanager
def trace_scope(trace_id: str | None = None) -> Iterator[str]:
    """Context manager wrapping the set / reset pair.

    Generates a fresh id when ``trace_id`` is None. Yields the active id
    so callers can stash it on event metadata without a second
    ``get_trace_id()`` call.

    Usage::

        with trace_scope() as tid:
            # all observability inside this block sees get_trace_id() == tid
            ...
    """
    tid = trace_id or new_trace_id()
    token = set_trace_id(tid)
    try:
        yield tid
    finally:
        reset_trace_id(token)


__all__ = [
    "get_trace_id",
    "new_trace_id",
    "reset_trace_id",
    "set_trace_id",
    "trace_scope",
]
