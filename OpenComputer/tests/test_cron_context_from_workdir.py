"""Tests for cron context_from chaining + per-job workdir (Wave 6.A).

Hermes-port (5ac536592 + 852c7f3be).

- ``context_from``: list of upstream job IDs whose last_response is
  prepended to this job's prompt.
- ``workdir``: per-job cwd applied for the agent run, restored after.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.cron.jobs import create_job, load_jobs, mark_job_run
from opencomputer.cron.scheduler import (
    _build_context_from_block,
    _build_run_prompt,
)


@pytest.fixture(autouse=True)
def _isolated_jobs_path(tmp_path, monkeypatch):
    """Each test gets a fresh jobs.json so we don't pollute each other."""
    monkeypatch.setattr(
        "opencomputer.cron.jobs.jobs_file", lambda: tmp_path / "jobs.json",
    )
    monkeypatch.setattr(
        "opencomputer.cron.jobs.cron_dir", lambda: tmp_path,
    )


# ─── create_job + persisted shape ────────────────────────────────────


def test_create_job_default_no_context_from(tmp_path):
    j = create_job(schedule="0 9 * * *", prompt="hi")
    assert j["context_from"] is None
    assert j["workdir"] is None
    assert j["last_response"] == ""


def test_create_job_with_context_from_and_workdir(tmp_path):
    j = create_job(
        schedule="0 9 * * *", prompt="follow up",
        context_from=["abc123"],
        workdir="/tmp/my-project",
    )
    assert j["context_from"] == ["abc123"]
    assert j["workdir"] == "/tmp/my-project"


# ─── _build_context_from_block ────────────────────────────────────────


def test_context_from_empty_returns_empty_string():
    job = {"context_from": None}
    assert _build_context_from_block(job) == ""
    job2 = {"context_from": []}
    assert _build_context_from_block(job2) == ""


def test_context_from_unknown_id_silently_skipped(tmp_path):
    create_job(schedule="0 9 * * *", prompt="job1")
    job = {"context_from": ["nonexistent-id"]}
    assert _build_context_from_block(job) == ""


def test_context_from_pulls_last_response(tmp_path):
    upstream = create_job(schedule="0 9 * * *", prompt="upstream", name="upstream")
    mark_job_run(upstream["id"], success=True, response="Result from upstream run.")
    job = {"context_from": [upstream["id"]]}
    block = _build_context_from_block(job)
    assert "Result from upstream run." in block
    assert "upstream" in block  # name surfaced


def test_context_from_multiple_upstream_jobs(tmp_path):
    a = create_job(schedule="0 9 * * *", prompt="a", name="A")
    b = create_job(schedule="0 9 * * *", prompt="b", name="B")
    mark_job_run(a["id"], success=True, response="alpha")
    mark_job_run(b["id"], success=True, response="bravo")
    job = {"context_from": [a["id"], b["id"]]}
    block = _build_context_from_block(job)
    assert "alpha" in block
    assert "bravo" in block


def test_context_from_skips_empty_responses(tmp_path):
    a = create_job(schedule="0 9 * * *", prompt="a", name="A")
    b = create_job(schedule="0 9 * * *", prompt="b", name="B")
    mark_job_run(a["id"], success=True, response="")  # empty
    mark_job_run(b["id"], success=True, response="non-empty")
    job = {"context_from": [a["id"], b["id"]]}
    block = _build_context_from_block(job)
    assert "non-empty" in block
    # No A block (it was empty)
    assert "id=" + b["id"] in block


def test_response_capped_at_8kb(tmp_path):
    j = create_job(schedule="0 9 * * *", prompt="x")
    big = "x" * 100_000
    mark_job_run(j["id"], success=True, response=big)
    jobs = load_jobs()
    saved = next(j for j in jobs if j["id"] == jobs[0]["id"])
    assert len(saved["last_response"]) == 8192


# ─── _build_run_prompt with context_from ──────────────────────────────


def test_prompt_includes_context_from_block(tmp_path):
    upstream = create_job(schedule="0 9 * * *", prompt="up", name="UP")
    mark_job_run(upstream["id"], success=True, response="prior result")
    job = {
        "prompt": "based on the above, what next?",
        "context_from": [upstream["id"]],
    }
    out = _build_run_prompt(job)
    assert "prior result" in out
    assert "based on the above" in out


def test_prompt_no_context_from_unchanged_shape(tmp_path):
    job = {"prompt": "plain prompt"}
    out = _build_run_prompt(job)
    assert "plain prompt" in out
    assert "[CONTEXT FROM" not in out


def test_skill_job_with_context_from(tmp_path):
    upstream = create_job(schedule="0 9 * * *", prompt="up")
    mark_job_run(upstream["id"], success=True, response="data")
    job = {
        "skill": "report-generator",
        "context_from": [upstream["id"]],
    }
    out = _build_run_prompt(job)
    assert "report-generator" in out
    assert "data" in out
