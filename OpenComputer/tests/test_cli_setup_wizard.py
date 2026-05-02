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
    from opencomputer.cli_ui.menu import WizardCancelled as menu_wc
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


def test_six_sections_are_deferred_two_are_live():
    from opencomputer.cli_setup.sections import SECTION_REGISTRY
    deferred = {s.key for s in SECTION_REGISTRY if s.deferred}
    live = {s.key for s in SECTION_REGISTRY if not s.deferred}
    assert deferred == {
        "opencomputer_prior_detect", "agent_settings", "tts_provider",
        "terminal_backend", "tools", "launchd_service",
    }
    assert live == {"inference_provider", "messaging_platforms"}
