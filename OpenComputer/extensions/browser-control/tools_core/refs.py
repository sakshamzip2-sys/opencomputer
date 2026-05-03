"""Ref → Locator resolution.

Two modes (deep dive §3):

  - **role**: ``state.roleRefs[ref] = {role, name?, nth?}``. Resolved
    via ``page.get_by_role(role, name=name, exact=True)`` chained with
    ``.nth(idx)`` if duplicate.
  - **aria**: opaque aria-ref id. Resolved via
    ``page.locator("aria-ref=eN")`` (Playwright's internal selector,
    stable across page rerenders).

A ref id that doesn't exist in the cache → ``UnknownRefError`` —
the agent must call snapshot again. There's no auto-resnapshot. By design.

Frame-scoped refs: if the snapshot was taken inside a frame
(``frame_selector`` set on the cache entry), the locator scope first
walks ``page.frame_locator(frame_selector)``.

A ref of the form ``e\\d+`` that's missing from the cache falls back to
``page.locator("aria-ref=eN")`` — Playwright's aria-ref index may still
resolve it even if our local cache lost the entry (deep dive §3 last
branch of the decision tree).
"""

from __future__ import annotations

import re
from typing import Any

from ..session.playwright_session import RoleRef, RoleRefsCacheEntry

_E_REF_RE = re.compile(r"^e\d+$")


class UnknownRefError(LookupError):
    """The supplied ref id has no entry in the role-refs cache."""

    def __init__(self, ref: str) -> None:
        super().__init__(
            f"Unknown ref {ref!r}. Run a new snapshot to refresh refs."
        )
        self.ref = ref


def _normalize(ref: str) -> str:
    """Strip leading ``@`` and ``ref=`` prefixes."""
    s = ref.strip()
    if s.startswith("ref="):
        s = s[4:]
    if s.startswith("@"):
        s = s[1:]
    return s


def ref_locator(
    page: Any,
    ref: str,
    *,
    cache_entry: RoleRefsCacheEntry | None,
) -> Any:
    """Resolve ``ref`` against ``page`` using the cached entry.

    ``cache_entry`` is the result of ``PlaywrightSession.get_role_refs(target_id)``.
    None means "no snapshot yet" — falls back to ``aria-ref=`` selector
    for ``e\\d+`` refs (Playwright owns its own ref index in aria-mode).

    Raises:
      UnknownRefError — ``ref`` is ``e\\d+`` shape but not in cache AND
        cache_entry is non-None (we have a snapshot but this id isn't ours).
    """
    normalized = _normalize(ref)
    if not normalized:
        raise ValueError("ref is empty")

    is_e_ref = bool(_E_REF_RE.match(normalized))

    # Build the scope: frame-locator if cache_entry has frame_selector.
    scope: Any = page
    if cache_entry is not None and cache_entry.frame_selector:
        scope = page.frame_locator(cache_entry.frame_selector)

    # Cached entry path.
    if cache_entry is not None and is_e_ref:
        if cache_entry.mode == "aria":
            return scope.locator(f"aria-ref={normalized}")
        info = cache_entry.refs.get(normalized)
        if info is None:
            raise UnknownRefError(normalized)
        if info.name:
            loc = scope.get_by_role(info.role, name=info.name, exact=True)
        else:
            loc = scope.get_by_role(info.role)
        if info.nth is not None and info.nth > 0:
            loc = loc.nth(info.nth)
        return loc

    # No cache OR not e\d+: fall back to aria-ref selector.
    return scope.locator(f"aria-ref={normalized}")


def store_refs_into_session(
    session: Any,
    *,
    target_id: str,
    refs: dict[str, RoleRef],
    frame_selector: str | None = None,
    mode: str | None = None,
) -> None:
    """Convenience: store snapshot results into the session cache."""
    session.store_role_refs(
        target_id=target_id, refs=refs, frame_selector=frame_selector, mode=mode  # type: ignore[arg-type]
    )
