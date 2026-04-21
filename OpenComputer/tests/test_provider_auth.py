"""Tests for Anthropic provider auth modes — covers the Claude Router bug fix.

The Anthropic provider lives in extensions/anthropic-provider/. Folder has
a dash so we can't use 'from extensions.anthropic-provider.provider import X'.
Instead, use sys.path manipulation like test_phase3.py does for openai.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch


def _import_anthropic_provider():
    """Load the provider module directly from its path, bypassing sys.modules cache.

    Why not a plain import? The openai-provider plugin also has a module named
    `provider`; Python's module cache would return whichever was loaded first.
    """
    import importlib.util

    repo_root = Path(__file__).resolve().parent.parent
    provider_path = repo_root / "extensions" / "anthropic-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location(
        "anthropic_provider_test_only", provider_path
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["anthropic_provider_test_only"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_default_uses_x_api_key_header() -> None:
    mod = _import_anthropic_provider()
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
        os.environ.pop("ANTHROPIC_AUTH_MODE", None)
        p = mod.AnthropicProvider()
        assert p.client is not None


def test_bearer_mode_sets_authorization_header() -> None:
    mod = _import_anthropic_provider()
    with patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "proxy-key-xyz", "ANTHROPIC_AUTH_MODE": "bearer"},
        clear=False,
    ):
        p = mod.AnthropicProvider()
        headers = p.client.default_headers
        assert "Authorization" in headers
        assert headers["Authorization"] == "Bearer proxy-key-xyz"


def test_base_url_from_env() -> None:
    mod = _import_anthropic_provider()
    with patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "k",
            "ANTHROPIC_BASE_URL": "https://claude-router.vercel.app",
        },
        clear=False,
    ):
        os.environ.pop("ANTHROPIC_AUTH_MODE", None)
        p = mod.AnthropicProvider()
        assert "claude-router.vercel.app" in str(p.client.base_url)


def test_unknown_auth_mode_raises() -> None:
    import pytest

    mod = _import_anthropic_provider()
    with patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_MODE": "garbage"},
        clear=False,
    ):
        with pytest.raises(RuntimeError, match="Unknown ANTHROPIC_AUTH_MODE"):
            mod.AnthropicProvider()


def test_missing_api_key_raises() -> None:
    import pytest

    mod = _import_anthropic_provider()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with pytest.raises(RuntimeError, match="API key not set"):
            mod.AnthropicProvider()


def test_bearer_mode_strips_x_api_key_header() -> None:
    """Verify the event hook actually removes x-api-key before the request goes out."""
    import asyncio

    import httpx

    mod = _import_anthropic_provider()

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
    assert "x-api-key" in req.headers

    asyncio.run(mod._strip_x_api_key(req))

    assert "x-api-key" not in req.headers
    assert req.headers["Authorization"] == "Bearer proxy-key-123"
