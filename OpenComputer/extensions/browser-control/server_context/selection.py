"""Tab selection — last_target_id fallback chain.

Mirrors the deep-dive §8 spec verbatim:

  1. If caller passes ``requested`` explicitly → resolve via
     ``resolve_target_id_from_tabs`` (exact, then case-insensitive prefix).
     Resolved → use it. Ambiguous prefix → raise. Not-found → raise.
  2. If caller passes nothing →
     a. Try ``profile_state.last_target_id``. If non-empty AND resolves
        cleanly → use it.
     b. Else: pick first tab with ``type == "page"``.
     c. Else: pick ``tabs[0]``.
     d. Else (no tabs): caller is responsible for opening ``about:blank``
        upstream.
  3. After picking: stamp ``profile_state.last_target_id``.

``close_tab`` deliberately does NOT stamp `last_target_id` — that's a
deliberate safety so the next call's fallback still has a valid hint.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from .state import ProfileRuntimeState, TabInfo


class TabNotFoundError(LookupError):
    """No tab matched the requested target id (or no tabs exist at all)."""


class AmbiguousTargetIdError(ValueError):
    """A truncated target id matched multiple tabs."""


@dataclass(frozen=True, slots=True)
class _Resolution:
    kind: str  # "ok" | "ambiguous" | "not_found"
    target_id: str | None = None
    candidates: tuple[str, ...] = ()


_RESOLVE_OK: Final[str] = "ok"
_RESOLVE_AMBIGUOUS: Final[str] = "ambiguous"
_RESOLVE_NOT_FOUND: Final[str] = "not_found"


def resolve_target_id_from_tabs(
    requested: str | None,
    tabs: Sequence[TabInfo],
) -> _Resolution:
    """Match a requested (possibly truncated) target id against ``tabs``."""
    if not requested:
        return _Resolution(kind=_RESOLVE_NOT_FOUND)
    cleaned = requested.strip()
    if not cleaned:
        return _Resolution(kind=_RESOLVE_NOT_FOUND)

    # Exact match wins outright (case-sensitive — Chrome target ids are
    # opaque hex strings; case-folding would mask collisions).
    for tab in tabs:
        if tab.target_id == cleaned:
            return _Resolution(kind=_RESOLVE_OK, target_id=tab.target_id)

    # Case-insensitive prefix.
    lc = cleaned.lower()
    matches = [tab.target_id for tab in tabs if tab.target_id.lower().startswith(lc)]
    if len(matches) == 1:
        return _Resolution(kind=_RESOLVE_OK, target_id=matches[0])
    if len(matches) > 1:
        return _Resolution(kind=_RESOLVE_AMBIGUOUS, candidates=tuple(matches))
    return _Resolution(kind=_RESOLVE_NOT_FOUND)


def select_target_id(
    runtime: ProfileRuntimeState,
    *,
    tabs: Sequence[TabInfo],
    requested: str | None = None,
    update_last: bool = True,
) -> str:
    """Pick the right target id — explicit, sticky, or first-page fallback.

    Raises:
      AmbiguousTargetIdError — caller-supplied ``requested`` matches >1 tabs.
      TabNotFoundError — caller-supplied ``requested`` matches none, or
        ``tabs`` is empty (caller should open ``about:blank`` then retry).
    """
    if requested is not None and requested.strip():
        resolution = resolve_target_id_from_tabs(requested, tabs)
        if resolution.kind == _RESOLVE_OK:
            chosen = resolution.target_id
            assert chosen is not None
            if update_last:
                runtime.last_target_id = chosen
            return chosen
        if resolution.kind == _RESOLVE_AMBIGUOUS:
            raise AmbiguousTargetIdError(
                f"target id {requested!r} matches {len(resolution.candidates)} tabs: "
                + ", ".join(resolution.candidates)
            )
        raise TabNotFoundError(f"no tab matches target id {requested!r}")

    if not tabs:
        raise TabNotFoundError("no tabs available; open about:blank first")

    # Try the sticky last_target_id.
    if runtime.last_target_id:
        sticky = resolve_target_id_from_tabs(runtime.last_target_id, tabs)
        if sticky.kind == _RESOLVE_OK and sticky.target_id is not None:
            if update_last:
                runtime.last_target_id = sticky.target_id
            return sticky.target_id
        # If ambiguous: deliberate fall-through. last_target_id is a hint;
        # we don't surface its ambiguity to the caller.

    # First page-typed tab.
    for tab in tabs:
        if tab.type == "page":
            if update_last:
                runtime.last_target_id = tab.target_id
            return tab.target_id

    # Any tab.
    chosen = tabs[0].target_id
    if update_last:
        runtime.last_target_id = chosen
    return chosen
