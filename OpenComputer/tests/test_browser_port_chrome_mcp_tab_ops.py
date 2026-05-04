"""Regression test for Bug B — chrome-mcp tab-ops backend wiring.

Before v0.5, opening a tab on the user (existing-session) profile
crashed with:

    Browser internal error: open_tab: no chrome-mcp opener for profile 'user'

The TabOpsBackend wired by ``_dispatcher_bootstrap._build_default_tab_ops_backend``
only set the ``open_tab_via_cdp`` slot, leaving the
``open_tab_via_mcp`` / ``focus_tab_via_mcp`` / ``close_tab_via_mcp``
slots ``None``. Profiles with ``uses_chrome_mcp=True`` then hit
``RuntimeError("no chrome-mcp opener")`` (now ``DriverUnsupportedError``
post-Bug-E) in ``server_context.tab_ops._pick_open_callable``.

These tests verify:
  * The default backend now wires the three MCP-flavoured slots.
  * Calling them dispatches to ``ChromeMcpClient.call_tool`` with the
    upstream ``new_page`` / ``select_page`` / ``close_page`` tool
    names and the argument shapes documented in
    ``docs/refs/openclaw/browser/04-ai-and-snapshot.md``.
  * ``list_tabs`` walks the MCP server's ``list_pages`` response.
"""

from __future__ import annotations

from typing import Any

import pytest

# ─── helpers ─────────────────────────────────────────────────────────


class StubMcpClient:
    """Minimal stand-in for ChromeMcpClient — just records call_tool calls."""

    def __init__(
        self,
        *,
        list_pages_result: dict[str, Any] | None = None,
        new_page_result: dict[str, Any] | None = None,
        select_page_result: dict[str, Any] | None = None,
        close_page_result: dict[str, Any] | None = None,
        close_page_raises: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self._responses = {
            "list_pages": list_pages_result or {"pages": []},
            "new_page": new_page_result
            or {"pages": [{"id": 1, "url": "https://example.com", "selected": True}]},
            "select_page": select_page_result or {"ok": True},
            "close_page": close_page_result or {"ok": True},
        }
        self._close_page_raises = close_page_raises

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.calls.append((name, dict(arguments or {})))
        if name == "close_page" and self._close_page_raises is not None:
            raise self._close_page_raises
        from extensions.browser_control.snapshot.chrome_mcp import (
            ChromeMcpToolResult,
        )

        return ChromeMcpToolResult(
            structured_content=self._responses.get(name),
            content_text=[],
            is_error=False,
            error_message=None,
        )

    async def close(self) -> None:
        self.closed = True


def _build_backend() -> Any:
    from extensions.browser_control._dispatcher_bootstrap import (  # type: ignore[import-not-found]
        _build_default_tab_ops_backend,
    )

    return _build_default_tab_ops_backend()


def _user_profile_runtime(client: Any) -> Any:
    """Construct a ProfileRuntimeState pinned to a chrome-mcp profile."""
    from extensions.browser_control.profiles.config import (  # type: ignore[import-not-found]
        ResolvedBrowserProfile,
    )
    from extensions.browser_control.server_context import (  # type: ignore[import-not-found]
        ProfileRuntimeState,
    )

    profile = ResolvedBrowserProfile(
        name="user",
        cdp_port=0,
        cdp_url="",
        cdp_host="127.0.0.1",
        cdp_is_loopback=True,
        color="#00AA00",
        driver="existing-session",
        attach_only=False,
        user_data_dir=None,
    )
    runtime = ProfileRuntimeState(profile=profile)
    runtime.chrome_mcp_client = client
    return runtime


# ─── tests ───────────────────────────────────────────────────────────


def test_default_backend_wires_chrome_mcp_slots() -> None:
    """All three chrome-mcp callables are populated post-W3.2."""
    backend = _build_backend()
    assert backend.open_tab_via_mcp is not None
    assert backend.focus_tab_via_mcp is not None
    assert backend.close_tab_via_mcp is not None


@pytest.mark.asyncio
async def test_open_tab_via_mcp_calls_new_page() -> None:
    backend = _build_backend()
    client = StubMcpClient(
        new_page_result={
            "pages": [
                {"id": 1, "url": "https://about:blank/", "selected": False},
                {"id": 2, "url": "https://learnx.com/dashboard", "selected": True},
            ]
        }
    )
    runtime = _user_profile_runtime(client)
    tab = await backend.open_tab_via_mcp(runtime, "https://learnx.com/dashboard")

    # Tool name + args wiring.
    names = [name for (name, _args) in client.calls]
    assert "new_page" in names
    new_page_args = next(args for (name, args) in client.calls if name == "new_page")
    assert new_page_args == {"url": "https://learnx.com/dashboard"}

    # Resulting TabInfo: selected page wins, target_id stringified.
    assert tab.target_id == "2"
    assert tab.url == "https://learnx.com/dashboard"


@pytest.mark.asyncio
async def test_open_tab_via_mcp_falls_back_to_list_pages() -> None:
    """When new_page returns a sparse response, we re-read the page list."""
    backend = _build_backend()
    client = StubMcpClient(
        new_page_result={"ok": True},  # no pages, no id
        list_pages_result={
            "pages": [
                {"id": 5, "url": "https://example.com/", "selected": True}
            ]
        },
    )
    runtime = _user_profile_runtime(client)
    tab = await backend.open_tab_via_mcp(runtime, "https://example.com/")
    assert tab.target_id == "5"
    names = [name for (name, _args) in client.calls]
    assert names == ["new_page", "list_pages"]


@pytest.mark.asyncio
async def test_focus_tab_via_mcp_passes_page_id_and_bring_to_front() -> None:
    backend = _build_backend()
    client = StubMcpClient()
    runtime = _user_profile_runtime(client)
    await backend.focus_tab_via_mcp(runtime, "7")
    assert client.calls == [
        ("select_page", {"pageId": 7, "bringToFront": True}),
    ]


@pytest.mark.asyncio
async def test_close_tab_via_mcp_passes_page_id() -> None:
    backend = _build_backend()
    client = StubMcpClient()
    runtime = _user_profile_runtime(client)
    await backend.close_tab_via_mcp(runtime, "3")
    assert client.calls == [("close_page", {"pageId": 3})]


@pytest.mark.asyncio
async def test_close_tab_via_mcp_swallows_already_closed_tool_error() -> None:
    """close_page that errors with ChromeMcpToolError must be idempotent."""
    from extensions.browser_control.snapshot.chrome_mcp import (
        ChromeMcpToolError,
    )

    backend = _build_backend()
    client = StubMcpClient(close_page_raises=ChromeMcpToolError("page already gone"))
    runtime = _user_profile_runtime(client)
    # Should NOT raise.
    await backend.close_tab_via_mcp(runtime, "9")
    assert client.calls == [("close_page", {"pageId": 9})]


@pytest.mark.asyncio
async def test_list_tabs_dispatches_to_mcp_for_chrome_mcp_profile() -> None:
    backend = _build_backend()
    client = StubMcpClient(
        list_pages_result={
            "pages": [
                {"id": 1, "url": "https://a/", "selected": True},
                {"id": 2, "url": "https://b/"},
            ]
        }
    )
    runtime = _user_profile_runtime(client)
    tabs = await backend.list_tabs(runtime)
    assert [t.target_id for t in tabs] == ["1", "2"]
    assert tabs[0].selected is True
    assert tabs[1].selected is False


@pytest.mark.asyncio
async def test_open_tab_routes_through_pick_open_callable() -> None:
    """Verify the dispatch in ``server_context.tab_ops._pick_open_callable``
    actually picks the chrome-mcp slot when ``uses_chrome_mcp=True``.
    """
    from extensions.browser_control.server_context.tab_ops import (  # type: ignore[import-not-found]
        open_tab,
    )

    backend = _build_backend()
    client = StubMcpClient(
        new_page_result={
            "pages": [{"id": 11, "url": "https://x/", "selected": True}]
        }
    )
    runtime = _user_profile_runtime(client)
    tab = await open_tab(runtime, "https://x/", backend=backend)
    assert tab.target_id == "11"
    assert runtime.last_target_id == "11"
