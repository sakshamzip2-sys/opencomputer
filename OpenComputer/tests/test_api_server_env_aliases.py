"""Hermes parity G6+G7: API_SERVER_KEY + API_SERVER_ENABLED env aliases."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_plugin():
    sys.modules.pop("api_server_plugin_test", None)
    spec_path = Path(__file__).parent.parent / "extensions" / "api-server" / "plugin.py"
    spec = importlib.util.spec_from_file_location("api_server_plugin_test", spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_server_plugin_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_api_server_key_resolves_token(monkeypatch):
    """G6: When only API_SERVER_KEY is set, plugin uses it as the bearer token."""
    monkeypatch.delenv("API_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("API_SERVER_KEY", "spec-key-abc")
    mod = _load_plugin()
    cfg = mod._resolve_api_server_config()
    assert cfg["token"] == "spec-key-abc"


def test_api_server_token_takes_precedence_over_key(monkeypatch):
    """When both set, OC's _TOKEN wins (existing users keep working)."""
    monkeypatch.setenv("API_SERVER_TOKEN", "oc-token")
    monkeypatch.setenv("API_SERVER_KEY", "spec-key")
    mod = _load_plugin()
    cfg = mod._resolve_api_server_config()
    assert cfg["token"] == "oc-token"


def test_neither_env_set_means_empty_token(monkeypatch):
    monkeypatch.delenv("API_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    mod = _load_plugin()
    cfg = mod._resolve_api_server_config()
    assert cfg["token"] == ""


def test_api_server_enabled_true_means_enabled(monkeypatch):
    """G7: API_SERVER_ENABLED=true reports enabled."""
    monkeypatch.setenv("API_SERVER_ENABLED", "true")
    mod = _load_plugin()
    assert mod._is_api_server_enabled() is True


def test_api_server_enabled_false_means_disabled(monkeypatch):
    monkeypatch.setenv("API_SERVER_ENABLED", "false")
    mod = _load_plugin()
    assert mod._is_api_server_enabled() is False


def test_api_server_enabled_unset_returns_none(monkeypatch):
    monkeypatch.delenv("API_SERVER_ENABLED", raising=False)
    mod = _load_plugin()
    assert mod._is_api_server_enabled() is None


def test_api_server_enabled_accepts_aliases(monkeypatch):
    for truthy in ("1", "yes", "on", "TRUE"):
        monkeypatch.setenv("API_SERVER_ENABLED", truthy)
        mod = _load_plugin()
        assert mod._is_api_server_enabled() is True, f"failed for {truthy!r}"
    for falsy in ("0", "no", "off", "FALSE"):
        monkeypatch.setenv("API_SERVER_ENABLED", falsy)
        mod = _load_plugin()
        assert mod._is_api_server_enabled() is False, f"failed for {falsy!r}"
