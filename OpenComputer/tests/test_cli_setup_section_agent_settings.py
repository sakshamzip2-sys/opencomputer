"""Tests for the agent-settings wizard section (S1)."""
from __future__ import annotations

from pathlib import Path


def _make_ctx(tmp_path: Path, config: dict | None = None):
    from opencomputer.cli_setup.sections import WizardCtx
    return WizardCtx(
        config=config or {},
        config_path=tmp_path / "config.yaml",
        is_first_run=True,
    )


def test_recommended_defaults_writes_loop_config(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import agent_settings as ag
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ag, "radiolist", lambda *a, **kw: 0)  # Apply

    ctx = _make_ctx(tmp_path)
    result = ag.run_agent_settings_section(ctx)

    assert result == SectionResult.CONFIGURED
    loop = ctx.config["loop"]
    assert loop["max_iterations"] == 90
    assert loop["parallel_tools"] is True
    assert loop["inactivity_timeout_s"] == 300
    assert loop["iteration_timeout_s"] == 1800


def test_skip_keeps_existing_loop_config(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import agent_settings as ag
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ag, "radiolist", lambda *a, **kw: 1)  # Skip

    existing = {"loop": {"max_iterations": 42, "parallel_tools": False}}
    ctx = _make_ctx(tmp_path, config=existing)
    result = ag.run_agent_settings_section(ctx)

    assert result == SectionResult.SKIPPED_FRESH
    assert ctx.config["loop"]["max_iterations"] == 42, "skip must not mutate"
    assert ctx.config["loop"]["parallel_tools"] is False


def test_apply_overwrites_partial_existing_config(monkeypatch, tmp_path):
    """User had max_iterations=20 set; apply defaults overwrites it."""
    from opencomputer.cli_setup.section_handlers import agent_settings as ag

    monkeypatch.setattr(ag, "radiolist", lambda *a, **kw: 0)

    ctx = _make_ctx(tmp_path, config={"loop": {"max_iterations": 20}})
    ag.run_agent_settings_section(ctx)

    assert ctx.config["loop"]["max_iterations"] == 90


def test_apply_summary_printed(monkeypatch, tmp_path, capsys):
    from opencomputer.cli_setup.section_handlers import agent_settings as ag

    monkeypatch.setattr(ag, "radiolist", lambda *a, **kw: 0)

    ctx = _make_ctx(tmp_path)
    ag.run_agent_settings_section(ctx)

    out = capsys.readouterr().out
    assert "90" in out, "summary should mention max_iterations"
    assert "300" in out or "5 min" in out, "summary mentions inactivity timeout"


def test_is_configured_returns_true_when_loop_block_exists(tmp_path):
    from opencomputer.cli_setup.section_handlers.agent_settings import (
        is_agent_settings_configured,
    )

    empty = _make_ctx(tmp_path)
    assert is_agent_settings_configured(empty) is False

    customized = _make_ctx(
        tmp_path, config={"loop": {"max_iterations": 90}},
    )
    assert is_agent_settings_configured(customized) is True


def test_section_registry_uses_live_agent_settings_handler():
    """After S1 lands, agent_settings should NOT be a deferred stub."""
    from opencomputer.cli_setup.sections import SECTION_REGISTRY

    sec = next(s for s in SECTION_REGISTRY if s.key == "agent_settings")
    assert sec.deferred is False, (
        "agent_settings is now LIVE (S1) — not deferred"
    )
    assert sec.configured_check is not None
