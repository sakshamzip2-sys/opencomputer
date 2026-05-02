"""Tests for the Gemini OAuth provider plugin."""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).parent.parent
_OPENAI_PROVIDER_PY = _REPO / "extensions" / "openai-provider" / "provider.py"
_GEMINI_PROVIDER_PY = _REPO / "extensions" / "gemini-oauth-provider" / "provider.py"


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _load():
    sys.modules.pop("provider", None)
    _load_module("provider", _OPENAI_PROVIDER_PY)
    sys.modules.pop("gemini_oauth_test", None)
    return _load_module("gemini_oauth_test", _GEMINI_PROVIDER_PY)


def test_class_attributes():
    mod = _load()
    assert mod.GeminiOAuthProvider.name == "gemini-oauth"
    assert mod.GeminiOAuthProvider.default_model.startswith("gemini-")


def test_raises_when_not_logged_in(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    mod = _load()
    with pytest.raises(RuntimeError, match="opencomputer auth login google"):
        mod.GeminiOAuthProvider()


def test_uses_cached_token_when_logged_in(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "google_oauth.json").write_text(json.dumps({
        "access_token": "google-at-fresh",
        "refresh_token": "google-rt",
        "expires_ms": int((time.time() + 3600) * 1000),
        "email": "u@example.com",
        "project_id": "",
    }))
    mod = _load()
    p = mod.GeminiOAuthProvider()
    assert p._api_key == "google-at-fresh"


def test_base_url_marker_is_cloudcode():
    """The base_url is the cloudcode-pa:// marker so the OpenAI HTTP shape
    cannot accidentally fire against AI Studio (which would error anyway)."""
    mod = _load()
    assert mod.DEFAULT_GEMINI_CLOUDCODE_BASE_URL == "cloudcode-pa://google"


def test_complete_raises_not_implemented(tmp_path, monkeypatch):
    """Cloud Code Assist adapter is the pending follow-up — surface it loudly."""
    import asyncio

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "google_oauth.json").write_text(json.dumps({
        "access_token": "x",
        "refresh_token": "y",
        "expires_ms": int((time.time() + 3600) * 1000),
    }))
    mod = _load()
    p = mod.GeminiOAuthProvider()
    with pytest.raises(NotImplementedError, match="Cloud Code Assist"):
        asyncio.run(p.complete([], "gemini-2.5-pro", []))


def test_plugin_manifest():
    manifest = json.loads(
        (_REPO / "extensions" / "gemini-oauth-provider" / "plugin.json").read_text()
    )
    assert manifest["entry"] == "plugin"
    setup = manifest["setup"]["providers"][0]
    assert setup["id"] == "gemini-oauth"
    assert "oauth_external" in setup["auth_methods"]


def test_appears_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        _discover_providers,
    )
    ids = {p["name"] for p in _discover_providers()}
    assert "gemini-oauth" in ids
