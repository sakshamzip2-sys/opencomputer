"""``dream-on`` registers a cron job; ``dream-off`` removes it.

Round 4 Item 4. Closes the gap where ``dream-on --interval daily``
just flipped a config flag and printed "set up cron yourself" — most
users would never wire up the schedule. Now it uses the cron infra
that landed earlier (opencomputer/cron/) automatically.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the cron + config + memory layer at a throwaway profile dir
    so tests don't touch the developer's real ~/.opencomputer/."""
    home = tmp_path / ".opencomputer"
    home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(home))


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_dream_on_creates_cron_job_with_known_name(runner: CliRunner) -> None:
    """`dream-on --interval daily` creates a cron job tagged
    ``memory-dreaming`` so we can find + remove it later."""
    from opencomputer.cli import app
    from opencomputer.cron import jobs as cron_jobs

    result = runner.invoke(app, ["memory", "dream-on", "--interval", "daily"])
    assert result.exit_code == 0, result.output

    matching = [j for j in cron_jobs.list_jobs() if j["name"] == "memory-dreaming"]
    assert len(matching) == 1, (
        f"expected exactly one memory-dreaming job; got {len(matching)}: "
        f"{[j['name'] for j in cron_jobs.list_jobs()]}"
    )
    assert matching[0]["schedule"]["expr"] == "0 3 * * *"
    assert matching[0]["schedule"]["kind"] == "cron"


def test_dream_on_hourly_uses_top_of_hour_schedule(runner: CliRunner) -> None:
    from opencomputer.cli import app
    from opencomputer.cron import jobs as cron_jobs

    runner.invoke(app, ["memory", "dream-on", "--interval", "hourly"])

    matching = [j for j in cron_jobs.list_jobs() if j["name"] == "memory-dreaming"]
    assert len(matching) == 1
    assert matching[0]["schedule"]["expr"] == "0 * * * *"


def test_dream_on_replaces_existing_job_on_interval_change(
    runner: CliRunner,
) -> None:
    """Switching daily → hourly removes the daily entry first; never two
    duplicate dreaming jobs."""
    from opencomputer.cli import app
    from opencomputer.cron import jobs as cron_jobs

    runner.invoke(app, ["memory", "dream-on", "--interval", "daily"])
    runner.invoke(app, ["memory", "dream-on", "--interval", "hourly"])

    matching = [j for j in cron_jobs.list_jobs() if j["name"] == "memory-dreaming"]
    assert len(matching) == 1, "switching interval must replace, not add"
    assert matching[0]["schedule"]["expr"] == "0 * * * *", (
        "after switch, the surviving job must reflect the new interval"
    )


def test_dream_on_idempotent_on_repeat_call(runner: CliRunner) -> None:
    """Re-running with the same interval still leaves exactly one job —
    not two — so a script that runs `dream-on` defensively can't bloat
    the schedule."""
    from opencomputer.cli import app
    from opencomputer.cron import jobs as cron_jobs

    runner.invoke(app, ["memory", "dream-on", "--interval", "daily"])
    runner.invoke(app, ["memory", "dream-on", "--interval", "daily"])
    runner.invoke(app, ["memory", "dream-on", "--interval", "daily"])

    matching = [j for j in cron_jobs.list_jobs() if j["name"] == "memory-dreaming"]
    assert len(matching) == 1


def test_dream_off_removes_the_cron_job(runner: CliRunner) -> None:
    """`dream-off` must clean up the cron job, otherwise users who
    disable dreaming would still pay the nightly LLM cost."""
    from opencomputer.cli import app
    from opencomputer.cron import jobs as cron_jobs

    runner.invoke(app, ["memory", "dream-on", "--interval", "daily"])
    assert any(j["name"] == "memory-dreaming" for j in cron_jobs.list_jobs())

    result = runner.invoke(app, ["memory", "dream-off"])
    assert result.exit_code == 0
    assert not any(j["name"] == "memory-dreaming" for j in cron_jobs.list_jobs())


def test_dream_off_when_no_job_exists_does_not_error(runner: CliRunner) -> None:
    """`dream-off` called before any `dream-on` (or after manual cron
    cleanup) must not crash."""
    from opencomputer.cli import app

    result = runner.invoke(app, ["memory", "dream-off"])
    assert result.exit_code == 0


def test_dream_on_does_not_disturb_unrelated_cron_jobs(
    runner: CliRunner,
) -> None:
    """A user's hand-rolled cron job (different name) survives
    dream-on / dream-off cycles."""
    from opencomputer.cli import app
    from opencomputer.cron import jobs as cron_jobs

    user_job = cron_jobs.create_job(
        schedule="0 9 * * *",
        name="my-morning-stocks",
        prompt="Give me a market summary.",
    )

    runner.invoke(app, ["memory", "dream-on", "--interval", "daily"])
    runner.invoke(app, ["memory", "dream-off"])

    surviving = [j for j in cron_jobs.list_jobs() if j["id"] == user_job["id"]]
    assert len(surviving) == 1, (
        "dream-on/off must not touch jobs with other names"
    )


def test_dream_on_invalid_interval_does_not_create_job(runner: CliRunner) -> None:
    """Invalid interval exits non-zero AND must not leave a half-state
    cron job behind."""
    from opencomputer.cli import app
    from opencomputer.cron import jobs as cron_jobs

    result = runner.invoke(app, ["memory", "dream-on", "--interval", "weekly"])
    assert result.exit_code != 0
    assert not any(j["name"] == "memory-dreaming" for j in cron_jobs.list_jobs())
