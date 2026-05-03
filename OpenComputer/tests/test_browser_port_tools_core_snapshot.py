"""Tests for ``tools_core.snapshot.snapshot_role_via_playwright``."""

from __future__ import annotations

from typing import Any

import pytest
from extensions.browser_control.tools_core.snapshot import (
    AriaModeUnsupportedError,
    snapshot_role_via_playwright,
)

_SAMPLE_ARIA = """
- main "Content"
  - button "OK"
  - button "OK"
  - link "Home"
""".strip()


class _RootLocator:
    """Mock for ``page.locator(":root")`` (or any selector) that returns
    aria_snapshot text on .aria_snapshot()."""

    def __init__(self, text: str) -> None:
        self._text = text

    def locator(self, _sel: str) -> _RootLocator:
        return self

    async def aria_snapshot(self) -> str:
        return self._text


class _MockFrameLocator:
    def __init__(self, text: str) -> None:
        self._text = text

    def locator(self, _sel: str) -> _RootLocator:
        return _RootLocator(self._text)


class _MockPage:
    def __init__(self, aria_text: str) -> None:
        self._text = aria_text
        self.frame_locator_calls: list[str] = []

    def locator(self, _sel: str) -> _RootLocator:
        return _RootLocator(self._text)

    def frame_locator(self, sel: str) -> _MockFrameLocator:
        self.frame_locator_calls.append(sel)
        return _MockFrameLocator(self._text)


@pytest.mark.asyncio
async def test_snapshot_role_returns_refs_and_text() -> None:
    page = _MockPage(_SAMPLE_ARIA)
    result = await snapshot_role_via_playwright(page)
    assert result.snapshot
    # Two button "OK" refs share a name → both retain nth.
    assert any(r.role == "button" and r.name == "OK" for r in result.refs.values())


@pytest.mark.asyncio
async def test_snapshot_truncation() -> None:
    page = _MockPage(_SAMPLE_ARIA)
    result = await snapshot_role_via_playwright(page, max_chars=20)
    assert result.truncated
    assert len(result.snapshot) <= 20


@pytest.mark.asyncio
async def test_snapshot_aria_mode_raises() -> None:
    page = _MockPage(_SAMPLE_ARIA)
    with pytest.raises(AriaModeUnsupportedError):
        await snapshot_role_via_playwright(page, mode="aria")


@pytest.mark.asyncio
async def test_snapshot_with_frame_selector_uses_frame_locator() -> None:
    page = _MockPage(_SAMPLE_ARIA)
    await snapshot_role_via_playwright(page, frame_selector="iframe[name=x]")
    assert page.frame_locator_calls == ["iframe[name=x]"]


@pytest.mark.asyncio
async def test_snapshot_empty_text_returns_empty_marker() -> None:
    page = _MockPage("")
    result = await snapshot_role_via_playwright(page)
    # The role-snapshot builder returns "(empty)" for empty input.
    assert result.snapshot
