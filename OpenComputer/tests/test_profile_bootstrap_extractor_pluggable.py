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
    _maybe_offer_extractor_setup,
    _smart_fallback_candidate,
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


# ── Smart fallback: Ollama-missing + cloud-key-present ───────────────


def test_smart_fallback_candidate_prefers_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    assert _smart_fallback_candidate() == ("anthropic", "ANTHROPIC_API_KEY")


def test_smart_fallback_candidate_falls_back_to_openai(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    assert _smart_fallback_candidate() == ("openai", "OPENAI_API_KEY")


def test_smart_fallback_candidate_returns_none_when_no_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _smart_fallback_candidate() is None


def test_offer_skipped_when_stderr_not_tty(monkeypatch, tmp_path):
    """CI / non-interactive runs must NEVER prompt — no stdin to answer."""
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    assert _maybe_offer_extractor_setup() is None


def test_offer_skipped_when_marker_exists(monkeypatch, tmp_path):
    """Once the user has answered, never prompt again."""
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    (tmp_path / "extractor_setup.json").write_text('{"answer": "n"}')
    assert _maybe_offer_extractor_setup() is None


def test_offer_skipped_when_no_api_key_in_env(monkeypatch, tmp_path):
    """No fallback target → no offer."""
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    assert _maybe_offer_extractor_setup() is None


def test_offer_accepted_persists_config_and_marker(monkeypatch, tmp_path, capsys):
    """Yes → config.yaml updated, marker written, privacy ack pre-written."""
    # Point both _home and the config-file path at our tmp dir.
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "opencomputer.agent.config_store.config_file_path", lambda: config_path,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "y")

    chosen = _maybe_offer_extractor_setup()
    assert chosen == "anthropic"

    # Marker written
    marker = tmp_path / "extractor_setup.json"
    assert marker.exists()
    import json
    payload = json.loads(marker.read_text())
    assert payload["answer"] == "y"
    assert payload["backend_offered"] == "anthropic"

    # Privacy banner ack pre-written so the standard banner doesn't fire
    assert (tmp_path / "deepening_consent_anthropic.acknowledged").exists()

    # config.yaml has the new value
    from opencomputer.agent.config_store import load_config
    loaded = load_config(config_path)
    assert loaded.deepening.extractor == "anthropic"


def test_offer_declined_returns_none_and_writes_marker(
    monkeypatch, tmp_path, capsys,
):
    """No → marker written so we don't pester; returns None so caller falls
    through to Ollama (which then raises on extract)."""
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")

    assert _maybe_offer_extractor_setup() is None
    assert (tmp_path / "extractor_setup.json").exists()
    # Privacy ack must NOT be pre-written for a declined backend
    assert not (tmp_path / "deepening_consent_anthropic.acknowledged").exists()


def test_offer_eof_treated_as_decline(monkeypatch, tmp_path):
    """Ctrl-D / piped-stdin-empty must not crash; treat as 'no'."""
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    def _eof():
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)

    assert _maybe_offer_extractor_setup() is None
    assert (tmp_path / "extractor_setup.json").exists()


def test_factory_triggers_smart_fallback_when_ollama_missing(
    monkeypatch, tmp_path, capsys,
):
    """End-to-end through the factory: Ollama default + Ollama unavailable
    + Anthropic key + tty → factory returns AnthropicArtifactExtractor."""
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "opencomputer.agent.config_store.config_file_path", lambda: config_path,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "y")
    # Make Ollama "not available" by stubbing the module-level helper
    monkeypatch.setattr(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        lambda: False,
    )

    inst = get_extractor(_make_config(extractor="ollama"))
    assert isinstance(inst, AnthropicArtifactExtractor)


def test_factory_does_not_trigger_fallback_when_user_explicit(
    monkeypatch, tmp_path,
):
    """If the user explicitly set extractor: anthropic, never run the
    smart-fallback path — that path is exclusively for the
    'ollama default + Ollama missing' case."""
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)

    # If smart fallback ran, it'd write a marker. We shouldn't see one.
    inst = get_extractor(_make_config(extractor="anthropic"))
    assert isinstance(inst, AnthropicArtifactExtractor)
    assert not (tmp_path / "extractor_setup.json").exists()


def test_factory_does_not_trigger_fallback_when_ollama_present(
    monkeypatch, tmp_path,
):
    """If Ollama IS installed, the user picked the default for a reason —
    don't second-guess."""
    monkeypatch.setattr(
        "opencomputer.agent.config._home", lambda: tmp_path,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        lambda: True,
    )

    inst = get_extractor(_make_config(extractor="ollama"))
    assert isinstance(inst, OllamaArtifactExtractor)
    assert not (tmp_path / "extractor_setup.json").exists()
