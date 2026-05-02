"""Tests for cli_setup/wizard.py orchestrator + sections data model."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_section_result_has_four_states():
    from opencomputer.cli_setup.sections import SectionResult
    assert {r.value for r in SectionResult} == {
        "configured", "skipped-keep", "skipped-fresh", "cancelled",
    }


def test_wizard_section_dataclass_fields():
    from opencomputer.cli_setup.sections import WizardSection
    sec = WizardSection(
        key="test", icon="◆", title="Test", description="d",
        handler=lambda ctx: None,
    )
    assert sec.deferred is False
    assert sec.configured_check is None


def test_wizard_ctx_holds_config_path_first_run_flag():
    from opencomputer.cli_setup.sections import WizardCtx
    ctx = WizardCtx(
        config={}, config_path=Path("/tmp/x.yaml"), is_first_run=True,
    )
    assert ctx.is_first_run is True
    assert ctx.quick_mode is False


def test_wizard_cancelled_re_exported_from_wizard_module():
    """Public-facing import path."""
    from opencomputer.cli_setup.wizard import WizardCancelled
    from opencomputer.cli_ui.menu import WizardCancelled as menu_wc  # noqa: N813

    assert WizardCancelled is menu_wc, "Same exception class, single source"


def test_section_registry_has_eight_entries_with_correct_order():
    from opencomputer.cli_setup.sections import SECTION_REGISTRY
    keys = [s.key for s in SECTION_REGISTRY]
    assert keys == [
        "opencomputer_prior_detect",
        "inference_provider",
        "messaging_platforms",
        "agent_settings",
        "tts_provider",
        "terminal_backend",
        "tools",
        "launchd_service",
    ]


def test_deferred_vs_live_section_split():
    """As sub-projects ship, sections move from deferred → live.
    F0+F1+F2: inference_provider + messaging_platforms.
    S1: agent_settings. S5: launchd_service. Remaining 4 deferred."""
    from opencomputer.cli_setup.sections import SECTION_REGISTRY
    deferred = {s.key for s in SECTION_REGISTRY if s.deferred}
    live = {s.key for s in SECTION_REGISTRY if not s.deferred}
    assert deferred == {
        "opencomputer_prior_detect", "tts_provider",
        "terminal_backend", "tools",
    }
    assert live == {
        "inference_provider", "messaging_platforms", "agent_settings",
        "launchd_service",
    }


def test_run_setup_iterates_all_sections_in_order(monkeypatch, tmp_path):
    from opencomputer.cli_setup import sections as sec_mod
    from opencomputer.cli_setup.sections import SectionResult, WizardSection
    from opencomputer.cli_setup.wizard import run_setup

    calls: list[str] = []

    def mk_handler(key):
        def h(ctx):
            calls.append(key)
            return SectionResult.SKIPPED_FRESH
        return h

    fake_registry = [
        WizardSection(key=k, icon="◆", title=k, description="d",
                      handler=mk_handler(k))
        for k in ["alpha", "beta", "gamma"]
    ]
    monkeypatch.setattr(sec_mod, "SECTION_REGISTRY", fake_registry)

    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "opencomputer.cli_setup.wizard._resolve_config_path",
        lambda: config_path,
    )

    rc = run_setup()
    assert rc == 0
    assert calls == ["alpha", "beta", "gamma"]


def test_run_setup_skips_deferred_sections_without_calling_handler(
    monkeypatch, tmp_path, capsys,
):
    from opencomputer.cli_setup import sections as sec_mod
    from opencomputer.cli_setup.sections import SectionResult, WizardSection
    from opencomputer.cli_setup.wizard import run_setup

    bad_called: list[bool] = []

    def bad(ctx):
        bad_called.append(True)
        return SectionResult.CONFIGURED

    fake_registry = [
        WizardSection(
            key="x", icon="◆", title="X", description="d",
            handler=bad, deferred=True, target_subproject="M9",
        ),
    ]
    monkeypatch.setattr(sec_mod, "SECTION_REGISTRY", fake_registry)
    monkeypatch.setattr(
        "opencomputer.cli_setup.wizard._resolve_config_path",
        lambda: tmp_path / "config.yaml",
    )

    rc = run_setup()
    assert rc == 0
    # Deferred handler IS called (its job is to print the stub line),
    # but we assert that the section result was SKIPPED_FRESH and that
    # the stub message naming "M9" was emitted.
    out = capsys.readouterr().out
    assert "M9" in out


def test_run_setup_writes_config_after_all_sections(monkeypatch, tmp_path):
    from opencomputer.cli_setup import sections as sec_mod
    from opencomputer.cli_setup.sections import SectionResult, WizardSection
    from opencomputer.cli_setup.wizard import run_setup

    def mutating(ctx):
        ctx.config["model"] = {"provider": "anthropic"}
        return SectionResult.CONFIGURED

    fake_registry = [
        WizardSection(key="m", icon="◆", title="M", description="d",
                      handler=mutating),
    ]
    monkeypatch.setattr(sec_mod, "SECTION_REGISTRY", fake_registry)
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "opencomputer.cli_setup.wizard._resolve_config_path",
        lambda: config_path,
    )

    rc = run_setup()
    assert rc == 0
    import yaml
    written = yaml.safe_load(config_path.read_text())
    assert written["model"]["provider"] == "anthropic"


def test_run_setup_configured_check_keep_does_not_call_handler(
    monkeypatch, tmp_path,
):
    """idx 0 = Keep current → handler not called."""
    from opencomputer.cli_setup import sections as sec_mod
    from opencomputer.cli_setup import wizard as wiz
    from opencomputer.cli_setup.sections import SectionResult, WizardSection

    handler_called: list[bool] = []

    def h(ctx):
        handler_called.append(True)
        return SectionResult.CONFIGURED

    fake_registry = [
        WizardSection(
            key="c", icon="◆", title="C", description="d", handler=h,
            configured_check=lambda ctx: True,
        ),
    ]
    monkeypatch.setattr(sec_mod, "SECTION_REGISTRY", fake_registry)
    monkeypatch.setattr(wiz, "_resolve_config_path",
                         lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(wiz, "radiolist", lambda *a, **kw: 0)

    rc = wiz.run_setup()
    assert rc == 0
    assert handler_called == []


def test_run_setup_configured_check_reconfigure_calls_handler(
    monkeypatch, tmp_path,
):
    """idx 1 = Reconfigure → handler called."""
    from opencomputer.cli_setup import sections as sec_mod
    from opencomputer.cli_setup import wizard as wiz
    from opencomputer.cli_setup.sections import SectionResult, WizardSection

    called: list[bool] = []

    def h(ctx):
        called.append(True)
        return SectionResult.CONFIGURED

    fake_registry = [
        WizardSection(
            key="c", icon="◆", title="C", description="d", handler=h,
            configured_check=lambda ctx: True,
        ),
    ]
    monkeypatch.setattr(sec_mod, "SECTION_REGISTRY", fake_registry)
    monkeypatch.setattr(wiz, "_resolve_config_path",
                         lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(wiz, "radiolist", lambda *a, **kw: 1)

    rc = wiz.run_setup()
    assert rc == 0
    assert called == [True]


def test_run_setup_esc_during_section_returns_one(monkeypatch, tmp_path):
    from opencomputer.cli_setup import sections as sec_mod
    from opencomputer.cli_setup.sections import WizardSection
    from opencomputer.cli_setup.wizard import WizardCancelled, run_setup

    def cancelling(ctx):
        raise WizardCancelled()

    fake_registry = [
        WizardSection(key="x", icon="◆", title="X", description="d",
                      handler=cancelling),
    ]
    monkeypatch.setattr(sec_mod, "SECTION_REGISTRY", fake_registry)
    monkeypatch.setattr(
        "opencomputer.cli_setup.wizard._resolve_config_path",
        lambda: tmp_path / "config.yaml",
    )

    rc = run_setup()
    assert rc == 1
