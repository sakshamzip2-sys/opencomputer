"""B6: oc cron prune --noise removes short-named + duplicate cron jobs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_jobs(home: Path, jobs: list[dict]) -> Path:
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True)
    jobs_file = cron_dir / "jobs.json"
    jobs_file.write_text(
        json.dumps({"jobs": jobs, "updated_at": "2026-05-09T00:00:00+00:00"})
    )
    return jobs_file


@pytest.fixture
def cron_jobs_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profile_home = tmp_path / "profile"
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(profile_home))
    return _write_jobs(profile_home, [
        # Noise: short names
        {"id": "01abc", "name": "a", "schedule": "every 60m", "prompt": ""},
        {"id": "02bcd", "name": "x", "schedule": "every 60m", "prompt": "x"},
        # Noise: exact duplicate of job 04
        {"id": "03cde", "name": "blogwa", "schedule": "every 60m", "prompt": "blogw"},
        {"id": "04def", "name": "blogwa", "schedule": "every 60m", "prompt": "blogw"},
        # Real job
        {
            "id": "05efg",
            "name": "Monday stock briefing",
            "schedule": "30 8 * * 1",
            "prompt": "Generate Monday stock briefing",
        },
    ])


def test_prune_dry_run_lists_noise_jobs(
    runner: CliRunner, cron_jobs_file: Path
) -> None:
    """Default behavior is dry-run; lists candidates without deleting."""
    result = runner.invoke(app, ["cron", "prune", "--noise"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    # Three noise jobs flagged (short name 'a', 'x', dup 'blogwa')
    assert "01abc" in out  # short name 'a'
    assert "02bcd" in out  # short name 'x'
    # One of the dup blogwa pair
    assert "03cde" in out or "04def" in out
    # Real job NOT flagged
    assert "05efg" not in out
    # File on disk unchanged
    after = json.loads(cron_jobs_file.read_text())["jobs"]
    assert len(after) == 5


def test_prune_apply_removes_noise(
    runner: CliRunner, cron_jobs_file: Path
) -> None:
    """`--apply --yes` actually deletes flagged jobs from jobs.json."""
    result = runner.invoke(app, ["cron", "prune", "--noise", "--apply", "--yes"])
    assert result.exit_code == 0, result.stdout
    remaining = json.loads(cron_jobs_file.read_text())["jobs"]
    names = [j["name"] for j in remaining]
    assert "Monday stock briefing" in names
    assert "a" not in names
    assert "x" not in names
    # Only one blogwa dup remains
    assert names.count("blogwa") <= 1


def test_prune_no_filter_short_circuits(
    runner: CliRunner, cron_jobs_file: Path
) -> None:
    """`oc cron prune` without --noise shouldn't delete anything."""
    result = runner.invoke(app, ["cron", "prune"])
    assert result.exit_code == 0
    after = json.loads(cron_jobs_file.read_text())["jobs"]
    assert len(after) == 5
