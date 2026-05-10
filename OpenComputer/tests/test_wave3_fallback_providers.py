"""Wave 3 — FallbackProvider dataclass + cross-provider router + oc fallback CLI."""

from __future__ import annotations

import asyncio

import pytest
import yaml
from typer.testing import CliRunner

from opencomputer.agent.config import (
    Config,
    FallbackProvider,
    default_config,
)
from opencomputer.agent.fallback import (
    call_with_provider_fallback,
    is_transient_error,
)
from opencomputer.cli import app

# ─── FallbackProvider dataclass ─────────────────────────────────


def test_fallback_provider_default():
    fp = FallbackProvider()
    assert fp.provider == ""
    assert fp.model == ""
    assert fp.base_url is None
    assert fp.key_env is None


def test_fallback_provider_construction():
    fp = FallbackProvider(
        provider="openrouter", model="anthropic/claude-sonnet-4"
    )
    assert fp.provider == "openrouter"
    assert fp.model == "anthropic/claude-sonnet-4"


def test_fallback_provider_yaml_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
fallback_providers:
  - provider: openrouter
    model: anthropic/claude-sonnet-4
  - provider: nous
    model: nous-hermes-3
  - provider: custom:local
    model: qwen3.5:27b
""",
        encoding="utf-8",
    )
    from opencomputer.agent.config_store import load_config

    cfg = load_config(cfg_path)
    assert len(cfg.fallback_providers) == 3
    assert cfg.fallback_providers[0].provider == "openrouter"
    assert cfg.fallback_providers[2].provider == "custom:local"
    assert cfg.fallback_providers[2].model == "qwen3.5:27b"


def test_default_config_has_empty_fallback_providers():
    cfg = default_config()
    assert cfg.fallback_providers == ()


# ─── cross-provider router ──────────────────────────────────────


def _make_provider(name: str = "fake"):
    """Tiny stub provider with a name attribute."""

    class _P:
        pass

    p = _P()
    p.name = name
    return p


def test_call_with_provider_fallback_primary_succeeds():
    async def primary(model: str):
        return f"primary:{model}"

    async def cross(prov, model: str):
        raise AssertionError("should not be called")

    result = asyncio.run(
        call_with_provider_fallback(
            primary, cross,
            primary_model="m1",
            fallback_models=("m2",),
            provider_chain=((_make_provider("p2"), "m2"),),
        )
    )
    assert result == "primary:m1"


def test_call_with_provider_fallback_falls_through_models_then_provider():
    calls: list[str] = []

    async def primary(model: str):
        calls.append(f"primary:{model}")
        raise RuntimeError("rate_limit hit")

    async def cross(prov, model: str):
        calls.append(f"cross:{prov.name}:{model}")
        return f"recovered:{prov.name}:{model}"

    result = asyncio.run(
        call_with_provider_fallback(
            primary, cross,
            primary_model="m1",
            fallback_models=("m2",),
            provider_chain=(
                (_make_provider("p2"), "alt-m"),
            ),
        )
    )
    assert result == "recovered:p2:alt-m"
    assert calls == [
        "primary:m1",
        "primary:m2",          # fallback_models
        "cross:p2:alt-m",      # cross-provider
    ]


def test_call_with_provider_fallback_non_transient_short_circuits():
    """Auth errors (non-transient) should NOT walk the chain."""

    async def primary(model: str):
        raise RuntimeError("invalid_api_key")

    async def cross(prov, model: str):
        raise AssertionError("should not be called — non-transient")

    with pytest.raises(RuntimeError, match="invalid_api_key"):
        asyncio.run(
            call_with_provider_fallback(
                primary, cross,
                primary_model="m1",
                fallback_models=(),
                provider_chain=((_make_provider("p2"), "m2"),),
            )
        )


def test_call_with_provider_fallback_exhausted_chain_raises_last():
    async def primary(model: str):
        raise RuntimeError("connection refused (primary)")

    async def cross(prov, model: str):
        raise RuntimeError("rate_limit (cross)")

    with pytest.raises(RuntimeError, match="rate_limit \\(cross\\)"):
        asyncio.run(
            call_with_provider_fallback(
                primary, cross,
                primary_model="m1",
                fallback_models=(),
                provider_chain=((_make_provider("p2"), "m2"),),
            )
        )


def test_is_transient_error_recognizes_rate_limit():
    assert is_transient_error(RuntimeError("HTTP 429 rate_limit reached"))


def test_is_transient_error_rejects_auth():
    assert not is_transient_error(RuntimeError("invalid_api_key"))


# ─── oc fallback CLI ────────────────────────────────────────────


def test_fallback_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["fallback"])
    assert result.exit_code == 0
    assert "no fallback_providers configured" in result.output


def test_fallback_add_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(
        app, ["fallback", "add", "openrouter/anthropic/claude-sonnet-4"]
    )
    assert result.exit_code == 0, result.output
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["fallback_providers"][0]["provider"] == "openrouter"
    assert cfg["fallback_providers"][0]["model"] == "anthropic/claude-sonnet-4"
    list_result = runner.invoke(app, ["fallback"])
    assert "openrouter/anthropic/claude-sonnet-4" in list_result.output


def test_fallback_add_custom_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(
        app, ["fallback", "add", "custom:local/qwen3.5:27b"]
    )
    assert result.exit_code == 0, result.output
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["fallback_providers"][0]["provider"] == "custom:local"
    assert cfg["fallback_providers"][0]["model"] == "qwen3.5:27b"


def test_fallback_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(app, ["fallback", "add", "a/m1"])
    runner.invoke(app, ["fallback", "add", "b/m2"])
    result = runner.invoke(app, ["fallback", "remove", "0"])
    assert result.exit_code == 0, result.output
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert len(cfg["fallback_providers"]) == 1
    assert cfg["fallback_providers"][0]["provider"] == "b"


def test_fallback_remove_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(app, ["fallback", "add", "a/m1"])
    result = runner.invoke(app, ["fallback", "remove", "99"])
    assert result.exit_code == 1
    assert "out of range" in result.output


def test_fallback_move(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(app, ["fallback", "add", "a/m1"])
    runner.invoke(app, ["fallback", "add", "b/m2"])
    runner.invoke(app, ["fallback", "add", "c/m3"])
    result = runner.invoke(app, ["fallback", "move", "0", "2"])
    assert result.exit_code == 0, result.output
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    order = [p["provider"] for p in cfg["fallback_providers"]]
    assert order == ["b", "c", "a"]


def test_fallback_move_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(app, ["fallback", "add", "a/m1"])
    result = runner.invoke(app, ["fallback", "move", "5", "0"])
    assert result.exit_code == 1
    assert "out of range" in result.output


def test_fallback_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(app, ["fallback", "add", "a/m1"])
    runner.invoke(app, ["fallback", "add", "b/m2"])
    result = runner.invoke(app, ["fallback", "clear"])
    assert result.exit_code == 0, result.output
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["fallback_providers"] == []


def test_fallback_add_invalid_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["fallback", "add", "no-slash-here"])
    # Invalid spec → typer.BadParameter → exit code 2
    assert result.exit_code != 0
