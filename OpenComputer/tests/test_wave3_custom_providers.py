"""Wave 3 — CustomProvider dataclass + config-schema parsing."""

from __future__ import annotations

import pytest

from opencomputer.agent.config import (
    CustomProvider,
    CustomProviderModelOverride,
    default_config,
)


def test_minimum_fields():
    p = CustomProvider(name="local", base_url="http://localhost:8080/v1")
    assert p.name == "local"
    assert p.base_url == "http://localhost:8080/v1"
    assert p.api_key is None
    assert p.key_env is None
    assert p.api_mode == "auto"
    assert p.request_timeout_seconds == 60.0
    assert p.models == {}


def test_default_construction_is_legal():
    """Auto-parser builds a default instance before applying overrides."""
    p = CustomProvider()
    assert p.name == ""
    assert p.base_url == ""


def test_with_models_dict():
    p = CustomProvider(
        name="local",
        base_url="http://localhost:11434/v1",
        models={
            "qwen3.5:27b": CustomProviderModelOverride(
                context_length=32768, timeout_seconds=180.0
            ),
        },
    )
    assert p.models["qwen3.5:27b"].context_length == 32768
    assert p.models["qwen3.5:27b"].timeout_seconds == 180.0


def test_models_dict_auto_converts_from_yaml_dict():
    """The YAML auto-parser delivers nested dicts; __post_init__ converts."""
    p = CustomProvider(
        name="local",
        base_url="http://x",
        models={"m1": {"context_length": 4096}},
    )
    assert isinstance(p.models["m1"], CustomProviderModelOverride)
    assert p.models["m1"].context_length == 4096
    assert p.models["m1"].timeout_seconds is None


def test_invalid_api_mode_raises():
    with pytest.raises(ValueError, match="api_mode must be one of"):
        CustomProvider(name="x", base_url="http://x", api_mode="bogus")


def test_zero_timeout_raises():
    with pytest.raises(ValueError, match="request_timeout_seconds must be > 0"):
        CustomProvider(name="x", base_url="http://x", request_timeout_seconds=0.0)


def test_negative_timeout_raises():
    with pytest.raises(ValueError, match="request_timeout_seconds must be > 0"):
        CustomProvider(name="x", base_url="http://x", request_timeout_seconds=-1.0)


def test_models_value_wrong_type_raises():
    with pytest.raises(TypeError, match="must be a dict or CustomProviderModelOverride"):
        CustomProvider(name="x", base_url="http://x", models={"m1": 123})  # type: ignore[arg-type]


def test_default_config_has_empty_custom_providers():
    cfg = default_config()
    assert cfg.custom_providers == ()


def test_load_config_roundtrip_with_custom_providers(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
custom_providers:
  - name: local
    base_url: http://localhost:11434/v1
    key_env: OLLAMA_KEY
    api_mode: openai
    request_timeout_seconds: 120.0
    models:
      qwen3.5_27b:
        context_length: 32768
        timeout_seconds: 180.0
  - name: groq
    base_url: https://api.groq.com/openai/v1
    key_env: GROQ_API_KEY
""",
        encoding="utf-8",
    )
    from opencomputer.agent.config_store import load_config

    cfg = load_config(cfg_path)
    assert len(cfg.custom_providers) == 2

    local = cfg.custom_providers[0]
    assert local.name == "local"
    assert local.base_url == "http://localhost:11434/v1"
    assert local.key_env == "OLLAMA_KEY"
    assert local.api_mode == "openai"
    assert local.request_timeout_seconds == 120.0
    assert local.models["qwen3.5_27b"].context_length == 32768
    assert local.models["qwen3.5_27b"].timeout_seconds == 180.0

    groq = cfg.custom_providers[1]
    assert groq.name == "groq"
    assert groq.api_mode == "auto"  # default
    assert groq.request_timeout_seconds == 60.0  # default
    assert groq.models == {}


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent.config_store import load_config, save_config

    cfg = default_config()
    # Build a config with a custom_provider and save it
    cfg = type(cfg)(
        **{
            **{f.name: getattr(cfg, f.name) for f in __import__("dataclasses").fields(cfg)},
            "custom_providers": (
                CustomProvider(
                    name="local",
                    base_url="http://x:8080/v1",
                    models={"m1": CustomProviderModelOverride(context_length=8192)},
                ),
            ),
        }
    )
    path = save_config(cfg, tmp_path / "config.yaml")
    cfg2 = load_config(path)
    assert len(cfg2.custom_providers) == 1
    cp = cfg2.custom_providers[0]
    assert cp.name == "local"
    assert cp.base_url == "http://x:8080/v1"
    assert cp.models["m1"].context_length == 8192
