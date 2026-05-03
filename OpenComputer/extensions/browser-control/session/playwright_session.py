"""PlaywrightSession — Browser + Context + active Page wrapper.

Owns:
  - the role-ref cache (``role_refs_by_target``) keyed by ``(cdp_url, target_id)``,
    which survives Playwright Page swaps
  - ``last_target_id`` — the sticky "last interacted with" tab pointer

Exposes:
  - ``get_page_for_target(target_id) -> Page``
  - ``list_pages() -> list[Page]``
  - ``store_role_refs / restore_role_refs / get_role_refs`` for snapshot integration
  - ``mark_blocked(page, target_id)`` for the navigation guard's quarantine

Does NOT own connect/disconnect — see ``cdp.py``.

The role-ref cache is implemented over ``OrderedDict`` with explicit
``move_to_end`` on read so reads bump LRU order. OpenClaw's TS map was
documented as LRU but actually FIFO-by-insertion (BLUEPRINT §7); we fix
that bug here.
"""

from __future__ import annotations

import logging
import weakref
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, Literal

from .helpers import normalize_cdp_url, target_key
from .target_id import page_target_id

_log = logging.getLogger("opencomputer.browser_control.session.pw_session")

MAX_ROLE_REFS_CACHE: Final[int] = 50


@dataclass(slots=True)
class RoleRef:
    role: str
    name: str | None = None
    nth: int | None = None


@dataclass(slots=True)
class RoleRefsCacheEntry:
    refs: dict[str, RoleRef]
    frame_selector: str | None = None
    mode: Literal["role", "aria"] | None = None


class BlockedBrowserTargetError(Exception):
    """Page is unavailable because the SSRF guard blocked its navigation."""


class PlaywrightSession:
    """Holds the live Browser + the per-target caches.

    One ``PlaywrightSession`` per ``(profile_name, cdp_url)`` pair. The
    server_context module owns the lifecycle and resets it when the
    underlying browser disconnects.
    """

    def __init__(self, *, browser: Any, cdp_url: str) -> None:
        self.browser = browser
        self.cdp_url: str = normalize_cdp_url(cdp_url)
        self.last_target_id: str | None = None

        # role-ref cache: keyed by f"{cdp_url}::{target_id}"
        self._role_refs: OrderedDict[str, RoleRefsCacheEntry] = OrderedDict()

        # blocked-target maps. WeakSet so we don't pin Page objects.
        self._blocked_target_ids: set[str] = set()
        self._blocked_page_refs: weakref.WeakSet[Any] = weakref.WeakSet()

    # ─── role-ref cache ──────────────────────────────────────────────

    def store_role_refs(
        self,
        *,
        target_id: str,
        refs: dict[str, RoleRef],
        frame_selector: str | None = None,
        mode: Literal["role", "aria"] | None = None,
    ) -> None:
        if not target_id:
            return
        key = target_key(self.cdp_url, target_id)
        if key in self._role_refs:
            self._role_refs.pop(key)
        self._role_refs[key] = RoleRefsCacheEntry(
            refs=refs, frame_selector=frame_selector, mode=mode
        )
        while len(self._role_refs) > MAX_ROLE_REFS_CACHE:
            self._role_refs.popitem(last=False)

    def get_role_refs(self, target_id: str) -> RoleRefsCacheEntry | None:
        if not target_id:
            return None
        key = target_key(self.cdp_url, target_id)
        entry = self._role_refs.get(key)
        if entry is None:
            return None
        # Bump LRU order on read.
        self._role_refs.move_to_end(key)
        return entry

    def evict_role_refs(self, target_id: str) -> None:
        if not target_id:
            return
        self._role_refs.pop(target_key(self.cdp_url, target_id), None)

    def role_refs_size(self) -> int:
        return len(self._role_refs)

    # ─── blocked target / page tracking ──────────────────────────────

    def mark_target_blocked(self, target_id: str | None) -> None:
        if target_id:
            self._blocked_target_ids.add(target_id)

    def is_target_blocked(self, target_id: str | None) -> bool:
        return bool(target_id) and target_id in self._blocked_target_ids

    def clear_blocked_target(self, target_id: str | None) -> None:
        if target_id:
            self._blocked_target_ids.discard(target_id)

    def has_any_blocked_targets(self) -> bool:
        return bool(self._blocked_target_ids)

    def mark_page_blocked(self, page: Any) -> None:
        try:
            self._blocked_page_refs.add(page)
        except TypeError:
            # Some test mocks aren't weakly referenceable.
            self._blocked_page_refs = weakref.WeakSet()  # reset rather than pin
            self._log_unweakref()

    def is_page_blocked(self, page: Any) -> bool:
        try:
            return page in self._blocked_page_refs
        except TypeError:
            return False

    def clear_blocked_page(self, page: Any) -> None:
        try:
            self._blocked_page_refs.discard(page)
        except TypeError:
            pass

    def _log_unweakref(self) -> None:
        _log.debug("PlaywrightSession: page object not weakly referenceable; reset block set")

    # ─── pages ───────────────────────────────────────────────────────

    def list_pages(self) -> list[Any]:
        out: list[Any] = []
        contexts = getattr(self.browser, "contexts", None)
        if contexts is None:
            return out
        for ctx in contexts:
            ctx_pages = getattr(ctx, "pages", None) or []
            for p in ctx_pages:
                out.append(p)
        return out

    async def get_page_for_target(self, target_id: str) -> Any:
        """Find the Page whose Chrome target ID matches.

        Tolerates Playwright stale-Page identity by checking each Page's
        target ID via CDP/HTTP fallback. Falls back to "the only page"
        in the extension-context single-page case.
        """
        if self.is_target_blocked(target_id):
            raise BlockedBrowserTargetError(
                f"target {target_id!r} is quarantined after a navigation block"
            )

        pages = self.list_pages()
        accessible: list[Any] = []
        any_failed = False
        for page in pages:
            if self.is_page_blocked(page):
                continue
            try:
                tid = await page_target_id(page, cdp_url=self.cdp_url)
            except Exception:  # noqa: BLE001
                any_failed = True
                continue
            if tid is None:
                any_failed = True
                continue
            if tid == target_id:
                if self.is_target_blocked(tid):
                    raise BlockedBrowserTargetError(
                        f"target {tid!r} is quarantined after a navigation block"
                    )
                return page
            accessible.append(page)

        # Single-page extension-context fallback: if no probe succeeded
        # and there is exactly one non-blocked page, return it.
        if any_failed and len(accessible) == 0:
            unblocked = [p for p in pages if not self.is_page_blocked(p)]
            if len(unblocked) == 1:
                return unblocked[0]

        raise LookupError(f"no page found with target id {target_id!r}")

    async def first_accessible_page(self) -> Any | None:
        for page in self.list_pages():
            if self.is_page_blocked(page):
                continue
            tid = await page_target_id(page, cdp_url=self.cdp_url)
            if tid is None:
                # Fail-closed if any blocks exist for this URL.
                if self.has_any_blocked_targets():
                    continue
                return page
            if self.is_target_blocked(tid):
                continue
            return page
        return None

    # ─── target-id enumeration helpers ───────────────────────────────

    async def known_target_ids(self) -> Iterable[str]:
        out: list[str] = []
        for p in self.list_pages():
            try:
                tid = await page_target_id(p, cdp_url=self.cdp_url)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(tid, str) and tid:
                out.append(tid)
        return out
