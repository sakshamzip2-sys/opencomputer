"""Unit tests for ``tools_core.refs.ref_locator`` decision tree."""

from __future__ import annotations

from typing import Any

import pytest
from extensions.browser_control.session.playwright_session import (
    RoleRef,
    RoleRefsCacheEntry,
)
from extensions.browser_control.tools_core.refs import (
    UnknownRefError,
    ref_locator,
)


class _Locator:
    def __init__(self, label: str) -> None:
        self.label = label

    def nth(self, n: int) -> _Locator:
        return _Locator(f"{self.label}.nth({n})")


class _MockPage:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_by_role(self, role: str, **kw: Any) -> _Locator:
        name = kw.get("name")
        exact = kw.get("exact")
        self.calls.append(f"get_by_role({role!r}, name={name!r}, exact={exact!r})")
        return _Locator(f"role={role}/name={name}")

    def locator(self, sel: str) -> _Locator:
        self.calls.append(f"locator({sel!r})")
        return _Locator(f"sel={sel}")

    def frame_locator(self, sel: str) -> _MockPage:
        self.calls.append(f"frame_locator({sel!r})")
        return self


def test_role_mode_named() -> None:
    page = _MockPage()
    entry = RoleRefsCacheEntry(refs={"e1": RoleRef("button", "Submit")}, mode="role")
    loc = ref_locator(page, "e1", cache_entry=entry)
    assert loc.label.startswith("role=button")
    assert "name='Submit'" in page.calls[0]
    assert "exact=True" in page.calls[0]


def test_role_mode_with_nth() -> None:
    page = _MockPage()
    entry = RoleRefsCacheEntry(
        refs={"e1": RoleRef("button", "Submit", nth=2)}, mode="role"
    )
    loc = ref_locator(page, "e1", cache_entry=entry)
    assert ".nth(2)" in loc.label


def test_role_mode_unknown_ref_raises() -> None:
    page = _MockPage()
    entry = RoleRefsCacheEntry(refs={}, mode="role")
    with pytest.raises(UnknownRefError):
        ref_locator(page, "e99", cache_entry=entry)


def test_aria_mode_uses_aria_ref_selector() -> None:
    page = _MockPage()
    entry = RoleRefsCacheEntry(refs={}, mode="aria")
    loc = ref_locator(page, "e7", cache_entry=entry)
    assert loc.label == "sel=aria-ref=e7"


def test_no_cache_falls_back_to_aria_ref() -> None:
    page = _MockPage()
    loc = ref_locator(page, "e3", cache_entry=None)
    assert loc.label == "sel=aria-ref=e3"


def test_strips_at_prefix() -> None:
    page = _MockPage()
    entry = RoleRefsCacheEntry(refs={"e1": RoleRef("link")}, mode="role")
    ref_locator(page, "@e1", cache_entry=entry)
    assert "'link'" in page.calls[0]


def test_strips_ref_prefix() -> None:
    page = _MockPage()
    entry = RoleRefsCacheEntry(refs={"e1": RoleRef("link")}, mode="role")
    ref_locator(page, "ref=e1", cache_entry=entry)
    assert "'link'" in page.calls[0]


def test_frame_scoped_uses_frame_locator() -> None:
    page = _MockPage()
    entry = RoleRefsCacheEntry(
        refs={"e1": RoleRef("button", "OK")}, mode="role", frame_selector="iframe[name=x]"
    )
    ref_locator(page, "e1", cache_entry=entry)
    assert any("frame_locator" in c for c in page.calls)


def test_empty_ref_raises_value_error() -> None:
    page = _MockPage()
    with pytest.raises(ValueError):
        ref_locator(page, "", cache_entry=None)
