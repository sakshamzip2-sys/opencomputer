"""Tier B item 19 — link auto-fetch + injection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from opencomputer.agent.link_understanding import (
    LinkFetcher,
    LinkUnderstandingConfig,
    _clear_caches,
    extract_urls,
    is_safe_url,
)


@pytest.fixture(autouse=True)
def reset_state():
    _clear_caches()
    yield
    _clear_caches()


# ──────────────────────────── URL extraction ────────────────────────────


def test_extract_simple_https_url():
    assert extract_urls("see https://example.com") == ["https://example.com"]


def test_extract_strips_trailing_punctuation():
    text = "Read https://example.com/article. It's good."
    assert extract_urls(text) == ["https://example.com/article"]


def test_extract_dedups_preserving_order():
    text = "https://a.com and https://b.com and https://a.com again"
    assert extract_urls(text) == ["https://a.com", "https://b.com"]


def test_extract_caps_at_max_urls():
    text = " ".join(f"https://site{i}.com" for i in range(10))
    assert len(extract_urls(text, max_urls=3)) == 3


def test_extract_handles_empty_text():
    assert extract_urls("") == []
    assert extract_urls(None) == []  # type: ignore[arg-type]


def test_extract_skips_non_http_schemes():
    assert extract_urls("ftp://x.com or file:///etc") == []


def test_extract_handles_complex_urls_with_query_strings():
    text = "https://api.example.com/v2/data?key=abc&user=123#section"
    out = extract_urls(text)
    assert len(out) == 1
    assert out[0].startswith("https://api.example.com/v2/data")


# ──────────────────────────── SSRF guard ────────────────────────────


def test_is_safe_url_blocks_loopback():
    assert is_safe_url("http://127.0.0.1/x") is False
    assert is_safe_url("http://localhost/x") is False  # resolves to 127.x


def test_is_safe_url_blocks_private_ips():
    assert is_safe_url("http://10.0.0.1/x") is False
    assert is_safe_url("http://192.168.1.1/x") is False
    assert is_safe_url("http://172.16.0.1/x") is False


def test_is_safe_url_blocks_cloud_metadata():
    assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False
    assert is_safe_url("http://metadata.google.internal/") is False


def test_is_safe_url_blocks_link_local():
    assert is_safe_url("http://169.254.0.1/x") is False


def test_is_safe_url_blocks_non_http():
    assert is_safe_url("file:///etc/passwd") is False
    assert is_safe_url("ftp://example.com/") is False


def test_is_safe_url_blocks_no_host():
    assert is_safe_url("http:///") is False
    assert is_safe_url("https://") is False


def test_is_safe_url_accepts_public_dns(monkeypatch):
    """Real DNS — but mock to avoid network in CI."""
    import socket as sock_mod

    monkeypatch.setattr(
        sock_mod,
        "getaddrinfo",
        lambda host, port: [(sock_mod.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
    )
    assert is_safe_url("https://example.com/") is True


def test_is_safe_url_blocks_when_dns_resolves_to_private(monkeypatch):
    """DNS-rebinding defense: if the public name resolves to a private IP,
    refuse anyway."""
    import socket as sock_mod

    monkeypatch.setattr(
        sock_mod,
        "getaddrinfo",
        lambda host, port: [(sock_mod.AF_INET, 0, 0, "", ("10.0.0.1", 0))],
    )
    assert is_safe_url("https://attacker-controlled.com/") is False


# ──────────────────────────── injection provider ────────────────────────────


@dataclass
class _FakeMessage:
    role: str
    content: str


@dataclass
class _FakeRuntime:
    plan_mode: bool = False
    yolo_mode: bool = False


@pytest.mark.asyncio
async def test_provider_returns_none_when_disabled():
    from opencomputer.agent.injection_providers.link_summary import (
        LinkUnderstandingInjectionProvider,
    )
    from plugin_sdk.injection import InjectionContext

    cfg = LinkUnderstandingConfig(enabled=False)
    p = LinkUnderstandingInjectionProvider(config=cfg)
    ctx = InjectionContext(
        messages=(_FakeMessage("user", "https://example.com"),),  # type: ignore[arg-type]
        runtime=_FakeRuntime(),  # type: ignore[arg-type]
    )
    assert await p.collect(ctx) is None


@pytest.mark.asyncio
async def test_provider_returns_none_when_no_urls():
    from opencomputer.agent.injection_providers.link_summary import (
        LinkUnderstandingInjectionProvider,
    )
    from plugin_sdk.injection import InjectionContext

    p = LinkUnderstandingInjectionProvider()
    ctx = InjectionContext(
        messages=(_FakeMessage("user", "no urls here"),),  # type: ignore[arg-type]
        runtime=_FakeRuntime(),  # type: ignore[arg-type]
    )
    assert await p.collect(ctx) is None


@pytest.mark.asyncio
async def test_provider_fetches_safe_url(monkeypatch):
    """End-to-end: provider sees a URL, calls LinkFetcher, returns summary."""
    from opencomputer.agent.injection_providers.link_summary import (
        LinkUnderstandingInjectionProvider,
    )
    from plugin_sdk.injection import InjectionContext

    fake_fetcher = LinkFetcher.__new__(LinkFetcher)
    fake_fetcher.fetch = AsyncMock(return_value="article body text")  # type: ignore[method-assign]
    monkeypatch.setattr(
        "opencomputer.agent.injection_providers.link_summary.is_safe_url",
        lambda url: True,
    )

    p = LinkUnderstandingInjectionProvider(fetcher=fake_fetcher)
    ctx = InjectionContext(
        messages=(_FakeMessage("user", "check https://example.com/article"),),  # type: ignore[arg-type]
        runtime=_FakeRuntime(),  # type: ignore[arg-type]
        session_id="s1",
    )
    out = await p.collect(ctx)
    assert out is not None
    assert "## Link summaries" in out
    assert "https://example.com/article" in out
    assert "article body text" in out


@pytest.mark.asyncio
async def test_provider_refuses_unsafe_url(monkeypatch):
    from opencomputer.agent.injection_providers.link_summary import (
        LinkUnderstandingInjectionProvider,
    )
    from plugin_sdk.injection import InjectionContext

    fake_fetcher = LinkFetcher.__new__(LinkFetcher)
    fake_fetcher.fetch = AsyncMock(return_value="should-not-be-fetched")  # type: ignore[method-assign]
    monkeypatch.setattr(
        "opencomputer.agent.injection_providers.link_summary.is_safe_url",
        lambda url: False,
    )
    p = LinkUnderstandingInjectionProvider(fetcher=fake_fetcher)
    ctx = InjectionContext(
        messages=(_FakeMessage("user", "see http://169.254.169.254/"),),  # type: ignore[arg-type]
        runtime=_FakeRuntime(),  # type: ignore[arg-type]
        session_id="s1",
    )
    out = await p.collect(ctx)
    assert out is not None
    assert "refused" in out.lower()
    fake_fetcher.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_provider_caches_per_session(monkeypatch):
    from opencomputer.agent.injection_providers.link_summary import (
        LinkUnderstandingInjectionProvider,
    )
    from plugin_sdk.injection import InjectionContext

    monkeypatch.setattr(
        "opencomputer.agent.injection_providers.link_summary.is_safe_url",
        lambda url: True,
    )
    fake_fetcher = LinkFetcher.__new__(LinkFetcher)
    fake_fetcher.fetch = AsyncMock(return_value="body")  # type: ignore[method-assign]

    p = LinkUnderstandingInjectionProvider(fetcher=fake_fetcher)

    ctx = InjectionContext(
        messages=(_FakeMessage("user", "see https://example.com"),),  # type: ignore[arg-type]
        runtime=_FakeRuntime(),  # type: ignore[arg-type]
        session_id="s1",
    )
    await p.collect(ctx)
    await p.collect(ctx)
    assert fake_fetcher.fetch.call_count == 1  # cached on second call


@pytest.mark.asyncio
async def test_provider_swallows_fetch_error(monkeypatch):
    from opencomputer.agent.injection_providers.link_summary import (
        LinkUnderstandingInjectionProvider,
    )
    from plugin_sdk.injection import InjectionContext

    monkeypatch.setattr(
        "opencomputer.agent.injection_providers.link_summary.is_safe_url",
        lambda url: True,
    )
    fake_fetcher = LinkFetcher.__new__(LinkFetcher)
    fake_fetcher.fetch = AsyncMock(return_value=None)  # signal error  # type: ignore[method-assign]

    p = LinkUnderstandingInjectionProvider(fetcher=fake_fetcher)
    ctx = InjectionContext(
        messages=(_FakeMessage("user", "see https://example.com"),),  # type: ignore[arg-type]
        runtime=_FakeRuntime(),  # type: ignore[arg-type]
        session_id="s1",
    )
    out = await p.collect(ctx)
    assert out is not None
    assert "fetch failed" in out


@pytest.mark.asyncio
async def test_provider_caps_max_urls_per_message():
    from opencomputer.agent.injection_providers.link_summary import (
        LinkUnderstandingInjectionProvider,
    )

    cfg = LinkUnderstandingConfig(enabled=True, max_urls_per_message=2)
    fake_fetcher = LinkFetcher.__new__(LinkFetcher)
    fake_fetcher.fetch = AsyncMock(return_value="body")  # type: ignore[method-assign]
    with patch(
        "opencomputer.agent.injection_providers.link_summary.is_safe_url",
        lambda url: True,
    ):
        from plugin_sdk.injection import InjectionContext

        p = LinkUnderstandingInjectionProvider(config=cfg, fetcher=fake_fetcher)
        ctx = InjectionContext(
            messages=(_FakeMessage(  # type: ignore[arg-type]
                "user",
                "https://a.com https://b.com https://c.com https://d.com",
            ),),
            runtime=_FakeRuntime(),  # type: ignore[arg-type]
            session_id="s1",
        )
        await p.collect(ctx)
    assert fake_fetcher.fetch.call_count == 2
