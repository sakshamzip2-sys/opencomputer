"""Per-session tab registry — auto-close agent-opened tabs at session end.

Memory-only. Outer key = session_key (case-folded), inner key =
``(target_id, base_url, profile)`` tuple. Tabs are tracked by
:func:`track_session_browser_tab` whenever the agent opens one and
removed by :func:`untrack_session_browser_tab` on explicit close. Cleanup
on session end calls :func:`close_tracked_browser_tabs_for_sessions` —
which deletes from the map FIRST, then issues the network closes.

The "delete first, close second" ordering matters: a concurrent cleanup
call for the same session sees an empty list and is idempotent. Errors
that look like "tab already closed" are swallowed silently.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import time
from typing import Any

_log = logging.getLogger("opencomputer.browser_control.client.tab_registry")

#: Substrings (case-insensitive) that mean "the tab is already gone".
#: Match both the server-side ``BrowserTabNotFoundError("tab not found")``
#: shape and the raw Chrome CDP error strings.
_IGNORABLE_CLOSE_PHRASES: tuple[str, ...] = (
    "tab not found",
    "target closed",
    "target not found",
    "no such target",
)


@dataclass(frozen=True, slots=True)
class TrackedTab:
    """One tab the agent has opened during a session."""

    session_key: str
    target_id: str
    base_url: str
    profile: str
    tracked_at: float


# session_key (folded) -> {(target_id, base_url, profile): TrackedTab}
_REGISTRY: dict[str, dict[tuple[str, str, str], TrackedTab]] = {}


def _fold_session_key(s: str) -> str:
    return s.strip().casefold()


def _fold_profile(s: str | None) -> str:
    return (s or "").strip().casefold()


def track_session_browser_tab(
    *,
    session_key: str,
    target_id: str,
    base_url: str | None = None,
    profile: str | None = None,
) -> str | None:
    """Record that ``session_key`` has an open tab.

    Returns the composite tracked-id (for symmetry with the TS source's
    ``trackedId`` return), or ``None`` when the inputs are blank (no-op).
    """
    sk = _fold_session_key(session_key)
    tid = (target_id or "").strip()
    if not sk or not tid:
        return None
    burl = (base_url or "").strip()
    prof = _fold_profile(profile)
    key = (tid, burl, prof)
    inner = _REGISTRY.setdefault(sk, {})
    inner[key] = TrackedTab(
        session_key=sk,
        target_id=tid,
        base_url=burl,
        profile=prof,
        tracked_at=time(),
    )
    return f"{tid}\x00{burl}\x00{prof}"


def untrack_session_browser_tab(
    *,
    session_key: str,
    target_id: str,
    base_url: str | None = None,
    profile: str | None = None,
) -> bool:
    """Remove the entry. Returns True if something was removed."""
    sk = _fold_session_key(session_key)
    tid = (target_id or "").strip()
    if not sk or not tid:
        return False
    inner = _REGISTRY.get(sk)
    if inner is None:
        return False
    key = (tid, (base_url or "").strip(), _fold_profile(profile))
    removed = inner.pop(key, None) is not None
    if not inner:
        _REGISTRY.pop(sk, None)
    return removed


CloseCallable = Callable[..., Awaitable[Any]]


async def close_tracked_browser_tabs_for_sessions(
    session_keys: list[str],
    *,
    close_tab: CloseCallable | None = None,
    on_warn: Callable[[str], None] | None = None,
) -> int:
    """Close every tracked tab for the given sessions.

    Returns the count of tabs successfully closed. The ``close_tab``
    callable defaults to :func:`BrowserActions.browser_close_tab`. Errors
    matching :data:`_IGNORABLE_CLOSE_PHRASES` are swallowed; other errors
    invoke ``on_warn`` (default: logger.warning).

    The registry is mutated FIRST, so a concurrent cleanup for the same
    session sees an empty list (idempotent).
    """
    folded = {_fold_session_key(k) for k in session_keys if (k or "").strip()}
    if not folded:
        return 0

    tabs: list[TrackedTab] = []
    for sk in folded:
        inner = _REGISTRY.pop(sk, None)
        if not inner:
            continue
        tabs.extend(inner.values())

    if not tabs:
        return 0

    if close_tab is None:
        # Lazy-import to avoid a circular dependency at module import.
        from .actions import BrowserActions

        actions = BrowserActions()

        async def _default_close(*, target_id: str, base_url: str, profile: str) -> Any:
            return await actions.browser_close_tab(
                target_id=target_id,
                base_url=base_url or None,
                profile=profile or None,
            )

        close_tab = _default_close

    if on_warn is None:
        def _default_warn(msg: str) -> None:
            _log.warning("%s", msg)
        on_warn = _default_warn

    closed = 0
    for tab in tabs:
        try:
            await close_tab(
                target_id=tab.target_id,
                base_url=tab.base_url,
                profile=tab.profile,
            )
            closed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _is_ignorable_close_error(exc):
                continue
            on_warn(f"failed to close tab {tab.target_id}: {exc}")
    return closed


def _is_ignorable_close_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _IGNORABLE_CLOSE_PHRASES)


# ─── test helpers ────────────────────────────────────────────────────


def reset_tracked_session_browser_tabs_for_tests() -> None:
    """Clear the registry — call between tests."""
    _REGISTRY.clear()


def count_tracked_session_browser_tabs_for_tests() -> int:
    return sum(len(inner) for inner in _REGISTRY.values())


__all__ = [
    "TrackedTab",
    "close_tracked_browser_tabs_for_sessions",
    "count_tracked_session_browser_tabs_for_tests",
    "reset_tracked_session_browser_tabs_for_tests",
    "track_session_browser_tab",
    "untrack_session_browser_tab",
]
