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
