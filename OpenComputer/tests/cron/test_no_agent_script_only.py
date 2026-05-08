"""Hermes parity (2026-05-08): cron --no-agent / --script script-only mode."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from opencomputer.cron import jobs as jobs_mod
from opencomputer.cron.scheduler import SILENT_MARKER, _run_script_only


@pytest.fixture
def scripts_dir(tmp_path, monkeypatch):
    """Pin OPENCOMPUTER_HOME so scripts dir is predictable."""
    fake_home = tmp_path / ".opencomputer"
    fake_home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(fake_home))
    sd = fake_home / "scripts"
    sd.mkdir()
    return sd


def _make_script(scripts_dir: Path, name: str, body: str) -> Path:
    path = scripts_dir / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.mark.asyncio
async def test_script_success_returns_stdout(scripts_dir):
    _make_script(scripts_dir, "ok.sh", "#!/usr/bin/env bash\necho 'hello world'\n")
    job = {
        "id": "j1", "name": "test", "schedule_display": "every 1m",
        "no_agent": True, "script": "ok.sh",
        "script_timeout_seconds": 5,
        "workdir": None,
    }
    success, doc, response, error = await _run_script_only(job)
    assert success is True
    assert "hello world" in response
    assert error is None


@pytest.mark.asyncio
async def test_script_empty_stdout_silent_tick(scripts_dir):
    _make_script(scripts_dir, "silent.sh", "#!/usr/bin/env bash\nexit 0\n")
    job = {
        "id": "j2", "name": "watchdog", "schedule_display": "every 5m",
        "no_agent": True, "script": "silent.sh",
        "script_timeout_seconds": 5,
        "workdir": None,
    }
    success, doc, response, error = await _run_script_only(job)
    assert success is True
    assert response == SILENT_MARKER  # silent tick — caller suppresses
    assert error is None


@pytest.mark.asyncio
async def test_script_nonzero_exit_returns_error(scripts_dir):
    _make_script(scripts_dir, "fail.sh", "#!/usr/bin/env bash\necho 'oops'\nexit 1\n")
    job = {
        "id": "j3", "name": "fail", "schedule_display": "once",
        "no_agent": True, "script": "fail.sh",
        "script_timeout_seconds": 5,
        "workdir": None,
    }
    success, doc, response, error = await _run_script_only(job)
    assert success is False
    assert "exited 1" in error
    assert "oops" in response  # output preserved for diagnostics


@pytest.mark.asyncio
async def test_script_missing_returns_error(scripts_dir):
    job = {
        "id": "j4", "name": "miss", "schedule_display": "once",
        "no_agent": True, "script": "nonexistent.sh",
        "script_timeout_seconds": 5,
        "workdir": None,
    }
    success, doc, response, error = await _run_script_only(job)
    assert success is False
    assert "not found" in error


@pytest.mark.asyncio
async def test_script_timeout_killed(scripts_dir):
    _make_script(scripts_dir, "hang.sh", "#!/usr/bin/env bash\nsleep 30\n")
    job = {
        "id": "j5", "name": "hang", "schedule_display": "once",
        "no_agent": True, "script": "hang.sh",
        "script_timeout_seconds": 1,
        "workdir": None,
    }
    success, doc, response, error = await _run_script_only(job)
    assert success is False
    assert "exceeded" in error and "1" in error


@pytest.mark.asyncio
async def test_script_no_script_field_returns_error(scripts_dir):
    job = {
        "id": "j6", "name": "bad", "schedule_display": "once",
        "no_agent": True, "script": None,
        "workdir": None,
    }
    success, doc, response, error = await _run_script_only(job)
    assert success is False
    assert "no script supplied" in error


def test_create_job_no_agent_requires_script(tmp_path, monkeypatch):
    fake_home = tmp_path / ".opencomputer"
    fake_home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(fake_home))
    with pytest.raises(ValueError, match="requires --script"):
        jobs_mod.create_job(
            schedule="every 5m",
            no_agent=True,
        )


def test_create_job_no_agent_excludes_prompt(tmp_path, monkeypatch):
    fake_home = tmp_path / ".opencomputer"
    fake_home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(fake_home))
    with pytest.raises(ValueError, match="exclusive"):
        jobs_mod.create_job(
            schedule="every 5m",
            no_agent=True,
            script="x.sh",
            prompt="hello",
        )


def test_create_job_no_agent_excludes_skill(tmp_path, monkeypatch):
    fake_home = tmp_path / ".opencomputer"
    fake_home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(fake_home))
    with pytest.raises(ValueError, match="exclusive"):
        jobs_mod.create_job(
            schedule="every 5m",
            no_agent=True,
            script="x.sh",
            skill="some-skill",
        )


def test_create_job_no_agent_persists_fields(tmp_path, monkeypatch):
    fake_home = tmp_path / ".opencomputer"
    fake_home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(fake_home))
    job = jobs_mod.create_job(
        schedule="every 5m",
        no_agent=True,
        script="watchdog.sh",
        script_timeout_seconds=300,
        name="my-watchdog",
    )
    assert job["no_agent"] is True
    assert job["script"] == "watchdog.sh"
    assert job["script_timeout_seconds"] == 300
    assert job["prompt"] is None
    assert job["skill"] is None
