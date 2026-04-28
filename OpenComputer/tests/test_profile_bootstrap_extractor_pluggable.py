"""Pluggable Layer 3 extractor (2026-04-28).

Pre-2026-04-28 the Layer 3 extractor was hard-bound to Ollama. This
suite locks in:

- Protocol conformance for all 3 implementations
- Factory selection by config.deepening.extractor
- Backend-availability semantics (raises on missing, returns blank on
  per-call failure)
- Privacy banner fires once per backend per profile, not on Ollama
- Back-compat: OllamaUnavailableError is still importable as alias
- Back-compat: extract_artifact() free function still works
- Cost-estimate helper produces sensible USD figures
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.profile_bootstrap.llm_extractor import (
    AnthropicArtifactExtractor,
    ArtifactExtraction,
    ArtifactExtractor,
    ExtractorUnavailableError,
    OllamaArtifactExtractor,
    OllamaUnavailableError,
    OpenAIArtifactExtractor,
    _estimate_cost_usd,
    extract_artifact,
    get_extractor,
)

# ── Protocol conformance ──────────────────────────────────────────────


def test_all_three_extractors_satisfy_protocol():
    assert isinstance(OllamaArtifactExtractor(), ArtifactExtractor)
    assert isinstance(AnthropicArtifactExtractor(), ArtifactExtractor)
    assert isinstance(OpenAIArtifactExtractor(), ArtifactExtractor)


def test_ollama_unavailable_error_is_back_compat_alias():
    assert OllamaUnavailableError is ExtractorUnavailableError


# ── Ollama (existing semantic preserved) ─────────────────────────────


def test_ollama_extract_raises_when_binary_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(ExtractorUnavailableError):
        OllamaArtifactExtractor().extract("hello")


def test_extract_artifact_free_function_still_works(monkeypatch):
    """Back-compat: callers using the free function path keep working."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(OllamaUnavailableError):
        extract_artifact("hello")


def test_ollama_default_model_constant_preserved():
    """External code may import _DEFAULT_MODEL — keep it stable."""
    from opencomputer.profile_bootstrap.llm_extractor import _DEFAULT_MODEL
    assert _DEFAULT_MODEL == "llama3.2:3b"
    assert OllamaArtifactExtractor()._DEFAULT_MODEL == _DEFAULT_MODEL


# ── Anthropic / OpenAI availability ───────────────────────────────────


def test_anthropic_unavailable_when_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert AnthropicArtifactExtractor().is_available() is False
    with pytest.raises(ExtractorUnavailableError):
        AnthropicArtifactExtractor().extract("hello")


def test_anthropic_available_when_key_in_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert AnthropicArtifactExtractor().is_available() is True


def test_anthropic_available_when_key_passed_explicit():
    assert AnthropicArtifactExtractor(api_key="sk-test").is_available() is True


def test_openai_unavailable_when_no_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert OpenAIArtifactExtractor().is_available() is False
    with pytest.raises(ExtractorUnavailableError):
        OpenAIArtifactExtractor().extract("hello")


# ── factory: validation + selection ───────────────────────────────────


def _make_config(extractor: str = "ollama", model: str = "", **kw):
    """Minimal Config-shape stub — only the fields the factory reads."""
    cfg = MagicMock()
    cfg.deepening.extractor = extractor
    cfg.deepening.model = model
    cfg.deepening.timeout_seconds = 15.0
    cfg.deepening.daily_cost_cap_usd = 0.50
    return cfg


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError, match="not in"):
        get_extractor(_make_config(extractor="gemini"))


def test_factory_returns_ollama_for_default():
    inst = get_extractor(_make_config(extractor="ollama"))
    assert isinstance(inst, OllamaArtifactExtractor)


def test_factory_returns_anthropic_when_selected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    inst = get_extractor(_make_config(extractor="anthropic"))
    assert isinstance(inst, AnthropicArtifactExtractor)


def test_factory_returns_openai_when_selected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    inst = get_extractor(_make_config(extractor="openai"))
    assert isinstance(inst, OpenAIArtifactExtractor)


def test_factory_passes_model_override(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    inst = get_extractor(_make_config(extractor="anthropic", model="claude-opus-4-7"))
    assert inst.model == "claude-opus-4-7"


def test_factory_uses_class_default_when_model_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    inst = get_extractor(_make_config(extractor="anthropic"))
    assert inst.model == AnthropicArtifactExtractor._DEFAULT_MODEL


# ── privacy banner: prints once per backend per profile ───────────────


def test_privacy_banner_writes_marker_on_first_anthropic_use(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    get_extractor(_make_config(extractor="anthropic"))
    err = capsys.readouterr().err
    assert "Layer 3 deepening is now using anthropic" in err
    assert (tmp_path / "deepening_consent_anthropic.acknowledged").exists()


def test_privacy_banner_silent_on_subsequent_use(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    get_extractor(_make_config(extractor="anthropic"))
    capsys.readouterr()  # drain first banner
    get_extractor(_make_config(extractor="anthropic"))  # second call
    err = capsys.readouterr().err
    assert err == ""


def test_privacy_banner_per_backend(monkeypatch, tmp_path, capsys):
    """Switching anthropic→openai prints a fresh banner; doesn't reuse the ack."""
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    get_extractor(_make_config(extractor="anthropic"))
    capsys.readouterr()

    get_extractor(_make_config(extractor="openai"))
    err = capsys.readouterr().err
    assert "openai" in err


def test_privacy_banner_not_printed_for_ollama(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    get_extractor(_make_config(extractor="ollama"))
    err = capsys.readouterr().err
    assert err == ""
    assert not (tmp_path / "deepening_consent_ollama.acknowledged").exists()


# ── End-to-end: Anthropic backend with mocked SDK ─────────────────────


def test_anthropic_extract_with_mocked_sdk(monkeypatch):
    """Mock the anthropic SDK so the test never makes a real API call."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    fake_response = MagicMock()
    fake_response.content = [
        MagicMock(text='{"topic":"agent design","people":[],"intent":"build","sentiment":"positive","timestamp":""}'),
    ]
    fake_response.usage = MagicMock(input_tokens=100, output_tokens=50)

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    fake_module = MagicMock()
    fake_module.Anthropic.return_value = fake_client

    with patch.dict("sys.modules", {"anthropic": fake_module}):
        result = AnthropicArtifactExtractor().extract("a long artifact")

    assert result.topic == "agent design"
    assert result.intent == "build"
    assert result.sentiment == "positive"
    fake_client.messages.create.assert_called_once()


def test_openai_extract_with_mocked_sdk(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_choice = MagicMock()
    fake_choice.message.content = (
        '{"topic":"meeting","people":["alice"],"intent":"plan","sentiment":"neutral","timestamp":""}'
    )
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]
    fake_response.usage = MagicMock(prompt_tokens=80, completion_tokens=40)

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_response

    fake_module = MagicMock()
    fake_module.OpenAI.return_value = fake_client

    with patch.dict("sys.modules", {"openai": fake_module}):
        result = OpenAIArtifactExtractor().extract("standup notes")

    assert result.topic == "meeting"
    assert result.people == ("alice",)
    assert result.sentiment == "neutral"


def test_anthropic_returns_blank_on_sdk_exception(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_module = MagicMock()
    fake_module.Anthropic.side_effect = RuntimeError("network down")
    with patch.dict("sys.modules", {"anthropic": fake_module}):
        result = AnthropicArtifactExtractor().extract("hello")
    assert result == ArtifactExtraction()


def test_anthropic_passes_base_url_when_proxy_env_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://router.example/api")

    fake_response = MagicMock()
    fake_response.content = [MagicMock(text='{"topic":"x","people":[],"intent":"","sentiment":"unknown","timestamp":""}')]
    fake_response.usage = MagicMock(input_tokens=10, output_tokens=5)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    fake_module = MagicMock()
    fake_module.Anthropic.return_value = fake_client

    with patch.dict("sys.modules", {"anthropic": fake_module}):
        AnthropicArtifactExtractor().extract("x")

    kwargs = fake_module.Anthropic.call_args.kwargs
    assert kwargs.get("base_url") == "https://router.example/api"


# ── Cost estimator ────────────────────────────────────────────────────


def test_cost_estimate_anthropic_haiku():
    # 1000 input + 200 output @ $0.80 / $4.00 per Mtok
    cost = _estimate_cost_usd("anthropic", "claude-haiku-4-5-20251001", 1000, 200)
    assert cost == pytest.approx((1000 / 1e6) * 0.80 + (200 / 1e6) * 4.00, rel=1e-6)


def test_cost_estimate_openai_4o_mini():
    cost = _estimate_cost_usd("openai", "gpt-4o-mini", 1000, 200)
    assert cost == pytest.approx((1000 / 1e6) * 0.15 + (200 / 1e6) * 0.60, rel=1e-6)


def test_cost_estimate_unknown_model_uses_fallback():
    """Unknown model still records non-zero so cost guard sees something."""
    cost = _estimate_cost_usd("anthropic", "claude-future-9000", 1000, 200)
    assert cost > 0


# ── DeepeningConfig wiring ────────────────────────────────────────────


def test_default_config_has_deepening_block():
    from opencomputer.agent.config import DeepeningConfig, default_config
    cfg = default_config()
    assert isinstance(cfg.deepening, DeepeningConfig)
    assert cfg.deepening.extractor == "ollama"
    assert cfg.deepening.daily_cost_cap_usd == 0.50


def test_yaml_load_overrides_extractor(tmp_path):
    from opencomputer.agent.config_store import load_config
    p = tmp_path / "config.yaml"
    p.write_text("deepening:\n  extractor: anthropic\n  daily_cost_cap_usd: 1.5\n")
    cfg = load_config(p)
    assert cfg.deepening.extractor == "anthropic"
    assert cfg.deepening.daily_cost_cap_usd == 1.5
