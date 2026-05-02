"""Tests for the prior-install detection wizard section (M1)."""
from __future__ import annotations

from pathlib import Path


def _make_ctx(tmp_path: Path, config: dict | None = None):
    from opencomputer.cli_setup.sections import WizardCtx
    return WizardCtx(
        config=config or {},
        config_path=tmp_path / "oc-home" / "config.yaml",
        is_first_run=True,
    )


def test_no_prior_install_returns_skipped_fresh(monkeypatch, tmp_path):
    """When neither ~/.openclaw nor ~/.hermes exist, section is a no-op."""
    from opencomputer.cli_setup.section_handlers import prior_install as pi
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(pi, "_detect_prior_installs", lambda: [])

    ctx = _make_ctx(tmp_path)
    result = pi.run_prior_install_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH


def test_openclaw_detected_user_declines_returns_skipped_fresh(
    monkeypatch, tmp_path,
):
    from opencomputer.cli_setup.section_handlers import prior_install as pi
    from opencomputer.cli_setup.sections import SectionResult

    fake_home = tmp_path / "fake-home" / ".openclaw"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(pi, "_detect_prior_installs",
                         lambda: [{"name": "OpenClaw", "path": fake_home}])
    monkeypatch.setattr(pi, "radiolist", lambda *a, **kw: 1)  # Decline

    ctx = _make_ctx(tmp_path)
    result = pi.run_prior_install_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH


def test_openclaw_detected_user_accepts_imports_files(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import prior_install as pi
    from opencomputer.cli_setup.sections import SectionResult

    src = tmp_path / "src" / ".openclaw"
    src.mkdir(parents=True)
    (src / "MEMORY.md").write_text("# Memory from openclaw\nold note\n")
    (src / "USER.md").write_text("# User from openclaw\n")
    (src / "skills").mkdir()
    (src / "skills" / "research" / "arxiv").mkdir(parents=True)
    (src / "skills" / "research" / "arxiv" / "SKILL.md").write_text("# arxiv\n")

    dest = tmp_path / "oc-home"
    dest.mkdir()

    monkeypatch.setattr(pi, "_detect_prior_installs",
                         lambda: [{"name": "OpenClaw", "path": src}])
    monkeypatch.setattr(pi, "radiolist", lambda *a, **kw: 0)  # Accept
    monkeypatch.setattr(pi, "_oc_home", lambda: dest)

    ctx = _make_ctx(tmp_path)
    result = pi.run_prior_install_section(ctx)

    assert result == SectionResult.CONFIGURED
    # MEMORY.md imported
    imported_memory = dest / "MEMORY.md"
    assert imported_memory.exists()
    assert "old note" in imported_memory.read_text()
    # Skills tree copied
    assert (dest / "skills" / "research" / "arxiv" / "SKILL.md").exists()


def test_openclaw_accept_does_not_overwrite_existing_oc_memory(
    monkeypatch, tmp_path,
):
    """If ~/.opencomputer/MEMORY.md already exists, import preserves it
    (writes to MEMORY.md.imported instead — no destructive overwrite)."""
    from opencomputer.cli_setup.section_handlers import prior_install as pi

    src = tmp_path / "src" / ".openclaw"
    src.mkdir(parents=True)
    (src / "MEMORY.md").write_text("imported content\n")

    dest = tmp_path / "oc-home"
    dest.mkdir()
    (dest / "MEMORY.md").write_text("existing content\n")

    monkeypatch.setattr(pi, "_detect_prior_installs",
                         lambda: [{"name": "OpenClaw", "path": src}])
    monkeypatch.setattr(pi, "radiolist", lambda *a, **kw: 0)
    monkeypatch.setattr(pi, "_oc_home", lambda: dest)

    ctx = _make_ctx(tmp_path)
    pi.run_prior_install_section(ctx)

    assert (dest / "MEMORY.md").read_text() == "existing content\n", \
        "existing MEMORY.md must NOT be overwritten"
    assert (dest / "MEMORY.md.imported").exists(), \
        "imported content lands at MEMORY.md.imported"
    assert (dest / "MEMORY.md.imported").read_text() == "imported content\n"


def test_section_records_migration_in_config(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import prior_install as pi

    src = tmp_path / "src" / ".hermes"
    src.mkdir(parents=True)
    (src / "MEMORY.md").write_text("h\n")

    dest = tmp_path / "oc-home"
    dest.mkdir()

    monkeypatch.setattr(pi, "_detect_prior_installs",
                         lambda: [{"name": "Hermes", "path": src}])
    monkeypatch.setattr(pi, "radiolist", lambda *a, **kw: 0)
    monkeypatch.setattr(pi, "_oc_home", lambda: dest)

    ctx = _make_ctx(tmp_path)
    pi.run_prior_install_section(ctx)

    migrations = ctx.config.get("migrations", {}).get("prior_install", [])
    assert any(m.get("source") == "Hermes" for m in migrations), \
        "config records the migration source"


def test_section_registry_uses_live_prior_install_handler():
    from opencomputer.cli_setup.sections import SECTION_REGISTRY

    sec = next(s for s in SECTION_REGISTRY if s.key == "opencomputer_prior_detect")
    assert sec.deferred is False, "opencomputer_prior_detect is now LIVE (M1)"
