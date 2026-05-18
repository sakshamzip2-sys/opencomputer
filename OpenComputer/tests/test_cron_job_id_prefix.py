"""Cron job-id *prefix* resolution.

``oc cron list`` / ``oc cron status`` display only the first 8 chars of
the 12-char job id, but ``oc cron get`` / ``pause`` / ``resume`` / ``run``
/ ``remove`` / ``edit`` did an *exact-match* lookup — so every id the CLI
showed you was rejected by every command that consumed one.

These tests lock the git-short-hash style prefix resolution that closes
the gap: any unique prefix (including the displayed 8-char form) resolves
to the full id; an ambiguous prefix resolves to ``None`` rather than
silently picking a job.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.cron import (
    create_job,
    get_job,
    jobs_file,
    pause_job,
    remove_job,
    resolve_job_id,
)


@pytest.fixture(autouse=True)
def isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test gets a fresh OPENCOMPUTER_HOME (isolated cron storage)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


def _make_job(name: str = "watcher") -> dict:
    """Create a job via the vetted ``skill`` path (skips the threat scan)."""
    return create_job(schedule="every 1h", skill="x", name=name)


class TestResolveJobId:
    def test_exact_id_resolves_to_itself(self) -> None:
        job = _make_job()
        assert resolve_job_id(job["id"]) == job["id"]

    def test_unique_8char_prefix_resolves(self) -> None:
        """The exact failure mode: the 8-char id shown by ``oc cron list``."""
        job = _make_job()
        short = job["id"][:8]
        assert short != job["id"]  # ids really are longer than 8 chars
        assert resolve_job_id(short) == job["id"]

    def test_unknown_prefix_returns_none(self) -> None:
        _make_job()
        assert resolve_job_id("ffffffffffff") is None

    def test_empty_prefix_returns_none(self) -> None:
        """An empty string must not resolve to the sole job."""
        _make_job()
        assert resolve_job_id("") is None

    def test_ambiguous_prefix_returns_none(self) -> None:
        """A prefix matching 2+ jobs resolves to None — never a silent pick."""
        _make_job("a")
        _make_job("b")
        raw = json.loads(jobs_file().read_text())
        raw["jobs"][0]["id"] = "ambig0000aaa"
        raw["jobs"][1]["id"] = "ambig0000bbb"
        jobs_file().write_text(json.dumps(raw))
        assert resolve_job_id("ambig0000") is None


class TestIdConsumingCommandsAcceptPrefix:
    """The user-visible payoff — the displayed 8-char id now works."""

    def test_get_job_by_8char_prefix(self) -> None:
        job = _make_job()
        fetched = get_job(job["id"][:8])
        assert fetched is not None
        assert fetched["id"] == job["id"]

    def test_pause_job_by_8char_prefix(self) -> None:
        job = _make_job()
        paused = pause_job(job["id"][:8])
        assert paused is not None
        assert paused["state"] == "paused"

    def test_remove_job_by_8char_prefix(self) -> None:
        job = _make_job()
        assert remove_job(job["id"][:8]) is True
        assert get_job(job["id"]) is None
