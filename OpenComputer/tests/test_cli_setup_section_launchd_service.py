"""Tests for the launchd_service wizard section (S5)."""
from __future__ import annotations

from pathlib import Path


def _make_ctx(tmp_path: Path, config: dict | None = None):
    from opencomputer.cli_setup.sections import WizardCtx
    return WizardCtx(
        config=config or {},
        config_path=tmp_path / "config.yaml",
        is_first_run=True,
    )


def test_skip_branch_returns_skipped_fresh(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import launchd_service as ls
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ls, "_is_macos", lambda: True)
    monkeypatch.setattr(ls, "radiolist", lambda *a, **kw: 1)  # Skip

    ctx = _make_ctx(tmp_path)
    result = ls.run_launchd_service_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH


def test_non_macos_returns_skipped_fresh_without_prompt(monkeypatch, tmp_path):
    """On Linux/Windows, the section is a no-op."""
    from opencomputer.cli_setup.section_handlers import launchd_service as ls
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ls, "_is_macos", lambda: False)

    radiolist_called: list[bool] = []

    def fake_radiolist(*a, **kw):
        radiolist_called.append(True)
        return 0

    monkeypatch.setattr(ls, "radiolist", fake_radiolist)

    ctx = _make_ctx(tmp_path)
    result = ls.run_launchd_service_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH
    assert radiolist_called == [], "Non-macOS must NOT prompt"


def test_install_writes_plist_and_loads(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import launchd_service as ls
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(ls, "_is_macos", lambda: True)
    monkeypatch.setattr(ls, "radiolist", lambda *a, **kw: 0)  # Install

    plist_target = tmp_path / "LaunchAgents"
    monkeypatch.setattr(ls, "_launch_agents_dir", lambda: plist_target)

    launchctl_calls: list[list[str]] = []

    def fake_launchctl(args):
        launchctl_calls.append(args)
        return 0  # success

    monkeypatch.setattr(ls, "_run_launchctl", fake_launchctl)
    monkeypatch.setattr(ls, "_oc_executable_path", lambda: "/opt/homebrew/bin/oc")

    ctx = _make_ctx(tmp_path)
    result = ls.run_launchd_service_section(ctx)

    assert result == SectionResult.CONFIGURED
    plist_files = list(plist_target.glob("*.plist"))
    assert len(plist_files) == 1, "exactly one plist written"
    plist_text = plist_files[0].read_text()
    assert "/opt/homebrew/bin/oc" in plist_text
    assert "gateway" in plist_text
    assert any("load" in c for c in launchctl_calls), "launchctl load was invoked"


def test_install_records_in_config(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import launchd_service as ls

    monkeypatch.setattr(ls, "_is_macos", lambda: True)
    monkeypatch.setattr(ls, "radiolist", lambda *a, **kw: 0)
    monkeypatch.setattr(ls, "_launch_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(ls, "_run_launchctl", lambda args: 0)
    monkeypatch.setattr(ls, "_oc_executable_path", lambda: "/usr/local/bin/oc")

    ctx = _make_ctx(tmp_path)
    ls.run_launchd_service_section(ctx)

    assert ctx.config.get("gateway", {}).get("launchd_installed") is True


def test_section_registry_uses_live_launchd_handler():
    from opencomputer.cli_setup.sections import SECTION_REGISTRY

    sec = next(s for s in SECTION_REGISTRY if s.key == "launchd_service")
    assert sec.deferred is False, "launchd_service is now LIVE (S5)"
