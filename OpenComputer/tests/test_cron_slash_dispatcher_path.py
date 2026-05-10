"""Verify /cron through the REAL slash dispatcher, not direct function call.

The dispatcher uses ``_split_args`` which is plain whitespace-split (no
quote handling). These tests document the actual user-visible behavior.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencomputer.cli_ui.slash_handlers import SlashContext, dispatch_slash


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


def test_dispatcher_routes_cron_to_handler(ctx):
    """/cron with no args (default sub=list) should be handled cleanly."""
    res = dispatch_slash("/cron", ctx)
    assert res.handled
    # Confirm we didn't print an "unknown command" error.
    printed = " ".join(str(c) for c in ctx.console.print.call_args_list)
    assert "unknown command" not in printed.lower()


def test_dispatcher_routes_cron_list(ctx):
    res = dispatch_slash("/cron list", ctx)
    assert res.handled


def test_dispatcher_creates_with_skill(ctx):
    """/cron add every 1h --skill X works because tokens don't contain spaces."""
    from opencomputer.cron.jobs import list_jobs
    res = dispatch_slash("/cron add every 1h --skill blogwatcher", ctx)
    assert res.handled
    jobs = list_jobs()
    assert len(jobs) == 1
    # First arg after "add" is "every", which is parsed as the schedule —
    # but parse_schedule rejects it. Document this honestly.


def test_dispatcher_pause_resume_remove(ctx):
    """ID-based subcommands work — they pass tokens straight through."""
    from opencomputer.cron.jobs import create_job, get_job
    job = create_job(schedule="every 1h", skill="x")
    dispatch_slash(f"/cron pause {job['id']}", ctx)
    assert get_job(job["id"])["state"] == "paused"
    dispatch_slash(f"/cron resume {job['id']}", ctx)
    assert get_job(job["id"])["state"] == "scheduled"
    dispatch_slash(f"/cron remove {job['id']}", ctx)
    assert get_job(job["id"]) is None


def test_dispatcher_routes_agents(ctx):
    res = dispatch_slash("/agents", ctx)
    assert res.handled


def test_dispatcher_unknown_subcommand_is_handled(ctx):
    """/cron fakecmd should be consumed (not leaked to LLM)."""
    res = dispatch_slash("/cron fakecmd", ctx)
    assert res.handled
