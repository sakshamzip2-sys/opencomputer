"""Tests for the TTS provider wizard section (S2)."""
from __future__ import annotations

from pathlib import Path


def _make_ctx(tmp_path: Path, config: dict | None = None):
    from opencomputer.cli_setup.sections import WizardCtx
    return WizardCtx(
        config=config or {},
        config_path=tmp_path / "config.yaml",
        is_first_run=True,
    )


def test_apply_default_writes_tts_provider(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import tts_provider as tts
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(tts, "radiolist", lambda *a, **kw: 0)  # Apply

    ctx = _make_ctx(tmp_path)
    result = tts.run_tts_provider_section(ctx)

    assert result == SectionResult.CONFIGURED
    assert ctx.config["tts"]["provider"] == "openai-tts"
    assert ctx.config["tts"]["voice"] == "alloy"


def test_skip_keeps_tts_config_untouched(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import tts_provider as tts
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(tts, "radiolist", lambda *a, **kw: 1)  # Skip

    existing = {"tts": {"provider": "elevenlabs", "voice": "Rachel"}}
    ctx = _make_ctx(tmp_path, config=existing)
    result = tts.run_tts_provider_section(ctx)

    assert result == SectionResult.SKIPPED_FRESH
    assert ctx.config["tts"]["provider"] == "elevenlabs"


def test_apply_does_not_clobber_unrelated_tts_keys(monkeypatch, tmp_path):
    """User-set tts.speed should survive an apply-defaults pass."""
    from opencomputer.cli_setup.section_handlers import tts_provider as tts

    monkeypatch.setattr(tts, "radiolist", lambda *a, **kw: 0)

    ctx = _make_ctx(tmp_path, config={"tts": {"speed": 1.25}})
    tts.run_tts_provider_section(ctx)

    assert ctx.config["tts"]["speed"] == 1.25, "user-set keys preserved"
    assert ctx.config["tts"]["provider"] == "openai-tts"


def test_section_registry_uses_live_tts_handler():
    from opencomputer.cli_setup.sections import SECTION_REGISTRY

    sec = next(s for s in SECTION_REGISTRY if s.key == "tts_provider")
    assert sec.deferred is False, "tts_provider is now LIVE (S2)"
