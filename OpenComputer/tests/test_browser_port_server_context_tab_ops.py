"""Unit tests for `server_context/tab_ops.py`."""

from __future__ import annotations

import pytest
from extensions.browser_control.profiles import (
    resolve_browser_config,
    resolve_profile,
)
from extensions.browser_control.server_context import (
    ProfileRuntimeState,
    TabInfo,
    close_tab,
    focus_tab,
    open_tab,
)
from extensions.browser_control.server_context.tab_ops import TabOpsBackend

# ─── fixtures ─────────────────────────────────────────────────────────


def _runtime(profile_name: str = "opencomputer") -> ProfileRuntimeState:
    cfg = resolve_browser_config({})
    p = resolve_profile(cfg, profile_name)
    assert p is not None
    return ProfileRuntimeState(profile=p)


def _runtime_user() -> ProfileRuntimeState:
    return _runtime(profile_name="user")


def _runtime_remote() -> ProfileRuntimeState:
    cfg = resolve_browser_config(
        {
            "profiles": {
                "remote": {
                    "cdp_url": "http://10.0.0.5:18800",
                    "driver": "managed",
                },
            }
        }
    )
    p = resolve_profile(cfg, "remote")
    assert p is not None and p.cdp_is_loopback is False
    return ProfileRuntimeState(profile=p)


# ─── open_tab ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_tab_via_cdp_path_for_local_managed() -> None:
    runtime = _runtime()
    opens: list[tuple[str, str]] = []

    async def list_tabs(_r) -> list[TabInfo]:
        return []

    async def open_via_cdp(_r, url: str) -> TabInfo:
        opens.append((runtime.profile.name, url))
        return TabInfo("T-NEW", url)

    backend = TabOpsBackend(list_tabs=list_tabs, open_tab_via_cdp=open_via_cdp)
    tab = await open_tab(runtime, "https://example.com/", backend=backend)
    assert tab.target_id == "T-NEW"
    assert opens == [("opencomputer", "https://example.com/")]
    assert runtime.last_target_id == "T-NEW"


@pytest.mark.asyncio
async def test_open_tab_via_chrome_mcp_for_existing_session() -> None:
    runtime = _runtime_user()
    opens: list[str] = []

    async def list_tabs(_r) -> list[TabInfo]:
        return []

    async def open_via_mcp(_r, url: str) -> TabInfo:
        opens.append(url)
        return TabInfo("MCP-T1", url)

    backend = TabOpsBackend(list_tabs=list_tabs, open_tab_via_mcp=open_via_mcp)
    tab = await open_tab(runtime, "https://x.example/", backend=backend)
    assert tab.target_id == "MCP-T1"
    assert opens == ["https://x.example/"]


@pytest.mark.asyncio
async def test_open_tab_via_persistent_playwright_for_remote() -> None:
    runtime = _runtime_remote()
    opens: list[str] = []

    async def list_tabs(_r) -> list[TabInfo]:
        return []

    async def open_via_pw(_r, url: str) -> TabInfo:
        opens.append(url)
        return TabInfo("REMOTE-T1", url)

    backend = TabOpsBackend(list_tabs=list_tabs, open_tab_via_playwright=open_via_pw)
    tab = await open_tab(runtime, "https://y.example/", backend=backend)
    assert tab.target_id == "REMOTE-T1"


@pytest.mark.asyncio
async def test_open_tab_missing_callable_raises() -> None:
    runtime = _runtime()  # local-managed needs CDP opener

    async def list_tabs(_r) -> list[TabInfo]:
        return []

    backend = TabOpsBackend(list_tabs=list_tabs)
    with pytest.raises(RuntimeError, match="no CDP opener"):
        await open_tab(runtime, "https://x/", backend=backend)


@pytest.mark.asyncio
async def test_open_tab_empty_url_raises() -> None:
    runtime = _runtime()

    async def list_tabs(_r) -> list[TabInfo]:
        return []

    backend = TabOpsBackend(list_tabs=list_tabs)
    with pytest.raises(ValueError):
        await open_tab(runtime, "", backend=backend)


# ─── focus_tab ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_focus_tab_with_explicit_target() -> None:
    runtime = _runtime()
    focuses: list[str] = []

    async def list_tabs(_r) -> list[TabInfo]:
        return [TabInfo("T1", "https://a/"), TabInfo("T2", "https://b/")]

    async def focus_via_cdp(_r, target_id: str) -> None:
        focuses.append(target_id)

    backend = TabOpsBackend(list_tabs=list_tabs, focus_tab_via_cdp=focus_via_cdp)
    chosen = await focus_tab(runtime, "T2", backend=backend)
    assert chosen == "T2"
    assert focuses == ["T2"]
    assert runtime.last_target_id == "T2"


@pytest.mark.asyncio
async def test_focus_tab_uses_sticky_when_no_target_passed() -> None:
    runtime = _runtime()
    runtime.last_target_id = "T2"

    async def list_tabs(_r) -> list[TabInfo]:
        return [TabInfo("T1", "https://a/"), TabInfo("T2", "https://b/")]

    focuses: list[str] = []

    async def focus_via_cdp(_r, target_id: str) -> None:
        focuses.append(target_id)

    backend = TabOpsBackend(list_tabs=list_tabs, focus_tab_via_cdp=focus_via_cdp)
    chosen = await focus_tab(runtime, None, backend=backend)
    assert chosen == "T2"
    assert focuses == ["T2"]


# ─── close_tab ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_tab_does_not_update_last_target_id() -> None:
    """close_tab is the deliberate exception to the lastTargetId-stamp rule."""
    runtime = _runtime()
    runtime.last_target_id = "T1"
    closes: list[str] = []

    async def list_tabs(_r) -> list[TabInfo]:
        return [TabInfo("T1", "https://a/"), TabInfo("T2", "https://b/")]

    async def close_via_cdp(_r, target_id: str) -> None:
        closes.append(target_id)

    backend = TabOpsBackend(list_tabs=list_tabs, close_tab_via_cdp=close_via_cdp)
    await close_tab(runtime, "T1", backend=backend)
    # last_target_id is INTENTIONALLY left as "T1" even though it now
    # points to a closed tab. See deep-dive §8 closing paragraph.
    assert runtime.last_target_id == "T1"
    assert closes == ["T1"]


@pytest.mark.asyncio
async def test_close_tab_empty_target_id_raises() -> None:
    runtime = _runtime()

    async def list_tabs(_r) -> list[TabInfo]:
        return []

    backend = TabOpsBackend(list_tabs=list_tabs)
    with pytest.raises(ValueError):
        await close_tab(runtime, "", backend=backend)
