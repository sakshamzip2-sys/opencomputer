"""Phase 10e: WebFetch + WebSearch tools.

Tests are network-free — every external request is mocked. We assert:

- input validation (missing/invalid url, missing query)
- HTML stripping (script/style/nav tags removed; visible text preserved)
- truncation respects max_chars
- HTTP errors (4xx/5xx, timeout) become friendly ToolResult is_error=True
- DDG result parsing (titles, urls unwrapped from /l/?uddg=, snippets)
- registry registration (Web tools land in `registry.names()`)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from opencomputer.tools.web_fetch import WebFetchTool, _html_to_text
from opencomputer.tools.web_search import WebSearchTool, _parse_results, _unwrap_ddg_redirect
from plugin_sdk.core import ToolCall


def _call(tool_name: str, args: dict) -> ToolCall:
    return ToolCall(id="tc-1", name=tool_name, arguments=args)


# ─── _html_to_text helper ──────────────────────────────────────────────


def test_html_to_text_strips_script_and_style() -> None:
    html = """<html>
    <body>
      <h1>Title</h1>
      <p>Visible paragraph.</p>
      <script>alert('hi');</script>
      <style>.x { color: red; }</style>
      <noscript>fallback</noscript>
    </body></html>"""
    text = _html_to_text(html)
    assert "Title" in text
    assert "Visible paragraph." in text
    assert "alert" not in text
    assert "color: red" not in text
    assert "fallback" not in text


def test_html_to_text_collapses_blank_lines() -> None:
    html = "<p>A</p>\n\n\n\n<p>B</p>"
    out = _html_to_text(html)
    # No more than one blank line between paragraphs after collapse
    assert out == "A\nB"


# ─── WebFetch ──────────────────────────────────────────────────────────


async def test_webfetch_rejects_missing_url() -> None:
    tool = WebFetchTool()
    res = await tool.execute(_call("WebFetch", {}))
    assert res.is_error
    assert "url is required" in res.content


async def test_webfetch_rejects_non_http_scheme() -> None:
    tool = WebFetchTool()
    res = await tool.execute(_call("WebFetch", {"url": "ftp://example.com"}))
    assert res.is_error
    assert "must start with http" in res.content


async def test_webfetch_returns_stripped_text(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = WebFetchTool()
    fake_resp = MagicMock(
        status_code=200,
        text="<html><body><h1>Hello</h1><script>x</script></body></html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url):
            return fake_resp

    monkeypatch.setattr("opencomputer.tools.web_fetch.httpx.AsyncClient", _FakeClient)
    res = await tool.execute(_call("WebFetch", {"url": "https://example.com"}))
    assert not res.is_error
    assert "Hello" in res.content
    assert "https://example.com" in res.content
    assert "<script>" not in res.content


async def test_webfetch_truncates_long_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = WebFetchTool()
    long_text = "X " * 5000
    fake_resp = MagicMock(
        status_code=200,
        text=f"<html><body><p>{long_text}</p></body></html>",
        headers={"content-type": "text/html"},
    )

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url):
            return fake_resp

    monkeypatch.setattr("opencomputer.tools.web_fetch.httpx.AsyncClient", _FakeClient)
    res = await tool.execute(
        _call("WebFetch", {"url": "https://example.com", "max_chars": 200})
    )
    assert not res.is_error
    assert "[truncated" in res.content


async def test_webfetch_handles_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = WebFetchTool()
    fake_resp = MagicMock(status_code=404, text="not found", headers={})

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url):
            return fake_resp

    monkeypatch.setattr("opencomputer.tools.web_fetch.httpx.AsyncClient", _FakeClient)
    res = await tool.execute(_call("WebFetch", {"url": "https://example.com/missing"}))
    assert res.is_error
    assert "404" in res.content


async def test_webfetch_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = WebFetchTool()

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url):
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr("opencomputer.tools.web_fetch.httpx.AsyncClient", _FakeClient)
    res = await tool.execute(_call("WebFetch", {"url": "https://slow.example.com"}))
    assert res.is_error
    assert "timed out" in res.content


# ─── WebSearch ─────────────────────────────────────────────────────────


def test_unwrap_ddg_redirect_extracts_real_url() -> None:
    wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Farticle&rut=abc"
    assert _unwrap_ddg_redirect(wrapped) == "https://example.com/article"


def test_unwrap_ddg_redirect_passes_through_plain_url() -> None:
    plain = "https://example.com/page"
    assert _unwrap_ddg_redirect(plain) == plain


def test_parse_results_extracts_title_url_snippet() -> None:
    html = """
    <div class="result">
      <h2 class="result__title">
        <a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Example A</a>
      </h2>
      <a class="result__snippet" href="x">Snippet for A</a>
    </div>
    <div class="result">
      <h2 class="result__title">
        <a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb">Example B</a>
      </h2>
      <a class="result__snippet" href="x">Snippet for B</a>
    </div>
    """
    results = _parse_results(html, max_results=10)
    assert len(results) == 2
    assert results[0]["title"] == "Example A"
    assert results[0]["url"] == "https://example.com/a"
    assert results[0]["snippet"] == "Snippet for A"


def test_parse_results_respects_max_results() -> None:
    html = "".join(
        f"""
        <div class="result">
          <h2 class="result__title"><a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fe.com%2F{i}">T{i}</a></h2>
          <a class="result__snippet" href="x">S{i}</a>
        </div>
        """
        for i in range(20)
    )
    results = _parse_results(html, max_results=5)
    assert len(results) == 5


async def test_websearch_rejects_empty_query() -> None:
    tool = WebSearchTool()
    res = await tool.execute(_call("WebSearch", {"query": ""}))
    assert res.is_error
    assert "query is required" in res.content


async def test_websearch_returns_markdown_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = WebSearchTool()
    fake_html = """
    <div class="result">
      <h2 class="result__title">
        <a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Doc A</a>
      </h2>
      <a class="result__snippet" href="x">Snippet A</a>
    </div>
    """
    fake_resp = MagicMock(status_code=200, text=fake_html, headers={})

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, data):
            return fake_resp

    monkeypatch.setattr("opencomputer.tools.web_search.httpx.AsyncClient", _FakeClient)
    res = await tool.execute(_call("WebSearch", {"query": "anything"}))
    assert not res.is_error
    assert "Doc A" in res.content
    assert "https://example.com/a" in res.content
    assert "Snippet A" in res.content


async def test_websearch_no_results_returns_friendly_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = WebSearchTool()
    fake_resp = MagicMock(status_code=200, text="<html></html>", headers={})

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, data):
            return fake_resp

    monkeypatch.setattr("opencomputer.tools.web_search.httpx.AsyncClient", _FakeClient)
    res = await tool.execute(_call("WebSearch", {"query": "obscure"}))
    assert not res.is_error
    assert "No results" in res.content


# ─── Registry registration ─────────────────────────────────────────────


def test_register_builtin_tools_includes_web_tools() -> None:
    """`opencomputer chat` calls _register_builtin_tools which must register
    WebFetch + WebSearch alongside the others."""
    from opencomputer.cli import _register_builtin_tools
    from opencomputer.tools.registry import registry

    _register_builtin_tools()
    names = set(registry.names())
    assert "WebFetch" in names
    assert "WebSearch" in names
