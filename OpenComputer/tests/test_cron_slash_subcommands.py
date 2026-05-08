"""Hermes parity: /cron list/add/pause/resume/run/remove + bug fix.

The pre-2026-05-08 _handle_cron_inline imported a nonexistent
``opencomputer.cron.store.CronStore`` (typo / leftover from a different
port), so /cron silently printed "Cron unavailable: <import error>"
even when jobs existed. This test family covers the fix.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencomputer.cli_ui.slash_handlers import SlashContext, _handle_cron_inline


@pytest.fixture
def ctx():
    return SlashContext(
        console=MagicMock(),
        session_id="s1",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=list,
    )


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    yield tmp_path


def test_cron_no_args_lists_jobs_no_crash(ctx, isolated_home):
    """Bug fix: /cron with no args used to print 'Cron unavailable: ...'.
    Now lists jobs cleanly (or empty-state hint when none)."""
    res = _handle_cron_inline(ctx, [])
    assert res.handled


def test_cron_list_subcommand(ctx, isolated_home):
    from opencomputer.cron.jobs import create_job
    create_job(schedule="every 1h", skill="x")
    res = _handle_cron_inline(ctx, ["list"])
    assert res.handled
    # Should not have printed an error.
    printed = " ".join(str(c) for c in ctx.console.print.call_args_list)
    assert "unavailable" not in printed.lower()


def test_cron_add_creates_job(ctx, isolated_home):
    from opencomputer.cron.jobs import list_jobs
    res = _handle_cron_inline(ctx, ["add", "every 1h", "Check status"])
    assert res.handled
    jobs = list_jobs()
    assert len(jobs) == 1
    assert "Check status" in (jobs[0]["prompt"] or "")


def test_cron_add_with_skill(ctx, isolated_home):
    from opencomputer.cron.jobs import list_jobs
    res = _handle_cron_inline(ctx, ["add", "every 1h", "--skill", "blogwatcher"])
    assert res.handled
    jobs = list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["skill"] == "blogwatcher"


def test_cron_pause_resume(ctx, isolated_home):
    from opencomputer.cron.jobs import create_job, get_job
    job = create_job(schedule="every 1h", skill="x")
    _handle_cron_inline(ctx, ["pause", job["id"]])
    assert get_job(job["id"])["state"] == "paused"
    _handle_cron_inline(ctx, ["resume", job["id"]])
    assert get_job(job["id"])["state"] == "scheduled"


def test_cron_remove(ctx, isolated_home):
    from opencomputer.cron.jobs import create_job, get_job
    job = create_job(schedule="every 1h", skill="x")
    _handle_cron_inline(ctx, ["remove", job["id"]])
    assert get_job(job["id"]) is None


def test_cron_help(ctx, isolated_home):
    res = _handle_cron_inline(ctx, ["help"])
    assert res.handled


def test_cron_unknown_subcommand(ctx, isolated_home):
    res = _handle_cron_inline(ctx, ["fakecmd"])
    assert res.handled


def test_cron_edit_schedule(ctx, isolated_home):
    from opencomputer.cron.jobs import create_job, get_job
    job = create_job(schedule="every 1h", skill="x")
    res = _handle_cron_inline(ctx, ["edit", job["id"], "--schedule", "every", "4h"])
    assert res.handled
    assert get_job(job["id"])["schedule"]["display"] == "every 240m"


def test_cron_edit_skill_replace(ctx, isolated_home):
    from opencomputer.cron.jobs import create_job, get_job
    job = create_job(schedule="every 1h", skill="x")
    res = _handle_cron_inline(ctx, ["edit", job["id"], "--skill", "y"])
    assert res.handled
    updated = get_job(job["id"])
    assert updated["skills"] == ["y"]


def test_cron_edit_add_skill(ctx, isolated_home):
    from opencomputer.cron.jobs import create_job, get_job
    job = create_job(schedule="every 1h", skills=["a"])
    res = _handle_cron_inline(ctx, ["edit", job["id"], "--add-skill", "b"])
    assert res.handled
    assert get_job(job["id"])["skills"] == ["a", "b"]


def test_cron_edit_clear_skills(ctx, isolated_home):
    from opencomputer.cron.jobs import create_job, get_job
    job = create_job(schedule="every 1h", skills=["a", "b"])
    res = _handle_cron_inline(ctx, ["edit", job["id"], "--clear-skills"])
    assert res.handled
    assert not get_job(job["id"]).get("skills")


def test_cron_edit_no_args_prints_usage(ctx, isolated_home):
    res = _handle_cron_inline(ctx, ["edit"])
    assert res.handled


def test_cron_edit_unknown_id(ctx, isolated_home):
    res = _handle_cron_inline(ctx, ["edit", "nonexistent", "--prompt", "x"])
    assert res.handled


def test_cron_edit_invalid_notify_rejected(ctx, isolated_home):
    from opencomputer.cron.jobs import create_job, get_job
    job = create_job(schedule="every 1h", skill="x")
    res = _handle_cron_inline(ctx, ["edit", job["id"], "--notify", "made_up:1"])
    assert res.handled
    # Job should be unchanged.
    assert get_job(job["id"])["notify"] is None
