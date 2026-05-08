"""Production-callsite verification for the T57-T71 follow-ups.

Closes the "ship modules with their callsite" gaps from the brutal
audit:
  - T61 — X-OC-Profile-Active response header surfaces the resolved
    profile on every api-server response.
  - T68 — complete_vision + complete_video share the fallback chain.
  - T69 — discover_anthropic_credential is consumed by AnthropicProvider
    when env + explicit args are both empty.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer


def _load_adapter_module():
    if "api_server_adapter_callsite_test" in sys.modules:
        return sys.modules["api_server_adapter_callsite_test"]
    spec_path = (
        Path(__file__).parent.parent / "extensions" / "api-server" / "adapter.py"
    )
    spec = importlib.util.spec_from_file_location(
        "api_server_adapter_callsite_test", spec_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_server_adapter_callsite_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── T61 production callsite: X-OC-Profile-Active response header ─────


@pytest.mark.asyncio
async def test_response_header_carries_resolved_profile():
    mod = _load_adapter_module()
    cfg = {"host": "127.0.0.1", "port": 0, "token": "tok"}
    adapter = mod.APIServerAdapter(cfg)

    async def handler(text, sid):
        return "ok"

    adapter.set_handler(handler)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.get(
            "/v1/capabilities", headers={"X-OC-Profile": "alice"}
        )
        assert r.status == 200
        assert r.headers["X-OC-Profile-Active"] == "alice"


@pytest.mark.asyncio
async def test_response_header_falls_back_to_env_profile(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "production")
    mod = _load_adapter_module()
    cfg = {"host": "127.0.0.1", "port": 0, "token": "tok"}
    adapter = mod.APIServerAdapter(cfg)
    app = adapter._build_app()
    async with TestClient(TestServer(app)) as client:
        # No X-OC-Profile header → falls back to env.
        r = await client.get("/v1/capabilities")
        assert r.headers["X-OC-Profile-Active"] == "production"


# ─── T68 production callsite: complete_vision/complete_video fallback ─


@pytest.mark.asyncio
async def test_complete_vision_walks_fallback(monkeypatch):
    from opencomputer.agent import aux_llm
    from plugin_sdk.core import Message
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    primary_calls = {"n": 0}

    class _Primary:
        async def complete(self, **kw):
            primary_calls["n"] += 1
            raise RuntimeError("rate limit exceeded")

    class _Backup:
        async def complete(self, **kw):
            return ProviderResponse(
                message=Message(role="assistant", content="from-backup"),
                usage=Usage(input_tokens=1, output_tokens=1),
                stop_reason="end_turn",
            )

    monkeypatch.setattr(aux_llm, "_resolve_provider", lambda: _Primary())
    monkeypatch.setattr(aux_llm, "_resolve_default_model", lambda: "x")
    monkeypatch.setattr(aux_llm, "_resolve_fallback_provider", lambda fp: _Backup())

    fake_cfg = MagicMock()
    fake_cfg.fallback_providers = (
        MagicMock(provider="p2", model="y", base_url=None, key_env=None),
    )
    monkeypatch.setattr(aux_llm, "default_config", lambda: fake_cfg)

    out = await aux_llm.complete_vision(
        image_base64="aGk=",
        mime_type="image/png",
        prompt="describe",
    )
    assert out == "from-backup"
    assert primary_calls["n"] == 1


@pytest.mark.asyncio
async def test_complete_video_walks_fallback(monkeypatch):
    from opencomputer.agent import aux_llm
    from plugin_sdk.core import Message
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    class _Primary:
        async def complete(self, **kw):
            raise RuntimeError("503 server error")

    class _Backup:
        async def complete(self, **kw):
            return ProviderResponse(
                message=Message(role="assistant", content="video-from-backup"),
                usage=Usage(input_tokens=1, output_tokens=1),
                stop_reason="end_turn",
            )

    monkeypatch.setattr(aux_llm, "_resolve_provider", lambda: _Primary())
    monkeypatch.setattr(aux_llm, "_resolve_default_model", lambda: "x")
    monkeypatch.setattr(aux_llm, "_resolve_fallback_provider", lambda fp: _Backup())

    fake_cfg = MagicMock()
    fake_cfg.fallback_providers = (
        MagicMock(provider="p2", model="y", base_url=None, key_env=None),
    )
    monkeypatch.setattr(aux_llm, "default_config", lambda: fake_cfg)

    out = await aux_llm.complete_video(
        video_base64="aGk=",
        mime_type="video/mp4",
        prompt="describe",
    )
    assert out == "video-from-backup"


# ─── T69 production callsite: AnthropicProvider uses discovery ────────


def test_anthropic_provider_uses_discovery(monkeypatch, tmp_path):
    """No env var + auth.json present → provider boots from discovery."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text(
        '{"anthropic": {"api_key": "sk-from-discovery"}}'
    )

    spec = importlib.util.spec_from_file_location(
        "anthropic_provider_callsite_test",
        Path(__file__).parent.parent
        / "extensions"
        / "anthropic-provider"
        / "provider.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["anthropic_provider_callsite_test"] = mod
    spec.loader.exec_module(mod)

    provider = mod.AnthropicProvider()
    assert provider._api_key == "sk-from-discovery"


def test_anthropic_provider_explicit_arg_wins_over_discovery(monkeypatch, tmp_path):
    """Explicit api_key arg short-circuits both env AND discovery."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text(
        '{"anthropic": {"api_key": "sk-from-disc"}}'
    )

    spec = importlib.util.spec_from_file_location(
        "anthropic_provider_callsite_test_2",
        Path(__file__).parent.parent
        / "extensions"
        / "anthropic-provider"
        / "provider.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["anthropic_provider_callsite_test_2"] = mod
    spec.loader.exec_module(mod)

    provider = mod.AnthropicProvider(api_key="sk-explicit")
    assert provider._api_key == "sk-explicit"


def test_anthropic_provider_no_creds_anywhere_raises(monkeypatch, tmp_path):
    """When env + auth.json + claude-code all empty → existing clear error."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    (tmp_path / "fake-home").mkdir()

    spec = importlib.util.spec_from_file_location(
        "anthropic_provider_callsite_test_3",
        Path(__file__).parent.parent
        / "extensions"
        / "anthropic-provider"
        / "provider.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["anthropic_provider_callsite_test_3"] = mod
    spec.loader.exec_module(mod)

    with pytest.raises(RuntimeError, match="Anthropic API key not set"):
        mod.AnthropicProvider()
