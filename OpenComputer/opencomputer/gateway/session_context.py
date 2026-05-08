"""
Session-scoped context variables for the OpenComputer gateway.

Replaces the previous ``os.environ``-based session state
(``OPENCOMPUTER_SESSION_PLATFORM``, ``OPENCOMPUTER_SESSION_CHAT_ID``, etc.)
with Python's ``contextvars.ContextVar``.

**Why this matters**

The gateway processes messages concurrently via ``asyncio``. When two
messages arrive at the same time the old code did:

    os.environ["OPENCOMPUTER_SESSION_THREAD_ID"] = str(context.source.thread_id)

Because ``os.environ`` is *process-global*, Message A's value was silently
overwritten by Message B before Message A's agent finished running.
Background-task notifications and tool calls therefore routed to the wrong
thread.

``contextvars.ContextVar`` values are *task-local*: each ``asyncio`` task
(and any ``run_in_executor`` thread it spawns) gets its own copy, so
concurrent messages never interfere.

**Backward compatibility**

The public helper ``get_session_env(name, default="")`` mirrors the old
``os.getenv("OPENCOMPUTER_SESSION_*", ...)`` calls. Existing tool code
only needs to replace the import + call site:

    # before
    import os
    platform = os.getenv("OPENCOMPUTER_SESSION_PLATFORM", "")

    # after
    from opencomputer.gateway.session_context import get_session_env
    platform = get_session_env("OPENCOMPUTER_SESSION_PLATFORM", "")
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Any

# Sentinel to distinguish "never set in this context" from "explicitly set to empty".
# When a contextvar holds _UNSET, we fall back to os.environ (CLI/cron compat).
# When it holds "" (after clear_session_vars resets it), we return "" — no fallback.
_UNSET: Any = object()

# ---------------------------------------------------------------------------
# Per-task session variables
# ---------------------------------------------------------------------------

_SESSION_PLATFORM: ContextVar = ContextVar("OPENCOMPUTER_SESSION_PLATFORM", default=_UNSET)
_SESSION_CHAT_ID: ContextVar = ContextVar("OPENCOMPUTER_SESSION_CHAT_ID", default=_UNSET)
_SESSION_CHAT_NAME: ContextVar = ContextVar("OPENCOMPUTER_SESSION_CHAT_NAME", default=_UNSET)
_SESSION_THREAD_ID: ContextVar = ContextVar("OPENCOMPUTER_SESSION_THREAD_ID", default=_UNSET)
_SESSION_USER_ID: ContextVar = ContextVar("OPENCOMPUTER_SESSION_USER_ID", default=_UNSET)
_SESSION_USER_NAME: ContextVar = ContextVar("OPENCOMPUTER_SESSION_USER_NAME", default=_UNSET)
_SESSION_KEY: ContextVar = ContextVar("OPENCOMPUTER_SESSION_KEY", default=_UNSET)

# Cron auto-delivery vars — set per-job in run_job() so concurrent jobs
# don't clobber each other's delivery targets.
_CRON_AUTO_DELIVER_PLATFORM: ContextVar = ContextVar(
    "OPENCOMPUTER_CRON_AUTO_DELIVER_PLATFORM", default=_UNSET
)
_CRON_AUTO_DELIVER_CHAT_ID: ContextVar = ContextVar(
    "OPENCOMPUTER_CRON_AUTO_DELIVER_CHAT_ID", default=_UNSET
)
_CRON_AUTO_DELIVER_THREAD_ID: ContextVar = ContextVar(
    "OPENCOMPUTER_CRON_AUTO_DELIVER_THREAD_ID", default=_UNSET
)

_VAR_MAP: dict[str, ContextVar] = {
    "OPENCOMPUTER_SESSION_PLATFORM": _SESSION_PLATFORM,
    "OPENCOMPUTER_SESSION_CHAT_ID": _SESSION_CHAT_ID,
    "OPENCOMPUTER_SESSION_CHAT_NAME": _SESSION_CHAT_NAME,
    "OPENCOMPUTER_SESSION_THREAD_ID": _SESSION_THREAD_ID,
    "OPENCOMPUTER_SESSION_USER_ID": _SESSION_USER_ID,
    "OPENCOMPUTER_SESSION_USER_NAME": _SESSION_USER_NAME,
    "OPENCOMPUTER_SESSION_KEY": _SESSION_KEY,
    "OPENCOMPUTER_CRON_AUTO_DELIVER_PLATFORM": _CRON_AUTO_DELIVER_PLATFORM,
    "OPENCOMPUTER_CRON_AUTO_DELIVER_CHAT_ID": _CRON_AUTO_DELIVER_CHAT_ID,
    "OPENCOMPUTER_CRON_AUTO_DELIVER_THREAD_ID": _CRON_AUTO_DELIVER_THREAD_ID,
}


def set_session_vars(
    *,
    platform: str = "",
    chat_id: str = "",
    chat_name: str = "",
    thread_id: str = "",
    user_id: str = "",
    user_name: str = "",
    key: str = "",
) -> list:
    """Set per-task session context variables and return reset tokens.

    Each asyncio task gets its own copy — concurrent calls from different
    tasks do not interfere.

    Returns a list of ``Token`` objects (one per variable). Callers may
    pass them to ``ContextVar.reset`` if they want to restore the prior
    value, but the standard pattern is to call :func:`clear_session_vars`
    in a ``finally`` block, which sets each var to ``""`` and prevents
    fallback to potentially stale ``os.environ`` values.
    """
    return [
        _SESSION_PLATFORM.set(platform),
        _SESSION_CHAT_ID.set(chat_id),
        _SESSION_CHAT_NAME.set(chat_name),
        _SESSION_THREAD_ID.set(thread_id),
        _SESSION_USER_ID.set(user_id),
        _SESSION_USER_NAME.set(user_name),
        _SESSION_KEY.set(key),
    ]


def clear_session_vars() -> None:
    """Mark session context variables as explicitly cleared.

    Sets every session var to ``""`` so :func:`get_session_env` returns the
    empty string instead of falling back to (potentially stale)
    ``os.environ`` values. We intentionally use ``var.set("")`` rather than
    ``var.reset(token)`` so the "explicitly cleared" state is
    distinguishable from "never set" (which holds the ``_UNSET`` sentinel).
    """
    for var in (
        _SESSION_PLATFORM,
        _SESSION_CHAT_ID,
        _SESSION_CHAT_NAME,
        _SESSION_THREAD_ID,
        _SESSION_USER_ID,
        _SESSION_USER_NAME,
        _SESSION_KEY,
    ):
        var.set("")


def set_cron_delivery(
    *,
    platform: str = "",
    chat_id: str = "",
    thread_id: str = "",
) -> list:
    """Set per-task cron auto-delivery context variables.

    Used by the cron scheduler so concurrent jobs route their auto-delivered
    output to the right destination without clobbering each other.
    """
    return [
        _CRON_AUTO_DELIVER_PLATFORM.set(platform),
        _CRON_AUTO_DELIVER_CHAT_ID.set(chat_id),
        _CRON_AUTO_DELIVER_THREAD_ID.set(thread_id),
    ]


def clear_cron_delivery() -> None:
    """Mark cron auto-delivery context variables as explicitly cleared."""
    for var in (
        _CRON_AUTO_DELIVER_PLATFORM,
        _CRON_AUTO_DELIVER_CHAT_ID,
        _CRON_AUTO_DELIVER_THREAD_ID,
    ):
        var.set("")


def get_session_env(name: str, default: str = "") -> str:
    """Read a session context variable by its ``OPENCOMPUTER_*`` name.

    Drop-in replacement for ``os.getenv("OPENCOMPUTER_SESSION_*", default)``.

    Resolution order:

    1. Context variable (set by the gateway for concurrency-safe access).
       If the variable was explicitly set (even to ``""``) via
       :func:`set_session_vars`, :func:`clear_session_vars`,
       :func:`set_cron_delivery`, or :func:`clear_cron_delivery`, that
       value is returned — **no fallback to os.environ**.
    2. ``os.environ`` (only when the context variable was never set in
       this context — i.e. CLI, cron scheduler, and test processes that
       don't use ``set_session_vars`` at all).
    3. *default*.
    """
    var = _VAR_MAP.get(name)
    if var is not None:
        value = var.get()
        if value is not _UNSET:
            return value
    return os.getenv(name, default)
