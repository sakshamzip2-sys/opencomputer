"""Tests for the terminal backend wizard section (S3)."""
from __future__ import annotations

from pathlib import Path


def _make_ctx(tmp_path: Path, config: dict | None = None):
    from opencomputer.cli_setup.sections import WizardCtx
    return WizardCtx(
        config=config or {},
        config_path=tmp_path / "config.yaml",
        is_first_run=True,
    )


def test_local_only_environment_offers_local_and_skip(monkeypatch, tmp_path):
    """When neither docker nor apptainer is installed, the menu offers
    only [local, skip]."""
    from opencomputer.cli_setup.section_handlers import terminal_backend as tb

    monkeypatch.setattr(tb, "_detect_backends", lambda: ["local"])

    captured = []

    def fake_radiolist(question, choices, default=0, description=None, **kw):
        captured.extend(c.value for c in choices)
        return 0

    monkeypatch.setattr(tb, "radiolist", fake_radiolist)

    ctx = _make_ctx(tmp_path)
    tb.run_terminal_backend_section(ctx)

    assert "local" in captured
    assert "__skip__" in captured
    assert "docker" not in captured
    assert "apptainer" not in captured


def test_docker_present_offers_docker_choice(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import terminal_backend as tb

    monkeypatch.setattr(tb, "_detect_backends",
                         lambda: ["docker", "local"])

    captured = []

    def fake_radiolist(question, choices, default=0, description=None, **kw):
        captured.extend(c.value for c in choices)
        return 0

    monkeypatch.setattr(tb, "radiolist", fake_radiolist)

    ctx = _make_ctx(tmp_path)
    tb.run_terminal_backend_section(ctx)

    assert "docker" in captured
    assert "local" in captured


def test_pick_local_writes_config(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import terminal_backend as tb
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(tb, "_detect_backends", lambda: ["local"])
    monkeypatch.setattr(tb, "radiolist", lambda *a, **kw: 0)  # local

    ctx = _make_ctx(tmp_path)
    result = tb.run_terminal_backend_section(ctx)

    assert result == SectionResult.CONFIGURED
    # Aligned with Hermes naming — "local" not "native".
    assert ctx.config["terminal"]["backend"] == "local"


def test_skip_keeps_existing_config(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import terminal_backend as tb
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(tb, "_detect_backends",
                         lambda: ["docker", "local"])
    # idx 2 = skip (after docker, local)
    monkeypatch.setattr(tb, "radiolist", lambda *a, **kw: 2)

    existing = {"terminal": {"backend": "apptainer"}}
    ctx = _make_ctx(tmp_path, config=existing)
    result = tb.run_terminal_backend_section(ctx)

    assert result == SectionResult.SKIPPED_FRESH
    assert ctx.config["terminal"]["backend"] == "apptainer"


def test_section_registry_uses_live_terminal_backend_handler():
    from opencomputer.cli_setup.sections import SECTION_REGISTRY

    sec = next(s for s in SECTION_REGISTRY if s.key == "terminal_backend")
    assert sec.deferred is False, "terminal_backend is now LIVE (S3)"
