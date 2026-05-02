"""Tests for OCMCPOAuthClient — the SDK-aligned MCP OAuth token store.

These tests cover the *new* primitives added to ``opencomputer/mcp/oauth.py``
to wrap the MCP Python SDK's :class:`OAuthClientProvider`. They do NOT
exercise the legacy :class:`OAuthTokenStore` (one-file-per-provider PAT
storage, see ``tests/test_mcp_oauth.py`` for that).

Plan section: ``docs/superpowers/plans/2026-05-02-best-of-import.md`` Task 1.2.
"""

from __future__ import annotations

import json

import pytest

from opencomputer.mcp.oauth import OCMCPOAuthClient, _tokens_path


def test_tokens_path_is_profile_aware(tmp_path, monkeypatch):
    """``_tokens_path()`` must resolve under the current profile home."""
    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    p = _tokens_path()
    assert p == tmp_path / "mcp" / "tokens.json"


def test_load_returns_empty_when_missing(tmp_path, monkeypatch):
    """Loading from a non-existent store returns an empty dict (never raises)."""
    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    assert OCMCPOAuthClient(server_name="github").load_tokens() == {}


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    """A roundtrip save→load returns the same payload for the same server name."""
    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    c = OCMCPOAuthClient(server_name="github")
    c.save_tokens({"access_token": "tok", "refresh_token": "ref"})
    assert c.load_tokens() == {"access_token": "tok", "refresh_token": "ref"}


def test_save_preserves_other_servers(tmp_path, monkeypatch):
    """Saving one server's tokens MUST NOT clobber other servers' entries."""
    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    OCMCPOAuthClient(server_name="github").save_tokens({"access_token": "g"})
    OCMCPOAuthClient(server_name="notion").save_tokens({"access_token": "n"})
    saved = json.loads((tmp_path / "mcp" / "tokens.json").read_text())
    assert saved["github"]["access_token"] == "g"
    assert saved["notion"]["access_token"] == "n"


def test_provider_factory_returns_sdk_provider(tmp_path, monkeypatch):
    """``as_sdk_provider`` returns an instance of the SDK's ``OAuthClientProvider``.

    The SDK's constructor is well-defined (verified against mcp>=1.6 in dev).
    If the SDK's API drifts in a future major bump, this test will be the
    first signal.
    """
    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    pytest.importorskip("mcp")
    from mcp.client.auth import OAuthClientProvider

    c = OCMCPOAuthClient(server_name="github")
    p = c.as_sdk_provider(
        server_url="https://example.invalid",
        client_metadata={
            "client_name": "OpenComputer",
            "redirect_uris": ["http://localhost:5454/callback"],
        },
    )
    assert isinstance(p, OAuthClientProvider)
