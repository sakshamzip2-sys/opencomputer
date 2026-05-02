"""Tests for the messaging-platforms wizard section."""
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
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp
    from opencomputer.cli_setup.sections import SectionResult

    # First radiolist: 1 = Skip
    monkeypatch.setattr(mp, "radiolist", lambda *a, **kw: 1)

    ctx = _make_ctx(tmp_path)
    result = mp.run_messaging_platforms_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH


def test_setup_now_branch_calls_checklist_and_invokes_per_platform(
    monkeypatch, tmp_path,
):
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp
    from opencomputer.cli_setup.sections import SectionResult

    fake_platforms = [
        {"name": "telegram", "label": "Telegram", "configured": False},
        {"name": "discord", "label": "Discord", "configured": False},
    ]
    monkeypatch.setattr(mp, "_discover_platforms", lambda: fake_platforms)

    monkeypatch.setattr(mp, "radiolist", lambda *a, **kw: 0)  # set up now
    monkeypatch.setattr(mp, "checklist", lambda *a, **kw: [0, 1])  # both

    invocations: list[str] = []

    def fake_invoke(name, ctx):
        invocations.append(name)
        return True

    monkeypatch.setattr(mp, "_invoke_platform_setup", fake_invoke)

    ctx = _make_ctx(tmp_path)
    result = mp.run_messaging_platforms_section(ctx)

    assert result == SectionResult.CONFIGURED
    assert invocations == ["telegram", "discord"]


def test_no_platforms_selected_returns_skipped_fresh(monkeypatch, tmp_path):
    from opencomputer.cli_setup.section_handlers import messaging_platforms as mp
    from opencomputer.cli_setup.sections import SectionResult

    monkeypatch.setattr(mp, "_discover_platforms", lambda: [
        {"name": "telegram", "label": "Telegram", "configured": False},
    ])
    monkeypatch.setattr(mp, "radiolist", lambda *a, **kw: 0)
    monkeypatch.setattr(mp, "checklist", lambda *a, **kw: [])

    ctx = _make_ctx(tmp_path)
    result = mp.run_messaging_platforms_section(ctx)
    assert result == SectionResult.SKIPPED_FRESH


def test_is_messaging_platforms_configured(tmp_path):
    from opencomputer.cli_setup.section_handlers.messaging_platforms import (
        is_messaging_platforms_configured,
    )

    empty = _make_ctx(tmp_path)
    assert is_messaging_platforms_configured(empty) is False

    with_platform = _make_ctx(
        tmp_path,
        config={"gateway": {"platforms": ["telegram"]}},
    )
    assert is_messaging_platforms_configured(with_platform) is True
