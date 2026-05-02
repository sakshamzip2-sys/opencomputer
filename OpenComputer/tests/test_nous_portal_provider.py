"""Tests for the Nous Portal provider plugin (O.b)."""
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
_NOUS_PROVIDER_PY = _REPO / "extensions" / "nous-portal-provider" / "provider.py"


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _load_class():
    sys.modules.pop("provider", None)
    _load_module("provider", _OPENAI_PROVIDER_PY)
    sys.modules.pop("nous_portal_test", None)
    mod = _load_module("nous_portal_test", _NOUS_PROVIDER_PY)
    return mod


def test_class_attributes():
    mod = _load_class()
    Cls = mod.NousPortalProvider
    assert Cls.name == "nous-portal"
    assert Cls._api_key_env == "NOUS_PORTAL_API_KEY"
    assert "Hermes" in Cls.default_model


def test_uses_env_api_key_when_set(monkeypatch):
    monkeypatch.setenv("NOUS_PORTAL_API_KEY", "sk-nous-direct")
    mod = _load_class()
    p = mod.NousPortalProvider()
    assert p._api_key == "sk-nous-direct"


def test_falls_back_to_oauth_token_store(monkeypatch, tmp_path):
    """When NOUS_PORTAL_API_KEY isn't set, load token from auth store."""
    monkeypatch.delenv("NOUS_PORTAL_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # Set up a fake token store with a non-expired Nous Portal token
    from opencomputer.auth import OAuthToken, save_token
    token_store = tmp_path / "auth_tokens.json"
    monkeypatch.setattr(
        "opencomputer.auth.token_store.default_store_path",
        lambda: token_store,
    )
    save_token(OAuthToken(
        provider="nous-portal",
        access_token="at-from-oauth",
        refresh_token="rt-test",
        expires_at=int(time.time()) + 3600,
    ))

    mod = _load_class()
    p = mod.NousPortalProvider()
    assert p._api_key == "at-from-oauth"


def test_raises_when_neither_env_nor_token_store(monkeypatch, tmp_path):
    monkeypatch.delenv("NOUS_PORTAL_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Empty token store
    monkeypatch.setattr(
        "opencomputer.auth.token_store.default_store_path",
        lambda: tmp_path / "auth_tokens.json",
    )

    mod = _load_class()
    with pytest.raises(RuntimeError, match="NOUS_PORTAL_API_KEY"):
        mod.NousPortalProvider()


def test_does_not_use_expired_oauth_token(monkeypatch, tmp_path):
    """Expired OAuth token is ignored, raises as if no token at all."""
    monkeypatch.delenv("NOUS_PORTAL_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from opencomputer.auth import OAuthToken, save_token
    token_store = tmp_path / "auth_tokens.json"
    monkeypatch.setattr(
        "opencomputer.auth.token_store.default_store_path",
        lambda: token_store,
    )
    save_token(OAuthToken(
        provider="nous-portal",
        access_token="at-expired",
        expires_at=int(time.time()) - 60,  # expired 1 min ago
    ))

    mod = _load_class()
    with pytest.raises(RuntimeError, match="NOUS_PORTAL_API_KEY"):
        mod.NousPortalProvider()


def test_run_device_code_login_saves_token(monkeypatch, tmp_path):
    """End-to-end: device-code → poll → save."""
    from opencomputer.auth import load_token

    token_store = tmp_path / "auth_tokens.json"
    monkeypatch.setattr(
        "opencomputer.auth.token_store.default_store_path",
        lambda: token_store,
    )

    device_resp = MagicMock()
    device_resp.status_code = 200
    device_resp.json.return_value = {
        "device_code": "dc-x",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://portal.nousresearch.com/activate",
        "verification_uri_complete": "https://portal.nousresearch.com/activate?code=ABCD-EFGH",
        "expires_in": 600,
        "interval": 1,
    }

    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {
        "access_token": "at-fresh",
        "refresh_token": "rt-fresh",
        "expires_in": 3600,
    }

    captured_logs: list[str] = []

    mod = _load_class()
    with patch("httpx.post", side_effect=[device_resp, token_resp]):
        with patch("time.sleep"):
            mod.run_device_code_login(
                client_id="opencomputer-test",
                print_fn=captured_logs.append,
            )

    saved = load_token("nous-portal")
    assert saved is not None
    assert saved.access_token == "at-fresh"
    # Verify user-facing logs include the verification URL
    assert any("portal.nousresearch.com/activate" in line for line in captured_logs)
    assert any("ABCD-EFGH" in line for line in captured_logs)


def test_plugin_manifest():
    manifest = json.loads(
        (_REPO / "extensions" / "nous-portal-provider" / "plugin.json").read_text()
    )
    assert manifest["entry"] == "plugin"
    setup = manifest["setup"]["providers"][0]
    assert setup["id"] == "nous-portal"
    assert setup["auth_methods"] == ["oauth_device_code"]


def test_appears_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        _discover_providers,
    )
    ids = {p["name"] for p in _discover_providers()}
    assert "nous-portal" in ids
