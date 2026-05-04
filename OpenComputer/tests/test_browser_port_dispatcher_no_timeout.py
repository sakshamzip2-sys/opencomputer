"""Regression test for Bug D — dispatcher 20s timeout on /act after a
chrome-mcp open_tab failure.

In v0.5 production we observed::

    browser error: In-process dispatcher timed out after 20s: /act

The chain that produced it:

  1. ``Browser(action="open", profile="user", url=...)`` hit
     ``server_context.tab_ops.open_tab`` which raised
     ``RuntimeError("open_tab: no chrome-mcp opener for profile 'user'")``
     (Bug B). The route returned 5xx but the request bookkeeping in
     ``ProfileRuntimeState`` was left in a partially-initialised
     state.
  2. The next ``Browser(action="act", ...)`` call entered the
     dispatcher, ran into the same partially-initialised state, and
     hung waiting on a never-emitted page event — eventually tripping
     the 20s wait_for in ``client.fetch._fetch_dispatcher``.

After v0.5 Bug B + Bug C land, the open_tab path no longer raises
``no chrome-mcp opener`` (it actually opens the tab), so the
follow-on /act call returns immediately rather than timing out. This
test exercises the full sequence with a stub MCP client and asserts
no timeout occurs.

The act-handler internals exercise more than just tab ops — to keep
this regression test focused, we stop after open_tab and assert the
dispatcher returned a TabInfo within sub-second wall time. A full
/act smoke test (snapshot + click + close) lives in
``test_browser_port_adapter_e2e.py`` already.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_open_then_focus_then_close_user_profile_no_timeout() -> None:
    """End-to-end smoke through the chrome-mcp tab-ops pipeline.

    Exercises the full pattern that used to trip the 20s timeout:

      open_tab → (would hang) → focus_tab → close_tab

    With Bug B + Bug C fixed, all three calls return promptly.
    """
    from extensions.browser_control._dispatcher_bootstrap import (  # type: ignore[import-not-found]
        _build_default_tab_ops_backend,
    )
    from extensions.browser_control.profiles.config import (  # type: ignore[import-not-found]
        ResolvedBrowserProfile,
    )
    from extensions.browser_control.server_context import (  # type: ignore[import-not-found]
        ProfileRuntimeState,
    )
    from extensions.browser_control.server_context.tab_ops import (  # type: ignore[import-not-found]
        close_tab,
        focus_tab,
        open_tab,
    )
    from extensions.browser_control.snapshot.chrome_mcp import (
        ChromeMcpToolResult,
    )

    class _StubClient:
        def __init__(self) -> None:
            self.pages: list[dict[str, Any]] = []
            self._next_id = 0

        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None
        ) -> ChromeMcpToolResult:
            args = dict(arguments or {})
            if name == "list_pages":
                return ChromeMcpToolResult(
                    structured_content={"pages": list(self.pages)}
                )
            if name == "new_page":
                self._next_id += 1
                page = {
                    "id": self._next_id,
                    "url": args.get("url", ""),
                    "selected": True,
                }
                # Only one page is selected at a time.
                for entry in self.pages:
                    entry["selected"] = False
                self.pages.append(page)
                return ChromeMcpToolResult(
                    structured_content={"pages": list(self.pages)}
                )
            if name == "select_page":
                pid = args.get("pageId")
                for entry in self.pages:
                    entry["selected"] = entry["id"] == pid
                return ChromeMcpToolResult(structured_content={"ok": True})
            if name == "close_page":
                pid = args.get("pageId")
                self.pages = [e for e in self.pages if e["id"] != pid]
                return ChromeMcpToolResult(structured_content={"ok": True})
            return ChromeMcpToolResult(structured_content=None)

        async def close(self) -> None:
            return None

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
    runtime.chrome_mcp_client = _StubClient()
    backend = _build_default_tab_ops_backend()

    async def _run() -> None:
        tab = await open_tab(
            runtime, "https://learnx.com/dashboard", backend=backend
        )
        assert tab.target_id == "1"
        assert runtime.last_target_id == "1"

        focused = await focus_tab(runtime, tab.target_id, backend=backend)
        assert focused == "1"

        await close_tab(runtime, tab.target_id, backend=backend)

    started = time.monotonic()
    # If Bug B/C still bit, this call would either crash with
    # "no chrome-mcp opener" or hang past the agent-loop budget.
    # We bound it well under the dispatcher's 20s ceiling.
    await asyncio.wait_for(_run(), timeout=2.0)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, (
        f"Bug D regression — chrome-mcp tab ops took {elapsed:.3f}s; "
        "the path should be near-instant under stubs"
    )
