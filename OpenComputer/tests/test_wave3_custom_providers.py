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
    """The YAML auto-parser delivers nested dicts; __post_init__ converts.

    Asserts on duck-type fields rather than ``isinstance`` because in
    CI's full-suite pytest ordering this module gets reloaded and the
    class identity drifts (see __post_init__ comment).
    """
    p = CustomProvider(
        name="local",
        base_url="http://x",
        models={"m1": {"context_length": 4096}},
    )
    m1 = p.models["m1"]
    assert hasattr(m1, "context_length")
    assert hasattr(m1, "timeout_seconds")
    assert m1.context_length == 4096
    assert m1.timeout_seconds is None


def test_invalid_api_mode_raises():
    with pytest.raises(ValueError, match="api_mode must be one of"):
        CustomProvider(name="x", base_url="http://x", api_mode="bogus")


def test_zero_timeout_raises():
    with pytest.raises(ValueError, match="request_timeout_seconds must be > 0"):
        CustomProvider(name="x", base_url="http://x", request_timeout_seconds=0.0)


def test_negative_timeout_raises():
    with pytest.raises(ValueError, match="request_timeout_seconds must be > 0"):
        CustomProvider(name="x", base_url="http://x", request_timeout_seconds=-1.0)


def test_models_value_non_dict_passes_through():
    """Duck-typing: __post_init__ only converts dicts; everything else
    passes through unchanged. Bad shapes are caught later at access time
    by the field-access in CompactionEngine etc.
    """
    cp = CustomProvider(name="x", base_url="http://x", models={"m1": 123})  # type: ignore[arg-type]
    assert cp.models["m1"] == 123  # untouched


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


def test_context_window_with_overrides_honors_per_model_setting():
    from opencomputer.agent.compaction import context_window_with_overrides

    cp = CustomProvider(
        name="local",
        base_url="http://x",
        models={"qwen": CustomProviderModelOverride(context_length=8192)},
    )
    # With the override, returns 8192.
    assert context_window_with_overrides("qwen", (cp,)) == 8192
    # Without a matching override, falls through to the standard chain.
    assert context_window_with_overrides("not-in-overrides", (cp,)) > 0


def test_context_window_with_overrides_no_custom_providers():
    from opencomputer.agent.compaction import (
        context_window_for,
        context_window_with_overrides,
    )

    # Empty custom_providers → identical to context_window_for.
    assert (
        context_window_with_overrides("claude-sonnet-4-6", ())
        == context_window_for("claude-sonnet-4-6")
    )


def test_save_config_emits_provider_routing(tmp_path, monkeypatch):
    """Round-trip preservation — provider_routing must survive save→load."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent.config import ProviderRoutingConfig
    from opencomputer.agent.config_store import load_config, save_config

    cfg = default_config()
    cfg = type(cfg)(
        **{
            **{f.name: getattr(cfg, f.name) for f in __import__("dataclasses").fields(cfg)},
            "provider_routing": ProviderRoutingConfig(
                sort="price",
                only=("Anthropic",),
                data_collection="deny",
            ),
        }
    )
    path = save_config(cfg, tmp_path / "config.yaml")
    cfg2 = load_config(path)
    assert cfg2.provider_routing.sort == "price"
    assert cfg2.provider_routing.only == ("Anthropic",)
    assert cfg2.provider_routing.data_collection == "deny"


def test_save_config_emits_fallback_providers(tmp_path, monkeypatch):
    """Round-trip preservation — fallback_providers must survive save→load."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent.config import FallbackProvider
    from opencomputer.agent.config_store import load_config, save_config

    cfg = default_config()
    cfg = type(cfg)(
        **{
            **{f.name: getattr(cfg, f.name) for f in __import__("dataclasses").fields(cfg)},
            "fallback_providers": (
                FallbackProvider(provider="openrouter", model="anthropic/claude-sonnet-4"),
                FallbackProvider(provider="custom:local", model="qwen3.5:27b"),
            ),
        }
    )
    path = save_config(cfg, tmp_path / "config.yaml")
    cfg2 = load_config(path)
    assert len(cfg2.fallback_providers) == 2
    assert cfg2.fallback_providers[0].provider == "openrouter"
    assert cfg2.fallback_providers[1].provider == "custom:local"


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
