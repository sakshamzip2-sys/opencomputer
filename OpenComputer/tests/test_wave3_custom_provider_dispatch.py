"""Wave 3 — parse_custom_model_spec + build_custom_provider."""

from __future__ import annotations

import pytest

from opencomputer.agent.config import (
    Config,
    CustomProvider,
    default_config,
)
from opencomputer.agent.custom_provider_client import (
    build_custom_provider,
    parse_custom_model_spec,
)


def test_parse_simple_spec():
    name, model_id = parse_custom_model_spec("custom:groq:llama-3.3-70b-versatile")
    assert name == "groq"
    assert model_id == "llama-3.3-70b-versatile"


def test_parse_model_id_with_colon():
    """Ollama tag form qwen3.5:27b survives the parser."""
    name, model_id = parse_custom_model_spec("custom:local:qwen3.5:27b")
    assert name == "local"
    assert model_id == "qwen3.5:27b"


def test_parse_model_id_with_slash_and_colon():
    """OpenRouter-style provider/family/model survives + tags too."""
    name, model_id = parse_custom_model_spec(
        "custom:proxy:meta-llama/Llama-3.3-70B-Instruct:fastest"
    )
    assert name == "proxy"
    assert model_id == "meta-llama/Llama-3.3-70B-Instruct:fastest"


def test_parse_no_prefix_raises():
    with pytest.raises(ValueError, match="expected 'custom:"):
        parse_custom_model_spec("openrouter:anthropic/claude-sonnet-4")


def test_parse_missing_model_raises():
    with pytest.raises(ValueError, match="expected 'custom:"):
        parse_custom_model_spec("custom:local")


def test_parse_missing_name_raises():
    with pytest.raises(ValueError, match="expected 'custom:"):
        parse_custom_model_spec("custom::qwen")


def test_build_unknown_name_raises():
    cfg = default_config()
    with pytest.raises(ValueError, match="no custom_provider named 'missing'"):
        build_custom_provider("missing", cfg)


def test_build_lists_available_in_error_message():
    cfg = default_config()
    cfg = Config(
        **{
            **{
                f.name: getattr(cfg, f.name)
                for f in __import__("dataclasses").fields(cfg)
            },
            "custom_providers": (
                CustomProvider(name="alpha", base_url="http://a"),
                CustomProvider(name="beta", base_url="http://b"),
            ),
        }
    )
    with pytest.raises(ValueError, match="alpha, beta"):
        build_custom_provider("missing", cfg)


def test_build_with_inline_api_key(monkeypatch):
    """Inline api_key is used as-is, no env lookup."""
    monkeypatch.setenv("DUMMY_PROVIDER", "1")  # placeholder; we're just resolving the key
    from opencomputer.agent.custom_provider_client import _resolve_api_key

    cp = CustomProvider(name="x", base_url="http://x", api_key="inline-key")
    assert _resolve_api_key(cp) == "inline-key"


def test_build_with_key_env(monkeypatch):
    monkeypatch.setenv("MY_KEY_VAR", "env-resolved-key")
    from opencomputer.agent.custom_provider_client import _resolve_api_key

    cp = CustomProvider(name="x", base_url="http://x", key_env="MY_KEY_VAR")
    assert _resolve_api_key(cp) == "env-resolved-key"


def test_build_missing_key_env_returns_empty(monkeypatch, caplog):
    monkeypatch.delenv("ABSENT_VAR", raising=False)
    from opencomputer.agent.custom_provider_client import _resolve_api_key

    cp = CustomProvider(name="x", base_url="http://x", key_env="ABSENT_VAR")
    result = _resolve_api_key(cp)
    assert result == ""
    assert any("ABSENT_VAR" in r.message for r in caplog.records)
