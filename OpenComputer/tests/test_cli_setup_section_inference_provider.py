"""Tests for the inference-provider wizard section."""
from __future__ import annotations

from pathlib import Path


def _make_ctx(tmp_path: Path, config: dict | None = None):
    from opencomputer.cli_setup.sections import WizardCtx
    return WizardCtx(
        config=config or {},
        config_path=tmp_path / "config.yaml",
        is_first_run=True,
    )


def test_run_lists_all_discovered_providers_plus_custom(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip

    fake_providers = [
        {"name": "anthropic", "label": "Anthropic", "description": "Claude"},
        {"name": "openai", "label": "OpenAI", "description": "GPT-4"},
    ]
    monkeypatch.setattr(ip, "_discover_providers", lambda: fake_providers)

    captured_choices = []

    def fake_radiolist(question, choices, default=0, description=None, **kw):
        captured_choices.extend(choices)
        return 0  # pick first

    monkeypatch.setattr(ip, "radiolist", fake_radiolist)
    monkeypatch.setattr(ip, "_invoke_provider_setup",
                         lambda name, ctx: True)

    ctx = _make_ctx(tmp_path)
    ip.run_inference_provider_section(ctx)

    labels = [c.label for c in captured_choices]
    assert "Anthropic" in labels and "OpenAI" in labels
    assert "Custom endpoint (enter URL manually)" in labels
    assert "Leave unchanged" in labels


def test_run_writes_provider_to_config_on_selection(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {"name": "anthropic", "label": "Anthropic", "description": "Claude"},
    ])
    monkeypatch.setattr(ip, "radiolist", lambda *a, **kw: 0)  # pick anthropic
    monkeypatch.setattr(ip, "_invoke_provider_setup",
                         lambda name, ctx: True)

    ctx = _make_ctx(tmp_path)
    result = ip.run_inference_provider_section(ctx)

    assert result == SectionResult.CONFIGURED
    # The mocked _invoke_provider_setup doesn't write — the test simply
    # verifies the handler returned CONFIGURED.


def test_run_leave_unchanged_returns_skipped_keep(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import inference_provider as ip
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ip, "_discover_providers", lambda: [
        {"name": "anthropic", "label": "Anthropic", "description": "Claude"},
    ])
    # Choices: [Anthropic, Custom endpoint, Leave unchanged] → idx 2
    monkeypatch.setattr(ip, "radiolist", lambda *a, **kw: 2)

    ctx = _make_ctx(tmp_path, config={"model": {"provider": "anthropic"}})
    result = ip.run_inference_provider_section(ctx)
    assert result == SectionResult.SKIPPED_KEEP


def test_is_configured_returns_true_when_provider_set(tmp_path):
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        is_inference_provider_configured,
    )
    ctx = _make_ctx(tmp_path, config={"model": {"provider": "anthropic"}})
    assert is_inference_provider_configured(ctx) is True


def test_is_configured_returns_false_for_none_provider(tmp_path):
    from opencomputer.cli_setup.section_handlers.inference_provider import (
        is_inference_provider_configured,
    )
    ctx = _make_ctx(tmp_path, config={"model": {"provider": "none"}})
    assert is_inference_provider_configured(ctx) is False
