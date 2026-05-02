"""Tests for the tools wizard section (S4)."""
from __future__ import annotations

from pathlib import Path


def _make_ctx(tmp_path: Path, config: dict | None = None):
    from opencomputer.cli_setup.sections import WizardCtx
    return WizardCtx(
        config=config or {},
        config_path=tmp_path / "config.yaml",
        is_first_run=True,
    )


def test_recommended_preset_writes_enabled_plugins(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import tools as ts
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ts, "radiolist", lambda *a, **kw: 0)  # Apply

    ctx = _make_ctx(tmp_path)
    result = ts.run_tools_section(ctx)

    assert result == SectionResult.CONFIGURED
    enabled = ctx.config.get("plugins", {}).get("enabled", [])
    assert "coding-harness" in enabled
    assert "memory-honcho" in enabled
    assert "dev-tools" in enabled


def test_skip_keeps_existing_plugins_config(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import tools as ts
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ts, "radiolist", lambda *a, **kw: 1)  # Skip

    existing = {"plugins": {"enabled": ["custom-only"]}}
    ctx = _make_ctx(tmp_path, config=existing)
    result = ts.run_tools_section(ctx)

    assert result == SectionResult.SKIPPED_FRESH
    assert ctx.config["plugins"]["enabled"] == ["custom-only"]


def test_apply_does_not_duplicate_existing_entries(monkeypatch, tmp_path):
    """If user already has coding-harness enabled, applying preset keeps
    it once (no duplicate)."""
    from opencomputer.cli_setup.section_handlers import tools as ts

    monkeypatch.setattr(ts, "radiolist", lambda *a, **kw: 0)

    ctx = _make_ctx(
        tmp_path,
        config={"plugins": {"enabled": ["coding-harness", "my-plugin"]}},
    )
    ts.run_tools_section(ctx)

    enabled = ctx.config["plugins"]["enabled"]
    assert enabled.count("coding-harness") == 1, "no duplicates"
    assert "my-plugin" in enabled, "user's existing entries preserved"


def test_apply_summary_printed(monkeypatch, tmp_path, capsys):
    from opencomputer.cli_setup.section_handlers import tools as ts

    monkeypatch.setattr(ts, "radiolist", lambda *a, **kw: 0)

    ctx = _make_ctx(tmp_path)
    ts.run_tools_section(ctx)

    out = capsys.readouterr().out
    assert "coding-harness" in out
    assert "✓" in out


def test_section_registry_uses_live_tools_handler():
    from opencomputer.cli_setup.sections import SECTION_REGISTRY

    sec = next(s for s in SECTION_REGISTRY if s.key == "tools")
    assert sec.deferred is False, "tools is now LIVE (S4)"
