"""Tests for the Skills Hub lockfile (tracks installed skills + versions)."""
import json

import pytest

from opencomputer.skills_hub.lockfile import HubLockFile, LockEntry


def test_empty_lockfile_starts_with_no_entries(tmp_path):
    lf = HubLockFile(tmp_path / "lockfile.json")
    assert lf.list() == []


def test_record_install_adds_entry(tmp_path):
    lf = HubLockFile(tmp_path / "lockfile.json")
    lf.record_install(
        identifier="well-known/pead-screener",
        version="1.0.0",
        source="well-known",
        install_path="well-known/pead-screener",
        sha256="abc123",
    )
    entries = lf.list()
    assert len(entries) == 1
    assert entries[0].identifier == "well-known/pead-screener"
    assert entries[0].version == "1.0.0"


def test_lockfile_persists_to_disk(tmp_path):
    p = tmp_path / "lockfile.json"
    lf1 = HubLockFile(p)
    lf1.record_install(
        identifier="well-known/foo",
        version="0.1.0",
        source="well-known",
        install_path="well-known/foo",
        sha256="x",
    )
    lf2 = HubLockFile(p)
    assert len(lf2.list()) == 1


def test_uninstall_removes_entry(tmp_path):
    lf = HubLockFile(tmp_path / "lockfile.json")
    lf.record_install("well-known/foo", "0.1.0", "well-known", "well-known/foo", "x")
    lf.record_uninstall("well-known/foo")
    assert lf.list() == []


def test_get_returns_entry(tmp_path):
    lf = HubLockFile(tmp_path / "lockfile.json")
    lf.record_install("well-known/foo", "0.1.0", "well-known", "well-known/foo", "abc")
    entry = lf.get("well-known/foo")
    assert entry is not None
    assert entry.sha256 == "abc"


def test_get_missing_returns_none(tmp_path):
    lf = HubLockFile(tmp_path / "lockfile.json")
    assert lf.get("well-known/nope") is None


def test_concurrent_writes_serialize(tmp_path):
    """Two HubLockFile instances writing should not corrupt each other."""
    p = tmp_path / "lockfile.json"
    lf1 = HubLockFile(p)
    lf2 = HubLockFile(p)
    lf1.record_install("well-known/a", "1.0", "well-known", "well-known/a", "x")
    lf2.record_install("well-known/b", "1.0", "well-known", "well-known/b", "y")
    lf3 = HubLockFile(p)
    ids = sorted(e.identifier for e in lf3.list())
    assert ids == ["well-known/a", "well-known/b"]


def test_corrupt_lockfile_raises_clear_error(tmp_path):
    p = tmp_path / "lockfile.json"
    p.write_text("{not json")
    with pytest.raises(ValueError, match="lockfile.*corrupt"):
        HubLockFile(p).list()


def test_double_install_replaces_entry(tmp_path):
    lf = HubLockFile(tmp_path / "lockfile.json")
    lf.record_install("well-known/foo", "0.1.0", "well-known", "well-known/foo", "x1")
    lf.record_install("well-known/foo", "0.2.0", "well-known", "well-known/foo", "x2")
    entries = lf.list()
    assert len(entries) == 1
    assert entries[0].version == "0.2.0"
    assert entries[0].sha256 == "x2"


def test_lock_entry_is_immutable(tmp_path):
    lf = HubLockFile(tmp_path / "lockfile.json")
    lf.record_install("well-known/foo", "1.0.0", "well-known", "well-known/foo", "x")
    entry = lf.list()[0]
    with pytest.raises((AttributeError, TypeError)):
        entry.version = "2.0.0"  # type: ignore[misc]
