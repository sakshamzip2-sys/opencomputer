"""Tests for installed_index.py — per-profile installed-plugin metadata."""

from __future__ import annotations

from pathlib import Path

from opencomputer.plugins.installed_index import (
    InstalledRecord,
    find_record,
    read_index,
    record_install,
    remove_install,
)


def test_record_and_read_roundtrip(tmp_path: Path):
    index_path = tmp_path / ".installed_index.json"
    record_install(
        index_path,
        InstalledRecord(
            plugin_id="example",
            version="0.1.0",
            source="git",
            source_url="git+https://github.com/x/y.git",
            source_ref="abc123",
            tarball_sha256=None,
            installed_at=1700000000,
        ),
    )
    records = read_index(index_path)
    assert len(records) == 1
    r = records[0]
    assert r.plugin_id == "example"
    assert r.source == "git"
    assert r.source_ref == "abc123"


def test_record_overwrites_existing(tmp_path: Path):
    index_path = tmp_path / ".installed_index.json"
    record_install(
        index_path,
        InstalledRecord("p", "0.1.0", "catalog", "p", None, "abc", 100),
    )
    record_install(
        index_path,
        InstalledRecord("p", "0.2.0", "catalog", "p", None, "def", 200),
    )
    records = read_index(index_path)
    assert len(records) == 1
    assert records[0].version == "0.2.0"
    assert records[0].tarball_sha256 == "def"


def test_remove_install(tmp_path: Path):
    index_path = tmp_path / ".installed_index.json"
    record_install(
        index_path, InstalledRecord("a", "0.1.0", "catalog", "a", None, "x", 0)
    )
    record_install(
        index_path, InstalledRecord("b", "0.1.0", "catalog", "b", None, "y", 0)
    )
    remove_install(index_path, "a")
    records = read_index(index_path)
    assert {r.plugin_id for r in records} == {"b"}


def test_read_missing_file_returns_empty(tmp_path: Path):
    assert read_index(tmp_path / "does-not-exist.json") == []


def test_find_record_returns_none_when_absent(tmp_path: Path):
    assert find_record(tmp_path / "noop.json", "ghost") is None


def test_find_record_returns_record(tmp_path: Path):
    index_path = tmp_path / ".installed_index.json"
    record_install(
        index_path,
        InstalledRecord("p", "0.1.0", "catalog", "p", None, "abc", 100),
    )
    rec = find_record(index_path, "p")
    assert rec is not None
    assert rec.plugin_id == "p"
