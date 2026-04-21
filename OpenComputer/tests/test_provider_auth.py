"""Tests for Anthropic provider auth modes — covers the Claude Router bug fix."""

from __future__ import annotations

import os
from unittest.mock import patch


def test_default_uses_x_api_key_header() -> None:
    from opencomputer.providers.anthropic_provider import AnthropicProvider

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
        # Make sure mode env is unset for this test
        os.environ.pop("ANTHROPIC_AUTH_MODE", None)
        p = AnthropicProvider()
        # Native Anthropic auth — SDK handles x-api-key internally via api_key.
        # We assert the client was constructed; auth headers are added by the SDK.
        assert p.client is not None


def test_bearer_mode_sets_authorization_header() -> None:
    from opencomputer.providers.anthropic_provider import AnthropicProvider

    with patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "proxy-key-xyz", "ANTHROPIC_AUTH_MODE": "bearer"},
        clear=False,
    ):
        p = AnthropicProvider()
        # Inspect the AsyncAnthropic client to confirm Authorization header set.
        # The SDK stores default_headers internally; verify via _custom_headers
        # or the internal client's default_headers attribute.
        headers = p.client.default_headers
        assert "Authorization" in headers, f"expected Authorization in {list(headers)}"
        assert headers["Authorization"] == "Bearer proxy-key-xyz"


def test_base_url_from_env() -> None:
    from opencomputer.providers.anthropic_provider import AnthropicProvider

    with patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "k",
            "ANTHROPIC_BASE_URL": "https://claude-router.vercel.app",
        },
        clear=False,
    ):
        os.environ.pop("ANTHROPIC_AUTH_MODE", None)
        p = AnthropicProvider()
        assert "claude-router.vercel.app" in str(p.client.base_url)


def test_unknown_auth_mode_raises() -> None:
    from opencomputer.providers.anthropic_provider import AnthropicProvider

    import pytest

    with patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_MODE": "garbage"},
        clear=False,
    ):
        with pytest.raises(RuntimeError, match="Unknown ANTHROPIC_AUTH_MODE"):
            AnthropicProvider()


def test_missing_api_key_raises() -> None:
    from opencomputer.providers.anthropic_provider import AnthropicProvider

    import pytest

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with pytest.raises(RuntimeError, match="API key not set"):
            AnthropicProvider()


def test_bearer_mode_strips_x_api_key_header() -> None:
    """Verify the event hook actually removes x-api-key before the request goes out."""
    import asyncio

    import httpx

    from opencomputer.providers.anthropic_provider import _strip_x_api_key

    # Build a request as the Anthropic SDK would (with x-api-key AND Authorization)
    req = httpx.Request(
        "POST",
        "https://claude-router.vercel.app/v1/messages",
        headers={
            "x-api-key": "proxy-key-123",
            "Authorization": "Bearer proxy-key-123",
            "anthropic-version": "2023-06-01",
        },
        json={"model": "claude-opus-4-7", "max_tokens": 10, "messages": []},
    )
    assert "x-api-key" in req.headers  # confirm starting state

    asyncio.run(_strip_x_api_key(req))

    assert "x-api-key" not in req.headers, "x-api-key was not stripped"
    assert req.headers["Authorization"] == "Bearer proxy-key-123", "Authorization was damaged"
