"""Scoped lock for cross-process resource exclusion (hermes parity).

Pinned to the v2026.4.26 Telegram E2E incident: two clients polling the
same bot token silently lost messages because no lock prevented the
collision. The new ``opencomputer.security.scope_lock`` makes the
collision a clear refusal at startup.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_lock_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENCOMPUTER_LOCK_DIR", str(tmp_path / "locks"))


def test_acquire_returns_true_when_no_holder() -> None:
    from opencomputer.security.scope_lock import acquire_scoped_lock

    ok, prior = acquire_scoped_lock("test-scope", "identity-A")
    assert ok is True
    assert prior is None


def test_acquire_returns_false_when_another_pid_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate a different live PID owning the lock — second acquire fails."""
    from opencomputer.security import scope_lock

    fake_pid = os.getpid() + 999_000
    lock_path = scope_lock._get_scope_lock_path("test-scope", "identity-A")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "pid": fake_pid,
                "start_time": None,
                "scope": "test-scope",
                "identity_hash": scope_lock._scope_hash("identity-A"),
                "metadata": {"note": "the holder"},
            }
        )
    )
    monkeypatch.setattr(scope_lock, "_is_pid_alive", lambda pid: pid == fake_pid)

    ok, holder = scope_lock.acquire_scoped_lock("test-scope", "identity-A")

    assert ok is False
    assert holder is not None
    assert holder["pid"] == fake_pid
    assert holder["metadata"] == {"note": "the holder"}


def test_acquire_clears_stale_lock_with_dead_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holder PID no longer exists → lock is stale, second process takes it."""
    from opencomputer.security import scope_lock

    dead_pid = os.getpid() + 999_001
    lock_path = scope_lock._get_scope_lock_path("test-scope", "identity-B")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": dead_pid, "start_time": None}))
    monkeypatch.setattr(scope_lock, "_is_pid_alive", lambda pid: pid != dead_pid)

    ok, prior = scope_lock.acquire_scoped_lock("test-scope", "identity-B")
    assert ok is True
    # New record on disk now points at us.
    on_disk = json.loads(lock_path.read_text())
    assert on_disk["pid"] == os.getpid()


def test_acquire_clears_corrupt_lock() -> None:
    """A truncated / non-JSON lock file is treated as stale."""
    from opencomputer.security import scope_lock

    lock_path = scope_lock._get_scope_lock_path("test-scope", "identity-C")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("{ broken json")

    ok, _prior = scope_lock.acquire_scoped_lock("test-scope", "identity-C")
    assert ok is True


def test_release_removes_our_lock() -> None:
    from opencomputer.security.scope_lock import (
        _get_scope_lock_path,
        acquire_scoped_lock,
        release_scoped_lock,
    )

    acquire_scoped_lock("test-scope", "identity-D")
    lock_path = _get_scope_lock_path("test-scope", "identity-D")
    assert lock_path.exists()

    release_scoped_lock("test-scope", "identity-D")
    assert not lock_path.exists()


def test_release_does_not_remove_someone_elses_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: release only deletes when the on-disk PID matches us."""
    from opencomputer.security import scope_lock

    other_pid = os.getpid() + 999_002
    lock_path = scope_lock._get_scope_lock_path("test-scope", "identity-E")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": other_pid, "start_time": None}))

    scope_lock.release_scoped_lock("test-scope", "identity-E")

    assert lock_path.exists(), "release must not delete a lock owned by another PID"


def test_release_is_idempotent_when_no_lock_exists() -> None:
    """Safe to call release in a finally block even when we never acquired."""
    from opencomputer.security.scope_lock import release_scoped_lock

    release_scoped_lock("test-scope", "never-acquired")  # must not raise


def test_lock_file_does_not_contain_plaintext_identity() -> None:
    """The bot token must not appear on disk — only its sha256 prefix."""
    from opencomputer.security.scope_lock import (
        _get_lock_dir,
        acquire_scoped_lock,
    )

    secret = "8626391590:AAHAGVlUA6tdS8UQ4JljZdKsZB1_yN1q5GY"
    acquire_scoped_lock("telegram-bot-token", secret)

    lock_dir = _get_lock_dir()
    for lock_file in lock_dir.iterdir():
        text = lock_file.read_text()
        assert secret not in text, "lock file leaked the raw identity"
        assert "AAHAGVlU" not in text, "lock file leaked a substring of the identity"


def test_re_acquire_by_same_process_is_idempotent() -> None:
    """A process that calls acquire twice with the same identity gets True both times."""
    from opencomputer.security.scope_lock import acquire_scoped_lock

    ok1, _ = acquire_scoped_lock("test-scope", "identity-F")
    ok2, prior = acquire_scoped_lock("test-scope", "identity-F")

    assert ok1 is True
    assert ok2 is True
    assert prior is not None and prior["pid"] == os.getpid()
