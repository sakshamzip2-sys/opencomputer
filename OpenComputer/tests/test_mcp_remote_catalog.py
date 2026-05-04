"""Tests for MCP remote catalog fetch + cache (T2 of mcp-deferrals-v2)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from opencomputer.mcp import remote_catalog

SAMPLE_CATALOG = {
    "version": "1",
    "servers": [
        {
            "slug": "filesystem",
            "description": "Read/write files within a configured root.",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
            "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        },
        {
            "slug": "github",
            "description": "Read GitHub repos, issues, PRs.",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "required_env": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
            "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/github",
        },
    ],
}


def _stub_httpx(monkeypatch, *, json_data=None, raise_exc=None):
    """Replace httpx.get with a stub returning json_data or raising."""
    class _MockResponse:
        def json(self):
            if raise_exc is not None:
                raise raise_exc
            return json_data
        def raise_for_status(self):
            return None
    def _mock_get(url, **kwargs):
        if raise_exc is not None and not isinstance(raise_exc, json.JSONDecodeError):
            raise raise_exc
        return _MockResponse()
    monkeypatch.setattr(remote_catalog.httpx, "get", _mock_get)


def test_fetch_writes_cache(tmp_path, monkeypatch):
    """Successful fetch persists the data to the cache path."""
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(remote_catalog, "_CACHE_PATH", cache_path)
    _stub_httpx(monkeypatch, json_data=SAMPLE_CATALOG)

    data = remote_catalog.fetch_catalog(refresh=True)
    assert data == SAMPLE_CATALOG
    assert cache_path.exists()
    cached = json.loads(cache_path.read_text())
    assert cached == SAMPLE_CATALOG


def test_fetch_uses_cache_when_fresh(tmp_path, monkeypatch):
    """Within TTL, fetch returns cache without hitting network."""
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(SAMPLE_CATALOG))
    monkeypatch.setattr(remote_catalog, "_CACHE_PATH", cache_path)

    network_calls = {"count": 0}
    def _mock_get(url, **kwargs):
        network_calls["count"] += 1
        raise RuntimeError("should not have been called")
    monkeypatch.setattr(remote_catalog.httpx, "get", _mock_get)

    data = remote_catalog.fetch_catalog(refresh=False)
    assert data == SAMPLE_CATALOG
    assert network_calls["count"] == 0


def test_fetch_bypasses_cache_when_refresh_true(tmp_path, monkeypatch):
    """refresh=True forces a network call even with a fresh cache."""
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"version": "0", "servers": []}))
    monkeypatch.setattr(remote_catalog, "_CACHE_PATH", cache_path)
    _stub_httpx(monkeypatch, json_data=SAMPLE_CATALOG)

    data = remote_catalog.fetch_catalog(refresh=True)
    assert data == SAMPLE_CATALOG  # got the network response, not stale cache


def test_fetch_falls_back_to_stale_cache_on_network_failure(tmp_path, monkeypatch):
    """If network fails AND cache exists (even stale), return cache + warn."""
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(SAMPLE_CATALOG))
    # Make cache appear ancient so it's outside TTL
    ancient = time.time() - 86400 * 10  # 10 days ago
    import os
    os.utime(cache_path, (ancient, ancient))
    monkeypatch.setattr(remote_catalog, "_CACHE_PATH", cache_path)
    _stub_httpx(monkeypatch, raise_exc=httpx.NetworkError("offline"))

    data = remote_catalog.fetch_catalog(refresh=True)
    assert data == SAMPLE_CATALOG  # stale cache returned


def test_fetch_raises_when_network_fails_and_no_cache(tmp_path, monkeypatch):
    """No cache + network failure → raise."""
    cache_path = tmp_path / "cache.json"  # doesn't exist
    monkeypatch.setattr(remote_catalog, "_CACHE_PATH", cache_path)
    _stub_httpx(monkeypatch, raise_exc=httpx.NetworkError("offline"))

    with pytest.raises(remote_catalog.CatalogFetchError):
        remote_catalog.fetch_catalog(refresh=True)


def test_fetch_handles_corrupted_cache(tmp_path, monkeypatch):
    """Cache file present but malformed → falls through to network fetch."""
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("not-valid-json{")
    monkeypatch.setattr(remote_catalog, "_CACHE_PATH", cache_path)
    _stub_httpx(monkeypatch, json_data=SAMPLE_CATALOG)

    data = remote_catalog.fetch_catalog(refresh=False)
    assert data == SAMPLE_CATALOG
    # And the corrupted cache was overwritten
    assert json.loads(cache_path.read_text()) == SAMPLE_CATALOG


def test_format_catalog_for_display():
    """Helper that turns the JSON into a human-readable string."""
    out = remote_catalog.format_catalog_for_display(SAMPLE_CATALOG)
    assert "filesystem" in out
    assert "github" in out
    assert "Read GitHub repos" in out
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in out  # required_env shown


def test_format_catalog_handles_empty():
    out = remote_catalog.format_catalog_for_display({"version": "1", "servers": []})
    assert "no servers" in out.lower() or "empty" in out.lower()


# ─── resolve_catalog_url — D.3 T2 closes PR #437 deferral ───


def test_resolve_explicit_arg_wins(monkeypatch):
    """Explicit url= takes precedence over env, config, and default."""
    monkeypatch.setenv("OC_MCP_CATALOG_URL", "https://env-override.example/c.json")
    out = remote_catalog.resolve_catalog_url("https://explicit.example/x.json")
    assert out == "https://explicit.example/x.json"


def test_resolve_env_var_wins_over_default(monkeypatch):
    monkeypatch.setenv("OC_MCP_CATALOG_URL", "https://env.example/c.json")
    # Force load_config to not provide a value
    import opencomputer.agent.config_store as cs
    monkeypatch.setattr(cs, "load_config", lambda *a, **kw: cs.default_config())
    out = remote_catalog.resolve_catalog_url()
    assert out == "https://env.example/c.json"


def test_resolve_env_var_strips_nothing_special(monkeypatch):
    """Env var is used as-is (no whitespace handling — that's the user's job)."""
    monkeypatch.setenv("OC_MCP_CATALOG_URL", "https://env.example/c.json?v=2")
    out = remote_catalog.resolve_catalog_url()
    assert out == "https://env.example/c.json?v=2"


def test_resolve_falls_back_to_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("OC_MCP_CATALOG_URL", raising=False)
    # Force load_config to provide nothing
    import opencomputer.agent.config_store as cs
    monkeypatch.setattr(cs, "load_config", lambda *a, **kw: cs.default_config())
    out = remote_catalog.resolve_catalog_url()
    assert out == remote_catalog._DEFAULT_CATALOG_URL


def test_resolve_uses_config_when_env_unset(monkeypatch):
    """When neither explicit arg nor env is set, profile config wins."""
    monkeypatch.delenv("OC_MCP_CATALOG_URL", raising=False)
    from dataclasses import replace

    import opencomputer.agent.config_store as cs

    def _fake_load_config(*a, **kw):
        base = cs.default_config()
        return replace(base, mcp=replace(base.mcp, catalog_url="https://cfg.example/c.json"))

    monkeypatch.setattr(cs, "load_config", _fake_load_config)
    out = remote_catalog.resolve_catalog_url()
    assert out == "https://cfg.example/c.json"


def test_resolve_treats_empty_config_as_unset(monkeypatch):
    """An empty-string catalog_url in config falls through to default."""
    monkeypatch.delenv("OC_MCP_CATALOG_URL", raising=False)
    from dataclasses import replace

    import opencomputer.agent.config_store as cs

    def _fake_load_config(*a, **kw):
        base = cs.default_config()
        return replace(base, mcp=replace(base.mcp, catalog_url=""))

    monkeypatch.setattr(cs, "load_config", _fake_load_config)
    out = remote_catalog.resolve_catalog_url()
    assert out == remote_catalog._DEFAULT_CATALOG_URL


def test_resolve_treats_whitespace_only_config_as_unset(monkeypatch):
    monkeypatch.delenv("OC_MCP_CATALOG_URL", raising=False)
    from dataclasses import replace

    import opencomputer.agent.config_store as cs

    def _fake_load_config(*a, **kw):
        base = cs.default_config()
        return replace(base, mcp=replace(base.mcp, catalog_url="   "))

    monkeypatch.setattr(cs, "load_config", _fake_load_config)
    out = remote_catalog.resolve_catalog_url()
    assert out == remote_catalog._DEFAULT_CATALOG_URL


def test_resolve_swallows_config_load_failure(monkeypatch):
    """If load_config raises (corrupt YAML, missing file, etc), we fall
    through to the default — never propagate a config error to a network
    fetch site."""
    monkeypatch.delenv("OC_MCP_CATALOG_URL", raising=False)
    import opencomputer.agent.config_store as cs

    def _broken_load_config(*a, **kw):
        raise RuntimeError("YAML parse error")

    monkeypatch.setattr(cs, "load_config", _broken_load_config)
    out = remote_catalog.resolve_catalog_url()
    assert out == remote_catalog._DEFAULT_CATALOG_URL


def test_fetch_catalog_uses_resolved_url(tmp_path, monkeypatch):
    """End-to-end: env override flows through fetch_catalog → httpx call."""
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(remote_catalog, "_CACHE_PATH", cache_path)
    monkeypatch.setenv("OC_MCP_CATALOG_URL", "https://override.example/c.json")

    captured: dict = {}

    class _MockResponse:
        def json(self):
            return SAMPLE_CATALOG
        def raise_for_status(self):
            return None

    def _mock_get(url, **kwargs):
        captured["url"] = url
        return _MockResponse()

    monkeypatch.setattr(remote_catalog.httpx, "get", _mock_get)

    remote_catalog.fetch_catalog(refresh=True)
    assert captured["url"] == "https://override.example/c.json"


def test_explicit_url_param_still_works_after_refactor(tmp_path, monkeypatch):
    """Backwards compat: callers passing url= continue to bypass the env+config chain."""
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(remote_catalog, "_CACHE_PATH", cache_path)
    monkeypatch.setenv("OC_MCP_CATALOG_URL", "https://env-should-lose.example/c.json")
    _stub_httpx(monkeypatch, json_data=SAMPLE_CATALOG)

    captured: dict = {}

    class _MockResponse:
        def json(self):
            return SAMPLE_CATALOG
        def raise_for_status(self):
            return None

    def _mock_get(url, **kwargs):
        captured["url"] = url
        return _MockResponse()

    monkeypatch.setattr(remote_catalog.httpx, "get", _mock_get)

    remote_catalog.fetch_catalog(refresh=True, url="https://explicit-arg.example/x.json")
    assert captured["url"] == "https://explicit-arg.example/x.json"


def test_mcp_config_dataclass_has_catalog_url_field():
    """Schema check — load_config must round-trip a YAML mcp.catalog_url
    value through MCPConfig.catalog_url. Defaults to empty string."""
    from opencomputer.agent.config import MCPConfig
    cfg = MCPConfig()
    assert cfg.catalog_url == ""

    custom = MCPConfig(catalog_url="https://my-fork.example/c.json")
    assert custom.catalog_url == "https://my-fork.example/c.json"
