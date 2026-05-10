"""Tests for credential_pool live-state-file (Phase 5 — close honest deferrals)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.agent.credential_pool import (
    CredentialPool,
    read_all_pool_states,
)


async def test_state_file_written_on_acquire(tmp_path: Path):
    state_file = tmp_path / "auth_pool_test.json"
    pool = CredentialPool(
        keys=["k-one", "k-two"],
        state_file=str(state_file),
        provider_label="test_provider",
    )
    # Constructor should initialize the file with all-healthy state.
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert data["provider"] == "test_provider"
    assert data["size"] == 2
    assert all(not k["quarantined"] for k in data["keys"])

    # Acquire updates use_count and rewrites the file.
    await pool.acquire()
    data2 = json.loads(state_file.read_text())
    assert sum(k["use_count"] for k in data2["keys"]) == 1


async def test_state_file_reflects_quarantine(tmp_path: Path):
    state_file = tmp_path / "auth_pool_test.json"
    pool = CredentialPool(
        keys=["bad", "good"],
        state_file=str(state_file),
        provider_label="quarantine_test",
    )
    await pool.report_auth_failure("bad", reason="401")

    data = json.loads(state_file.read_text())
    bad_state = next(
        k for k in data["keys"] if k["last_failure_reason"] == "401"
    )
    assert bad_state["quarantined"] is True
    assert bad_state["quarantine_remaining_s"] > 0


def test_state_file_default_off(tmp_path: Path):
    """No state_file kwarg → no file written."""
    state_file = tmp_path / "should_not_exist.json"
    CredentialPool(keys=["k1"])
    assert not state_file.exists()


async def test_state_file_unwritable_does_not_break_pool(tmp_path: Path):
    """A write failure must not break acquire/report."""
    # Use a path inside a non-existent dir, then make parent unwritable.
    bad_dir = tmp_path / "ro"
    bad_dir.mkdir()
    bad_file = bad_dir / "auth_pool_x.json"
    # First write works (mkdir succeeds).
    pool = CredentialPool(
        keys=["k1"], state_file=str(bad_file), provider_label="unwritable"
    )
    # Now make dir read-only and verify acquire still succeeds.
    import os

    os.chmod(bad_dir, 0o500)
    try:
        # Should not raise — state-write swallowed.
        key = await pool.acquire()
        assert key == "k1"
    finally:
        os.chmod(bad_dir, 0o700)


def test_read_all_pool_states_empty_dir(tmp_path: Path):
    assert read_all_pool_states(str(tmp_path)) == []


async def test_read_all_pool_states_finds_files(tmp_path: Path):
    p1 = tmp_path / "auth_pool_anthropic.json"
    p2 = tmp_path / "auth_pool_openai.json"
    CredentialPool(
        keys=["a"], state_file=str(p1), provider_label="anthropic"
    )
    CredentialPool(
        keys=["b", "c"], state_file=str(p2), provider_label="openai"
    )

    states = read_all_pool_states(str(tmp_path))
    assert len(states) == 2
    providers = {s["provider"] for s in states}
    assert providers == {"anthropic", "openai"}


def test_read_all_pool_states_skips_bad_json(tmp_path: Path):
    (tmp_path / "auth_pool_broken.json").write_text("not-json{{{")
    (tmp_path / "auth_pool_ok.json").write_text(
        '{"provider": "ok", "size": 1, "keys": []}'
    )
    states = read_all_pool_states(str(tmp_path))
    # Only the parseable file is returned.
    assert len(states) == 1
    assert states[0]["provider"] == "ok"
