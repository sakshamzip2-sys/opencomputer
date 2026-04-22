"""Phase 12d.2 — multi-provider WebSearch backend chain.

Tests are network-free: every external request is mocked via httpx
context managers patched on the per-backend module. We verify:

- The 5 backends register correctly + get_backend raises on unknown id
- SearchHit + SearchBackend ABC contract
- Each backend's HTTP shape (URL, headers, payload) matches its provider
- Each keyed backend rejects missing key with a friendly SearchBackendError
- Each keyed backend translates 401 / 429 / generic 4xx into clean errors
- WebSearchTool reads config.tools.web_search.provider as default
- WebSearchTool honours per-call provider override
- WebSearchTool returns friendly is_error=True for SearchBackendError /
  timeout / HTTPError instead of raising
- ToolsConfig + WebSearchConfig defaults exist
- Refactored WebSearchTool keeps the markdown render contract
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from opencomputer.tools.search_backends import (
    BACKEND_IDS,
    BACKENDS,
    BraveBackend,
    DuckDuckGoBackend,
    ExaBackend,
    FirecrawlBackend,
    SearchBackend,
    SearchBackendError,
    SearchHit,
    TavilyBackend,
    get_backend,
)
from opencomputer.tools.web_search import WebSearchTool
from plugin_sdk.core import ToolCall


def _call(args: dict[str, Any]) -> ToolCall:
    return ToolCall(id="tc-1", name="WebSearch", arguments=args)


def _fake_async_client_factory(captured: dict[str, Any], response: MagicMock):
    """Return a class that mimics httpx.AsyncClient for one request and stashes
    its constructor + per-call args into `captured` for assertions."""

    class _FakeClient:
        def __init__(self, *a, **kw):
            captured["client_init"] = kw

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, params=None, headers=None):
            captured["method"] = "GET"
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return response

        async def post(self, url, json=None, headers=None, data=None):
            captured["method"] = "POST"
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            captured["data"] = data
            return response

    return _FakeClient


# ─── registry / ABC contract ────────────────────────────────────────────


def test_backends_registry_has_all_five() -> None:
    assert set(BACKEND_IDS) == {"ddg", "brave", "tavily", "exa", "firecrawl"}
    assert BACKENDS["ddg"] is DuckDuckGoBackend
    assert BACKENDS["brave"] is BraveBackend
    assert BACKENDS["tavily"] is TavilyBackend
    assert BACKENDS["exa"] is ExaBackend
    assert BACKENDS["firecrawl"] is FirecrawlBackend


def test_get_backend_unknown_id_raises_with_helpful_message() -> None:
    with pytest.raises(KeyError) as ei:
        get_backend("yahoo")
    msg = str(ei.value)
    assert "yahoo" in msg
    # Available list is part of the error so users know the valid set
    assert "ddg" in msg


def test_search_hit_dataclass_minimal() -> None:
    h = SearchHit(title="t", url="https://example.com")
    assert h.title == "t"
    assert h.url == "https://example.com"
    assert h.snippet == ""  # default


def test_needs_api_key_reflects_env_var_field() -> None:
    assert DuckDuckGoBackend().needs_api_key() is False
    assert BraveBackend().needs_api_key() is True
    assert TavilyBackend().needs_api_key() is True


# ─── DDG backend ───────────────────────────────────────────────────────


async def test_ddg_parses_html_and_unwraps_redirects(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    fake_html = (
        '<div class="result">'
        '<h2 class="result__title">'
        '<a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">A</a>'
        "</h2>"
        '<a class="result__snippet" href="x">snippet A</a>'
        "</div>"
    )
    response = MagicMock(status_code=200, text=fake_html)
    monkeypatch.setattr(
        "opencomputer.tools.search_backends.ddg.httpx.AsyncClient",
        _fake_async_client_factory(captured, response),
    )
    hits = await DuckDuckGoBackend().search(query="x", max_results=5, timeout_s=10)
    assert len(hits) == 1
    assert hits[0].title == "A"
    # uddg redirect was unwrapped to the real destination
    assert hits[0].url == "https://example.com/a"
    assert hits[0].snippet == "snippet A"
    assert captured["url"].endswith("/html/")


async def test_ddg_4xx_raises_search_backend_error(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    response = MagicMock(status_code=503, text="")
    monkeypatch.setattr(
        "opencomputer.tools.search_backends.ddg.httpx.AsyncClient",
        _fake_async_client_factory(captured, response),
    )
    with pytest.raises(SearchBackendError) as ei:
        await DuckDuckGoBackend().search(query="x", max_results=5, timeout_s=10)
    assert "503" in str(ei.value)


# ─── keyed backends — happy path + missing key + 401 + 429 ─────────────


@pytest.mark.parametrize(
    "backend_cls,env_var,fake_payload,expected_url_part",
    [
        (
            BraveBackend,
            "BRAVE_API_KEY",
            {"web": {"results": [{"title": "B", "url": "https://b.com", "description": "bd"}]}},
            "api.search.brave.com",
        ),
        (
            TavilyBackend,
            "TAVILY_API_KEY",
            {"results": [{"title": "T", "url": "https://t.com", "content": "td"}]},
            "api.tavily.com",
        ),
        (
            ExaBackend,
            "EXA_API_KEY",
            {"results": [{"title": "E", "url": "https://e.com", "text": "ed"}]},
            "api.exa.ai",
        ),
        (
            FirecrawlBackend,
            "FIRECRAWL_API_KEY",
            {"data": [{"title": "F", "url": "https://f.com", "description": "fd"}]},
            "firecrawl.dev",
        ),
    ],
)
async def test_keyed_backend_happy_path(
    backend_cls, env_var, fake_payload, expected_url_part, monkeypatch
) -> None:
    captured: dict[str, Any] = {}
    response = MagicMock(status_code=200)
    response.json = MagicMock(return_value=fake_payload)
    monkeypatch.setenv(env_var, "test-key")
    monkeypatch.setattr(
        f"opencomputer.tools.search_backends.{backend_cls.__module__.rsplit('.', 1)[1]}.httpx.AsyncClient",
        _fake_async_client_factory(captured, response),
    )

    hits = await backend_cls().search(query="q", max_results=3, timeout_s=10)
    assert len(hits) == 1
    assert hits[0].url.startswith("https://")
    assert expected_url_part in captured["url"]


@pytest.mark.parametrize(
    "backend_cls,env_var",
    [
        (BraveBackend, "BRAVE_API_KEY"),
        (TavilyBackend, "TAVILY_API_KEY"),
        (ExaBackend, "EXA_API_KEY"),
        (FirecrawlBackend, "FIRECRAWL_API_KEY"),
    ],
)
async def test_keyed_backend_missing_env_var_friendly_error(
    backend_cls, env_var, monkeypatch
) -> None:
    monkeypatch.delenv(env_var, raising=False)
    with pytest.raises(SearchBackendError) as ei:
        await backend_cls().search(query="q", max_results=3, timeout_s=10)
    assert env_var in str(ei.value)
    # signup_url surfaced so the user knows how to get a key
    assert "http" in str(ei.value).lower()


@pytest.mark.parametrize(
    "backend_cls,env_var,module_name",
    [
        (BraveBackend, "BRAVE_API_KEY", "brave"),
        (TavilyBackend, "TAVILY_API_KEY", "tavily"),
        (ExaBackend, "EXA_API_KEY", "exa"),
        (FirecrawlBackend, "FIRECRAWL_API_KEY", "firecrawl"),
    ],
)
async def test_keyed_backend_401_translates_to_unauthorized_error(
    backend_cls, env_var, module_name, monkeypatch
) -> None:
    captured: dict[str, Any] = {}
    response = MagicMock(status_code=401)
    response.json = MagicMock(return_value={})
    monkeypatch.setenv(env_var, "bad-key")
    monkeypatch.setattr(
        f"opencomputer.tools.search_backends.{module_name}.httpx.AsyncClient",
        _fake_async_client_factory(captured, response),
    )
    with pytest.raises(SearchBackendError) as ei:
        await backend_cls().search(query="q", max_results=3, timeout_s=10)
    assert "401" in str(ei.value) or "unauthor" in str(ei.value).lower()


@pytest.mark.parametrize(
    "backend_cls,env_var,module_name",
    [
        (BraveBackend, "BRAVE_API_KEY", "brave"),
        (TavilyBackend, "TAVILY_API_KEY", "tavily"),
        (ExaBackend, "EXA_API_KEY", "exa"),
        (FirecrawlBackend, "FIRECRAWL_API_KEY", "firecrawl"),
    ],
)
async def test_keyed_backend_429_translates_to_rate_limit_error(
    backend_cls, env_var, module_name, monkeypatch
) -> None:
    captured: dict[str, Any] = {}
    response = MagicMock(status_code=429)
    response.json = MagicMock(return_value={})
    monkeypatch.setenv(env_var, "ok-key")
    monkeypatch.setattr(
        f"opencomputer.tools.search_backends.{module_name}.httpx.AsyncClient",
        _fake_async_client_factory(captured, response),
    )
    with pytest.raises(SearchBackendError) as ei:
        await backend_cls().search(query="q", max_results=3, timeout_s=10)
    assert "429" in str(ei.value) or "rate" in str(ei.value).lower()


# ─── WebSearchTool integration ─────────────────────────────────────────


async def test_websearch_tool_uses_default_provider_from_constructor(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    fake_html = (
        '<div class="result"><h2 class="result__title"><a href="https://x.com">X</a></h2></div>'
    )
    response = MagicMock(status_code=200, text=fake_html)
    monkeypatch.setattr(
        "opencomputer.tools.search_backends.ddg.httpx.AsyncClient",
        _fake_async_client_factory(captured, response),
    )
    tool = WebSearchTool(default_provider="ddg")
    result = await tool.execute(_call({"query": "test"}))
    assert not result.is_error
    assert "ddg" in result.content
    assert "X" in result.content


async def test_websearch_tool_per_call_provider_override(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    fake_payload = {"web": {"results": [{"title": "B", "url": "https://b.com", "description": ""}]}}
    response = MagicMock(status_code=200)
    response.json = MagicMock(return_value=fake_payload)
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    monkeypatch.setattr(
        "opencomputer.tools.search_backends.brave.httpx.AsyncClient",
        _fake_async_client_factory(captured, response),
    )
    # Default provider is DDG, but we override per-call to brave.
    tool = WebSearchTool(default_provider="ddg")
    result = await tool.execute(_call({"query": "test", "provider": "brave"}))
    assert not result.is_error
    assert "brave" in result.content


async def test_websearch_tool_unknown_provider_returns_error() -> None:
    tool = WebSearchTool(default_provider="ddg")
    result = await tool.execute(_call({"query": "test", "provider": "yahoo"}))
    assert result.is_error
    assert "yahoo" in result.content


async def test_websearch_tool_search_backend_error_translates_to_friendly_result(
    monkeypatch,
) -> None:
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    tool = WebSearchTool(default_provider="brave")
    result = await tool.execute(_call({"query": "test"}))
    assert result.is_error
    assert "BRAVE_API_KEY" in result.content


async def test_websearch_tool_timeout_returns_friendly_error(monkeypatch) -> None:
    class _TimeoutClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **kw):
            raise httpx.TimeoutException("timed out")

    monkeypatch.setattr("opencomputer.tools.search_backends.ddg.httpx.AsyncClient", _TimeoutClient)
    tool = WebSearchTool(default_provider="ddg")
    result = await tool.execute(_call({"query": "test", "timeout_s": 1}))
    assert result.is_error
    assert "timed out" in result.content
    assert "ddg" in result.content


async def test_websearch_tool_empty_query_rejected() -> None:
    tool = WebSearchTool(default_provider="ddg")
    result = await tool.execute(_call({"query": ""}))
    assert result.is_error
    assert "required" in result.content


async def test_websearch_tool_no_hits_returns_no_results_message(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    response = MagicMock(status_code=200, text="<html></html>")
    monkeypatch.setattr(
        "opencomputer.tools.search_backends.ddg.httpx.AsyncClient",
        _fake_async_client_factory(captured, response),
    )
    tool = WebSearchTool(default_provider="ddg")
    result = await tool.execute(_call({"query": "obscure"}))
    assert not result.is_error
    assert "No results" in result.content


# ─── config dataclasses ────────────────────────────────────────────────


def test_tools_config_defaults() -> None:
    from opencomputer.agent.config import (
        ToolsConfig,
        WebSearchConfig,
        default_config,
    )

    cfg = default_config()
    assert isinstance(cfg.tools, ToolsConfig)
    assert isinstance(cfg.tools.web_search, WebSearchConfig)
    # Default provider is the keyless one — no setup required.
    assert cfg.tools.web_search.provider == "ddg"


def test_websearch_tool_constructor_defaults_pull_from_config() -> None:
    """No-arg constructor reads config.tools.web_search.provider."""
    tool = WebSearchTool()
    assert tool._default_provider == "ddg"


def test_websearch_schema_lists_all_5_provider_options() -> None:
    """The tool schema enumerates valid providers so the model knows the set."""
    tool = WebSearchTool()
    schema = tool.schema
    provider_enum = schema.parameters["properties"]["provider"]["enum"]
    assert set(provider_enum) == {"ddg", "brave", "tavily", "exa", "firecrawl"}


# ─── ABC contract ──────────────────────────────────────────────────────


def test_search_backend_is_abstract() -> None:
    """Direct instantiation of the ABC is rejected so subclasses can't forget
    to implement `search`."""
    with pytest.raises(TypeError):
        SearchBackend()  # type: ignore[abstract]
