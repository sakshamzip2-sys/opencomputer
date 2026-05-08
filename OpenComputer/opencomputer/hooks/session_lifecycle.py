"""Session-lifecycle hook firing helpers — `SESSION_FINALIZE` + `SESSION_RESET`.

These two events extend the existing :class:`~plugin_sdk.hooks.HookEvent`
enum (2026-05-08, Hermes Doc-2 parity). They fire at points the existing
``SESSION_END`` does *not* cover:

* ``SESSION_FINALIZE`` — once per surface session tear-down. ``SESSION_END``
  fires on every ``run_conversation`` return (i.e. per turn), so a plugin
  that wants to flush a long-lived cache once-per-CLI-exit cannot use it.
  Surfaces fire ``SESSION_FINALIZE`` exactly once when the user closes a
  REPL, the gateway evicts a session, or a wire client disconnects.

* ``SESSION_RESET`` — fires after ``/new`` / ``/reset`` / ``/clear``
  rotates a session id. The CLI's slash handler dispatches ``on_clear``
  which both ends the previous session and allocates a new id; this
  hook lets a plugin transfer in-memory state (recall caches, plan
  drafts, etc.) keyed on the old id over to the new one.

Both helpers are intentionally fire-and-forget — a wedged handler
must never block CLI exit or the next turn (CLAUDE.md §7). The engine
itself catches handler exceptions; we additionally guard against
HookEngine import-time issues (e.g., a plugin import failing) so the
helpers stay safe to call from teardown paths where exceptions are
catastrophic.
"""

from __future__ import annotations

import logging
from typing import Final

from plugin_sdk.hooks import HookContext, HookEvent

logger = logging.getLogger(__name__)


_FINALIZE_REASONS: Final[frozenset[str]] = frozenset({
    "cli_exit", "gateway_evict", "wire_disconnect", "shutdown", "error",
})


def fire_session_finalize(
    *,
    session_id: str,
    reason: str = "cli_exit",
    surface: str = "cli",
) -> None:
    """Fire :data:`HookEvent.SESSION_FINALIZE` for a surface tear-down.

    Parameters
    ----------
    session_id:
        The session being finalized.
    reason:
        One of ``cli_exit``, ``gateway_evict``, ``wire_disconnect``,
        ``shutdown``, ``error``. Free-form strings are tolerated but
        warned — the documented set lets handlers branch on intent.
    surface:
        Origin surface (``cli``, ``gateway``, ``wire``, ``acp``).
    """
    if reason not in _FINALIZE_REASONS:
        logger.warning(
            "fire_session_finalize: unrecognized reason %r — handlers "
            "may not match it. Use one of %s",
            reason, sorted(_FINALIZE_REASONS),
        )
    try:
        from opencomputer.hooks.engine import engine

        engine.fire_and_forget(
            HookContext(
                event=HookEvent.SESSION_FINALIZE,
                session_id=session_id,
                finalize_reason=reason,
                surface_origin=surface,
            )
        )
    except Exception:  # noqa: BLE001 — teardown must never raise
        logger.debug("SESSION_FINALIZE fire failed", exc_info=True)


def fire_session_reset(
    *,
    new_session_id: str,
    previous_session_id: str | None,
    surface: str = "cli",
) -> None:
    """Fire :data:`HookEvent.SESSION_RESET` after ``/new`` / ``/reset``.

    The previous id is exposed so handlers can carry forward state
    (caches, draft plans) keyed on the old id. ``previous_session_id``
    is ``None`` when the slash command fires before any session was
    established (rare — almost always the user is mid-session).
    """
    try:
        from opencomputer.hooks.engine import engine

        engine.fire_and_forget(
            HookContext(
                event=HookEvent.SESSION_RESET,
                session_id=new_session_id,
                previous_session_id=previous_session_id,
                surface_origin=surface,
            )
        )
    except Exception:  # noqa: BLE001
        logger.debug("SESSION_RESET fire failed", exc_info=True)


__all__ = ["fire_session_finalize", "fire_session_reset"]
