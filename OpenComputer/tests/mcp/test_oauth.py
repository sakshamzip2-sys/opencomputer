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


# --- Pydantic round-trip through _SDKStorageAdapter ---------------------------
# These tests guard against the bug where the adapter returned raw dicts but
# the SDK consumer code treats results as Pydantic models (attribute access).
# Real OAuth flows would crash on first refresh after restart without these.


def test_adapter_get_tokens_returns_oauth_token_model(tmp_path, monkeypatch):
    """get_tokens MUST return an OAuthToken Pydantic instance, not a dict."""
    import asyncio

    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    pytest.importorskip("mcp")
    from mcp.shared.auth import OAuthToken

    from opencomputer.mcp.oauth import _SDKStorageAdapter

    c = OCMCPOAuthClient(server_name="github")
    c.save_tokens({"access_token": "tok", "token_type": "Bearer", "expires_in": 3600})
    adapter = _SDKStorageAdapter(c)
    out = asyncio.run(adapter.get_tokens())
    assert isinstance(out, OAuthToken)
    assert out.access_token == "tok"
    # Attribute access (the SDK pattern) must work
    assert out.token_type == "Bearer"


def test_adapter_get_client_info_returns_pydantic_model(tmp_path, monkeypatch):
    """get_client_info MUST return an OAuthClientInformationFull instance."""
    import asyncio

    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    pytest.importorskip("mcp")
    from mcp.shared.auth import OAuthClientInformationFull

    from opencomputer.mcp.oauth import _SDKStorageAdapter

    c = OCMCPOAuthClient(server_name="github")
    c.save_tokens({
        "client_info": {
            "client_id": "abc123",
            "redirect_uris": ["http://localhost:5454/callback"],
        },
    })
    adapter = _SDKStorageAdapter(c)
    out = asyncio.run(adapter.get_client_info())
    assert isinstance(out, OAuthClientInformationFull)
    assert out.client_id == "abc123"


def test_adapter_set_tokens_preserves_client_info_under_lock(tmp_path, monkeypatch):
    """set_tokens MUST preserve a previously-saved client_info via under-lock merge."""
    import asyncio

    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    pytest.importorskip("mcp")
    from mcp.shared.auth import OAuthToken

    from opencomputer.mcp.oauth import _SDKStorageAdapter

    c = OCMCPOAuthClient(server_name="github")
    # Pre-existing client_info from an earlier dynamic registration
    c.save_tokens({"client_info": {"client_id": "abc123", "redirect_uris": ["http://x"]}})

    adapter = _SDKStorageAdapter(c)
    new_token = OAuthToken(access_token="rotated", token_type="Bearer", expires_in=3600)
    asyncio.run(adapter.set_tokens(new_token))

    saved = c.load_tokens()
    assert saved["access_token"] == "rotated"
    assert saved["client_info"]["client_id"] == "abc123"


def test_save_tokens_xor_argument_validation(tmp_path, monkeypatch):
    """save_tokens must require exactly one of `tokens=` or `mutator=`."""
    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    c = OCMCPOAuthClient(server_name="github")
    with pytest.raises(ValueError, match="exactly one"):
        c.save_tokens()  # neither
    with pytest.raises(ValueError, match="exactly one"):
        c.save_tokens({"a": 1}, mutator=lambda d: d)  # both


def test_all_tokens_handles_non_dict_corruption(tmp_path, monkeypatch, caplog):
    """A tokens.json that's a JSON list/string/null must not crash later reads."""
    import logging

    monkeypatch.setattr("opencomputer.agent.config._home", lambda: tmp_path)
    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp" / "tokens.json").write_text(json.dumps(["unexpected", "list"]))
    c = OCMCPOAuthClient(server_name="github")
    with caplog.at_level(logging.ERROR, logger="opencomputer.mcp.oauth"):
        result = c.load_tokens()
    assert result == {}
    assert any("not a JSON object" in rec.message for rec in caplog.records)
