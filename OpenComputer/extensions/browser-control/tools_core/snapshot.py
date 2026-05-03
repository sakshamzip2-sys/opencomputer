"""Snapshot orchestration — calls Playwright, runs through the role
classifier from W1b, stores refs in the session cache.

Two modes:

  - ``role`` (default, v0.1): public ``locator.aria_snapshot()`` →
    ``build_role_snapshot_from_aria_snapshot`` → store with mode="role".
  - ``aria``: ``page._snapshot_for_ai`` (the underscore API). NOT
    shipped for v0.1 — playwright-python doesn't reliably expose it.
    Calling with mode="aria" raises ``AriaModeUnsupportedError``.

The result is a ``SnapshotResult`` (re-exported from W1b). Callers that
need to take subsequent actions against the refs should pass the same
``target_id`` into ``ref_locator`` so the cache lookup works.

Truncation: if ``max_chars`` is set and the rendered text exceeds it,
the text is sliced (the ref dictionary is left intact — Playwright/our
build keep refs stable across truncation).
"""

from __future__ import annotations

from typing import Any, Literal

from ..snapshot.role_snapshot import (
    SnapshotResult,
    build_role_snapshot_from_aria_snapshot,
)


class AriaModeUnsupportedError(NotImplementedError):
    """``mode="aria"`` requires the underscore API, not shipped in v0.1."""


async def snapshot_role_via_playwright(
    page: Any,
    *,
    selector: str | None = None,
    frame_selector: str | None = None,
    mode: Literal["role", "aria"] = "role",
    max_chars: int | None = None,
    interactive_only: bool = False,
    compact: bool = False,
) -> SnapshotResult:
    """Build a role-snapshot for ``page`` (or a sub-scope).

    Returns ``SnapshotResult`` with ``snapshot`` (text), ``refs`` (dict),
    and ``stats``.

    The session-side caching of refs is the caller's responsibility —
    typically ``session.store_role_refs(target_id=..., refs=result.refs,
    frame_selector=frame_selector, mode=mode)`` immediately after.
    """
    if mode == "aria":
        raise AriaModeUnsupportedError(
            "Snapshot mode 'aria' requires page._snapshot_for_ai which is not "
            "exposed by playwright-python. Use mode='role' for v0.1."
        )

    scope: Any = page
    if frame_selector:
        scope = page.frame_locator(frame_selector)
    if selector:
        scope = scope.locator(selector)
    else:
        # Default to :root so we get the whole document tree.
        scope = scope.locator(":root")

    aria_text = await scope.aria_snapshot()
    if not isinstance(aria_text, str):
        aria_text = ""

    result = build_role_snapshot_from_aria_snapshot(
        aria_text, interactive=interactive_only, compact=compact
    )

    if max_chars is not None and max_chars > 0 and len(result.snapshot) > max_chars:
        result.snapshot = result.snapshot[:max_chars]
        result.truncated = True

    return result
