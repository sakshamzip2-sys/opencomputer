"""Tests for the Qwen OAuth provider plugin."""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).parent.parent
_OPENAI_PROVIDER_PY = _REPO / "extensions" / "openai-provider" / "provider.py"
_QWEN_PROVIDER_PY = _REPO / "extensions" / "qwen-oauth-provider" / "provider.py"


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _load():
    sys.modules.pop("provider", None)
    _load_module("provider", _OPENAI_PROVIDER_PY)
    sys.modules.pop("qwen_test", None)
    return _load_module("qwen_test", _QWEN_PROVIDER_PY)


def test_class_attributes():
    mod = _load()
    assert mod.QwenOAuthProvider.name == "qwen-oauth"
    assert mod.QwenOAuthProvider._api_key_env == "QWEN_API_KEY"


def test_uses_env_api_key_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("QWEN_API_KEY", "sk-qwen-direct")
    monkeypatch.setenv("QWEN_CREDS_PATH", str(tmp_path / "noexist.json"))
    mod = _load()
    p = mod.QwenOAuthProvider()
    assert p._api_key == "sk-qwen-direct"


def test_falls_back_to_creds_file_with_valid_token(monkeypatch, tmp_path):
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    creds_path = tmp_path / "creds.json"
    # Token expires far in the future
    future_ms = int((time.time() + 7200) * 1000)
    creds_path.write_text(json.dumps({
        "access_token": "qwen-at-from-cli",
        "refresh_token": "qwen-rt-x",
        "expiry_date": future_ms,
    }))
    monkeypatch.setenv("QWEN_CREDS_PATH", str(creds_path))

    mod = _load()
    p = mod.QwenOAuthProvider()
    assert p._api_key == "qwen-at-from-cli"


def test_refreshes_expired_token(monkeypatch, tmp_path):
    """Expired access_token triggers a refresh POST + saves new token."""
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    creds_path = tmp_path / "creds.json"
    past_ms = int((time.time() - 60) * 1000)  # expired 1 min ago
    creds_path.write_text(json.dumps({
        "access_token": "qwen-at-stale",
        "refresh_token": "qwen-rt-active",
        "expiry_date": past_ms,
    }))
    monkeypatch.setenv("QWEN_CREDS_PATH", str(creds_path))

    refreshed_response = MagicMock()
    refreshed_response.status_code = 200
    refreshed_response.json.return_value = {
        "access_token": "qwen-at-fresh",
        "refresh_token": "qwen-rt-rotated",
        "expires_in": 3600,
    }

    mod = _load()
    with patch("httpx.post", return_value=refreshed_response):
        p = mod.QwenOAuthProvider()

    assert p._api_key == "qwen-at-fresh"
    # Verify creds file was updated
    saved = json.loads(creds_path.read_text())
    assert saved["access_token"] == "qwen-at-fresh"
    assert saved["refresh_token"] == "qwen-rt-rotated"


def test_refresh_failure_raises_with_helpful_message(monkeypatch, tmp_path):
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(json.dumps({
        "access_token": "stale",
        "refresh_token": "rt-bad",
        "expiry_date": int((time.time() - 60) * 1000),
    }))
    monkeypatch.setenv("QWEN_CREDS_PATH", str(creds_path))

    bad_response = MagicMock()
    bad_response.status_code = 400
    bad_response.text = '{"error": "invalid_grant"}'

    mod = _load()
    with patch("httpx.post", return_value=bad_response):
        with pytest.raises(RuntimeError, match="qwen auth qwen-oauth"):
            mod.QwenOAuthProvider()


def test_raises_when_no_creds_and_no_env(monkeypatch, tmp_path):
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("QWEN_CREDS_PATH", str(tmp_path / "missing.json"))

    mod = _load()
    with pytest.raises(RuntimeError, match="qwen auth qwen-oauth"):
        mod.QwenOAuthProvider()


def test_raises_when_creds_file_lacks_refresh_token(monkeypatch, tmp_path):
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(json.dumps({
        "access_token": "expired-no-refresh",
        "expiry_date": int((time.time() - 60) * 1000),
    }))
    monkeypatch.setenv("QWEN_CREDS_PATH", str(creds_path))

    mod = _load()
    with pytest.raises(RuntimeError, match="qwen auth qwen-oauth"):
        mod.QwenOAuthProvider()


def test_corrupt_creds_file_treated_as_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    creds_path = tmp_path / "creds.json"
    creds_path.write_text("not valid json {{{")
    monkeypatch.setenv("QWEN_CREDS_PATH", str(creds_path))

    mod = _load()
    with pytest.raises(RuntimeError, match="qwen auth qwen-oauth"):
        mod.QwenOAuthProvider()


def test_uses_default_qwen_base_url(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "sk-x")
    monkeypatch.delenv("QWEN_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    mod = _load()
    p = mod.QwenOAuthProvider()
    assert "portal.qwen.ai" in p._base


def test_respects_qwen_base_url_override(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "sk-x")
    monkeypatch.setenv("QWEN_BASE_URL", "https://qwen-internal/v1")
    mod = _load()
    p = mod.QwenOAuthProvider()
    assert p._base == "https://qwen-internal/v1"


def test_is_expiring_true_for_past_timestamp():
    mod = _load()
    past_ms = int((time.time() - 60) * 1000)
    assert mod._is_expiring(past_ms) is True


def test_is_expiring_false_for_distant_future():
    mod = _load()
    future_ms = int((time.time() + 3600) * 1000)
    assert mod._is_expiring(future_ms) is False


def test_is_expiring_true_for_unparseable():
    mod = _load()
    assert mod._is_expiring("not-a-number") is True
    assert mod._is_expiring(None) is True


def test_plugin_manifest():
    manifest = json.loads(
        (_REPO / "extensions" / "qwen-oauth-provider" / "plugin.json").read_text()
    )
    assert manifest["entry"] == "plugin"
    setup = manifest["setup"]["providers"][0]
    assert setup["id"] == "qwen-oauth"
    assert "QWEN_API_KEY" in setup["env_vars"]


def test_appears_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        _discover_providers,
    )
    ids = {p["name"] for p in _discover_providers()}
    assert "qwen-oauth" in ids
