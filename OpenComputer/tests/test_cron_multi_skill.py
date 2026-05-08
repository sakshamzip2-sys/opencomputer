"""Hermes parity: multiple skills per cron job."""
from __future__ import annotations

import pytest

from opencomputer.cron.jobs import create_job


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    yield tmp_path


def test_create_with_skills_list_persists(isolated_home):
    job = create_job(schedule="every 1h", skills=["blogwatcher", "maps"])
    assert job["skills"] == ["blogwatcher", "maps"]
    assert job["skill"] is None


def test_create_with_singular_skill_back_compat(isolated_home):
    job = create_job(schedule="every 1h", skill="blogwatcher")
    assert job["skill"] == "blogwatcher"
    assert job["skills"] is None


def test_create_with_both_prefers_skills_list(isolated_home):
    job = create_job(schedule="every 1h", skill="X", skills=["A", "B"])
    assert job["skills"] == ["A", "B"]
    # Singular cleared when plural supplied (no double-mention in prompt).
    assert job["skill"] is None


def test_build_run_prompt_multi_skill():
    from opencomputer.cron.scheduler import _build_run_prompt
    job = {"skills": ["blogwatcher", "maps"]}
    prompt = _build_run_prompt(job)
    assert "blogwatcher" in prompt
    assert "maps" in prompt
    assert "combine" in prompt.lower()


def test_build_run_prompt_single_skill_in_list():
    from opencomputer.cron.scheduler import _build_run_prompt
    job = {"skills": ["solo"]}
    prompt = _build_run_prompt(job)
    assert "solo" in prompt
    assert "combine" not in prompt.lower()


def test_build_run_prompt_singular_skill_back_compat():
    from opencomputer.cron.scheduler import _build_run_prompt
    job = {"skill": "legacy"}
    prompt = _build_run_prompt(job)
    assert "legacy" in prompt


def test_create_with_no_skill_or_prompt_or_skills_rejects(isolated_home):
    with pytest.raises(ValueError, match="prompt|skill|skills"):
        create_job(schedule="every 1h")
