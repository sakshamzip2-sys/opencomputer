"""Hermes parity: oc cron edit — change schedule/prompt/skill on existing jobs."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from opencomputer.cli_cron import cron_app
from opencomputer.cron.jobs import create_job, get_job

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    yield tmp_path


def test_edit_schedule(isolated_home):
    job = create_job(schedule="every 1h", skill="x")
    res = runner.invoke(cron_app, ["edit", job["id"], "--schedule", "every 4h"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert updated["schedule"]["display"] == "every 240m"


def test_edit_prompt(isolated_home):
    job = create_job(schedule="every 1h", skill="x")
    res = runner.invoke(cron_app, ["edit", job["id"], "--prompt", "do new thing"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert updated["prompt"] == "do new thing"


def test_edit_replace_skills(isolated_home):
    job = create_job(schedule="every 1h", skills=["a", "b"])
    res = runner.invoke(cron_app, ["edit", job["id"], "--skill", "c", "--skill", "d"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert updated["skills"] == ["c", "d"]


def test_edit_add_skill(isolated_home):
    job = create_job(schedule="every 1h", skills=["a"])
    res = runner.invoke(cron_app, ["edit", job["id"], "--add-skill", "b"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert updated["skills"] == ["a", "b"]


def test_edit_remove_skill(isolated_home):
    job = create_job(schedule="every 1h", skills=["a", "b"])
    res = runner.invoke(cron_app, ["edit", job["id"], "--remove-skill", "a"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert updated["skills"] == ["b"]


def test_edit_clear_skills(isolated_home):
    job = create_job(schedule="every 1h", skills=["a", "b"])
    res = runner.invoke(cron_app, ["edit", job["id"], "--clear-skills"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert not updated.get("skills")
    assert not updated.get("skill")


def test_edit_unknown_id(isolated_home):
    res = runner.invoke(cron_app, ["edit", "nonexistent", "--prompt", "x"])
    assert res.exit_code == 2


def test_create_with_multi_skill(isolated_home):
    res = runner.invoke(
        cron_app,
        ["create", "--schedule", "every 1h", "--skill", "a", "--skill", "b", "--name", "T"],
    )
    assert res.exit_code == 0, res.output


def test_edit_prompt_on_skill_job_clears_skill(isolated_home):
    """Production-grade: --prompt on a skill job clears the skill.

    Otherwise _build_run_prompt would silently prefer the (stale) skill
    and the prompt edit would no-op invisibly.
    """
    job = create_job(schedule="every 1h", skill="x")
    res = runner.invoke(cron_app, ["edit", job["id"], "--prompt", "do new thing"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert updated["prompt"] == "do new thing"
    assert updated["skill"] is None
    assert not updated.get("skills")


def test_edit_skill_on_prompt_job_clears_prompt(isolated_home):
    """Mirror: --skill on a prompt job clears the prompt."""
    job = create_job(schedule="every 1h", prompt="old prompt")
    res = runner.invoke(cron_app, ["edit", job["id"], "--skill", "newskill"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert updated["skills"] == ["newskill"]
    assert updated["prompt"] is None


def test_edit_invalid_notify_rejected(isolated_home):
    """Production-grade: invalid notify target fails fast at edit time."""
    job = create_job(schedule="every 1h", skill="x")
    res = runner.invoke(
        cron_app, ["edit", job["id"], "--notify", "made_up:1234"]
    )
    assert res.exit_code == 2
    # Original job unchanged.
    assert get_job(job["id"])["notify"] is None


def test_create_invalid_notify_rejected(isolated_home):
    """Production-grade: invalid notify target fails fast at create."""
    res = runner.invoke(
        cron_app,
        ["create", "--schedule", "every 1h", "--skill", "x", "--notify", "made_up:1234"],
    )
    assert res.exit_code == 2
