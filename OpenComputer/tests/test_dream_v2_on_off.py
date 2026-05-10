"""``dream-v2-on`` flips ``memory.dreaming_v2_enabled``; ``dream-v2-off`` unflips.

Closes the CLI gap that v1 ``dream-on`` registers a cron job for v1
``dream-now`` (which uses ``DreamRunner`` from ``opencomputer.agent.dreaming``),
but v2 has no equivalent toggle. v2 ticks via ``system_jobs.run_system_tick``
gated solely by ``cfg.memory.dreaming_v2_enabled`` — so flipping the flag
is sufficient (no separate cron entry needed; the system tick fires on
every cron daemon iteration).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / ".opencomputer"
    home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(home))
    # 2026-05-10 — dreaming defaults flipped to True; these tests cover the
    # CLI flip semantics (off → on → off), so explicitly write a config.yaml
    # that pins both flags off as the starting state.
    (home / "config.yaml").write_text(
        "memory:\n  dreaming_enabled: false\n  dreaming_v2_enabled: false\n",
        encoding="utf-8",
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_dream_v2_on_flips_config_flag(runner: CliRunner) -> None:
    from opencomputer.agent.config_store import load_config
    from opencomputer.cli import app

    cfg_before = load_config()
    assert cfg_before.memory.dreaming_v2_enabled is False

    result = runner.invoke(app, ["memory", "dream-v2-on"])
    assert result.exit_code == 0, result.output
    assert "dreaming_v2 enabled" in result.output

    cfg_after = load_config()
    assert cfg_after.memory.dreaming_v2_enabled is True


def test_dream_v2_on_does_not_create_cron_entry(runner: CliRunner) -> None:
    """v2 fires via system_tick — flag-only flip, no separate cron job.

    Distinct from v1 ``dream-on`` which creates a 'memory-dreaming'
    cron entry. If this test fails because a cron entry is created,
    audit whether v2 was accidentally given a duplicate scheduling
    surface.
    """
    from opencomputer.cli import app
    from opencomputer.cron import jobs as cron_jobs

    runner.invoke(app, ["memory", "dream-v2-on"])

    v2_named = [
        j for j in cron_jobs.list_jobs()
        if "v2" in j.get("name", "").lower()
        or "dreaming-v2" in j.get("name", "").lower()
    ]
    assert v2_named == [], (
        f"dream-v2-on should NOT register a cron entry (v2 fires via "
        f"system_tick). Got: {[j['name'] for j in v2_named]}"
    )


def test_dream_v2_off_unflips_flag(runner: CliRunner) -> None:
    from opencomputer.agent.config_store import load_config
    from opencomputer.cli import app

    runner.invoke(app, ["memory", "dream-v2-on"])
    assert load_config().memory.dreaming_v2_enabled is True

    result = runner.invoke(app, ["memory", "dream-v2-off"])
    assert result.exit_code == 0, result.output
    assert "dreaming_v2 disabled" in result.output

    assert load_config().memory.dreaming_v2_enabled is False


def test_dream_v2_on_independent_of_dream_on(runner: CliRunner) -> None:
    """v1 and v2 flags are orthogonal — toggling v2 must NOT affect v1."""
    from opencomputer.agent.config_store import load_config
    from opencomputer.cli import app

    runner.invoke(app, ["memory", "dream-on", "--interval", "daily"])
    cfg_v1 = load_config()
    assert cfg_v1.memory.dreaming_enabled is True
    assert cfg_v1.memory.dreaming_v2_enabled is False

    runner.invoke(app, ["memory", "dream-v2-on"])
    cfg_both = load_config()
    assert cfg_both.memory.dreaming_enabled is True  # v1 unchanged
    assert cfg_both.memory.dreaming_v2_enabled is True

    runner.invoke(app, ["memory", "dream-v2-off"])
    cfg_v1_only = load_config()
    assert cfg_v1_only.memory.dreaming_enabled is True  # v1 still on
    assert cfg_v1_only.memory.dreaming_v2_enabled is False


def test_dream_v2_on_idempotent(runner: CliRunner) -> None:
    from opencomputer.agent.config_store import load_config
    from opencomputer.cli import app

    runner.invoke(app, ["memory", "dream-v2-on"])
    runner.invoke(app, ["memory", "dream-v2-on"])
    runner.invoke(app, ["memory", "dream-v2-on"])
    assert load_config().memory.dreaming_v2_enabled is True
