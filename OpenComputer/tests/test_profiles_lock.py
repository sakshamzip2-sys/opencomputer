"""Tests for the generic ``file_lock`` advisory file-lock contextmanager.

``profile_yaml_lock`` (tested in ``tests/test_profile_yaml_flock.py``) now
delegates to ``file_lock``; these tests exercise the generic primitive
directly — arbitrary lock path, parent-dir auto-creation, release on
exception, and the no-op fallback when neither fcntl nor msvcrt exists.
"""

from __future__ import annotations

import threading

import pytest

from opencomputer.profiles_lock import file_lock


def test_file_lock_acquires_and_creates_lock_file(tmp_path):
    """The lock file is created inside the block and survives after it."""
    lock_path = tmp_path / ".some.lock"
    assert not lock_path.exists()
    with file_lock(lock_path):
        assert lock_path.exists()
    # File persists (only the advisory lock is released).
    assert lock_path.exists()


def test_file_lock_creates_missing_parent_dir(tmp_path):
    """A lock path under a not-yet-existing directory is handled."""
    lock_path = tmp_path / "nested" / "deeper" / ".x.lock"
    assert not lock_path.parent.exists()
    with file_lock(lock_path):
        assert lock_path.exists()
    assert lock_path.parent.is_dir()


def test_file_lock_releases_on_exception(tmp_path):
    """The lock releases even when the wrapped block raises."""
    lock_path = tmp_path / ".boom.lock"

    with pytest.raises(ValueError, match="boom"):
        with file_lock(lock_path):
            raise ValueError("boom")

    # A subsequent acquire must succeed — no orphaned lock.
    with file_lock(lock_path):
        pass


def test_file_lock_serializes_concurrent_writers(tmp_path):
    """Two threads each appending under the lock — both writes land."""
    lock_path = tmp_path / ".counter.lock"
    data_path = tmp_path / "data.txt"
    data_path.write_text("")

    def append(token: str) -> None:
        with file_lock(lock_path):
            current = data_path.read_text()
            data_path.write_text(current + token)

    t1 = threading.Thread(target=append, args=("a",))
    t2 = threading.Thread(target=append, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    final = data_path.read_text()
    assert sorted(final) == ["a", "b"]


def test_file_lock_no_locking_primitive_is_noop(tmp_path, monkeypatch):
    """When fcntl AND msvcrt are both None the lock is a documented no-op."""
    import opencomputer.profiles_lock as mod

    monkeypatch.setattr(mod, "fcntl", None)
    monkeypatch.setattr(mod, "msvcrt", None)

    lock_path = tmp_path / ".noop.lock"
    # Must not raise even with no locking primitive available.
    with file_lock(lock_path):
        assert lock_path.exists()
