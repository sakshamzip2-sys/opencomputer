"""Tests for the 9 new Browser actions added in Wave 4 (adapter promotion)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


class _FakeActions:
    """In-memory ``BrowserActions`` stub that records every call.

    Matches the surface ``_tool.py`` actually invokes: each method
    returns whatever was queued for it (or a default).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._returns: dict[str, object] = {}

    def queue(self, method: str, value):
        self._returns[method] = value

    async def browser_status(self, **kw):
        self.calls.append(("status", kw))
        return self._returns.get("status", {"ok": True})

    async def browser_requests(self, **kw):
        self.calls.append(("requests", kw))
        return self._returns.get(
            "requests",
            {"requests": [{"url": "https://x", "method": "GET", "status": 200}]},
        )

    async def browser_response_body(self, **kw):
        self.calls.append(("response_body", kw))
        return self._returns.get("response_body", {"body": "..."})

    async def browser_act(self, request, **kw):
        self.calls.append(("act", {**request, **kw}))
        kind = request.get("kind", "")
        if kind == "evaluate":
            return self._returns.get("evaluate", {"result": []})
        return self._returns.get(f"act_{kind}", {})

    async def browser_navigate(self, **kw):
        self.calls.append(("navigate", kw))
        return self._returns.get("navigate", {"targetId": "T1"})


def _new_browser(actions):
    from extensions.browser_control._tool import Browser

    return Browser(actions=actions)


def _call(action: str, **kw):
    from plugin_sdk.core import ToolCall

    return ToolCall(id="x", name="Browser", arguments={"action": action, **kw})


# ─── 1) network_start ─────────────────────────────────────────────


def test_network_start_clears_buffer():
    fa = _FakeActions()
    browser = _new_browser(fa)
    res = asyncio.run(browser.execute(_call("network_start")))
    assert not res.is_error
    # Verify it called browser_requests with clear=True
    assert any(
        m == "requests" and kw.get("clear") is True for m, kw in fa.calls
    )


# ─── 2) network_list ─────────────────────────────────────────────


def test_network_list_returns_requests():
    fa = _FakeActions()
    fa.queue("requests", {"requests": [{"url": "https://api.x.com/1"}]})
    browser = _new_browser(fa)
    res = asyncio.run(browser.execute(_call("network_list")))
    assert not res.is_error
    assert "api.x.com" in res.content


# ─── 3) network_detail ────────────────────────────────────────────


def test_network_detail_requires_request_id():
    fa = _FakeActions()
    browser = _new_browser(fa)
    res = asyncio.run(browser.execute(_call("network_detail")))
    assert res.is_error
    assert "requestId" in res.content


def test_network_detail_returns_body():
    fa = _FakeActions()
    fa.queue("response_body", {"body": "hello"})
    browser = _new_browser(fa)
    res = asyncio.run(browser.execute(_call("network_detail", requestId="abc")))
    assert not res.is_error
    assert "hello" in res.content


# ─── 4) resource_timing ──────────────────────────────────────────


def test_resource_timing_evaluates_perf_buffer():
    fa = _FakeActions()
    fa.queue("evaluate", {"result": [{"name": "https://x.com/api/1"}]})
    browser = _new_browser(fa)
    res = asyncio.run(browser.execute(_call("resource_timing")))
    assert not res.is_error
    # the JS expression went through act/evaluate
    assert any(c[0] == "act" and c[1].get("kind") == "evaluate" for c in fa.calls)


# ─── 5) analyze ───────────────────────────────────────────────────


def test_analyze_returns_pattern_and_endpoints():
    fa = _FakeActions()
    fa.queue(
        "evaluate",
        {"result": [{"name": "https://target.example/api/v1/data"}]},
    )
    browser = _new_browser(fa)
    res = asyncio.run(
        browser.execute(_call("analyze", url="https://target.example"))
    )
    assert not res.is_error
    # Either the analysis succeeded with pattern + candidate_endpoints,
    # or we got an error string. The tool wraps non-failure paths into
    # a JSON dict with these keys.
    import json

    payload = json.loads(res.content)
    assert "pattern" in payload
    assert "candidate_endpoints" in payload


# ─── 6) adapter_new ──────────────────────────────────────────────


def test_adapter_new_writes_stub(tmp_path: Path):
    fa = _FakeActions()
    browser = _new_browser(fa)
    res = asyncio.run(
        browser.execute(
            _call(
                "adapter_new",
                site="testsite",
                name="probe",
                adapters_root=str(tmp_path),
                strategy="public",
            )
        )
    )
    assert not res.is_error, res.content
    expected = tmp_path / "testsite" / "probe.py"
    assert expected.is_file()
    text = expected.read_text()
    assert "@adapter" in text
    assert 'site="testsite"' in text
    assert 'name="probe"' in text


def test_adapter_new_refuses_overwrite(tmp_path: Path):
    fa = _FakeActions()
    browser = _new_browser(fa)
    asyncio.run(
        browser.execute(
            _call(
                "adapter_new",
                site="s",
                name="n",
                adapters_root=str(tmp_path),
            )
        )
    )
    res2 = asyncio.run(
        browser.execute(
            _call(
                "adapter_new",
                site="s",
                name="n",
                adapters_root=str(tmp_path),
            )
        )
    )
    assert res2.is_error
    assert "already exists" in res2.content


# ─── 7) adapter_validate ─────────────────────────────────────────


def test_adapter_validate_well_formed(tmp_path: Path):
    fa = _FakeActions()
    browser = _new_browser(fa)
    f = tmp_path / "good.py"
    f.write_text(
        '''
from extensions.adapter_runner import adapter, Strategy

@adapter(site="okv", name="cmd", description="d", domain="e.com",
         strategy=Strategy.PUBLIC, columns=["a"])
async def run(args, ctx):
    return [{"a": 1}]
'''
    )
    res = asyncio.run(browser.execute(_call("adapter_validate", path=str(f))))
    assert not res.is_error, res.content
    import json

    payload = json.loads(res.content)
    assert payload["ok"] is True
    assert payload["tool_name"] == "OkvCmd"


def test_adapter_validate_with_errors(tmp_path: Path):
    fa = _FakeActions()
    browser = _new_browser(fa)
    f = tmp_path / "bad.py"
    f.write_text("def def def\n")
    res = asyncio.run(browser.execute(_call("adapter_validate", path=str(f))))
    import json

    payload = json.loads(res.content)
    assert payload["ok"] is False
    assert payload["errors"]
