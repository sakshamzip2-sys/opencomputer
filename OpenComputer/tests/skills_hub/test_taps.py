"""Tests for TapsManager."""
import pytest

from opencomputer.skills_hub.taps import TapsManager


def test_empty_taps(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    assert mgr.list() == []


def test_add_tap(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    mgr.add("alice/skills")
    assert mgr.list() == ["alice/skills"]


def test_add_normalizes_url(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    mgr.add("https://github.com/alice/skills.git")
    assert mgr.list() == ["alice/skills"]


def test_add_normalizes_url_without_git_suffix(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    mgr.add("https://github.com/alice/skills")
    assert mgr.list() == ["alice/skills"]


def test_remove_tap(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    mgr.add("alice/skills")
    mgr.add("bob/skills")
    mgr.remove("alice/skills")
    assert mgr.list() == ["bob/skills"]


def test_add_duplicate_is_idempotent(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    mgr.add("alice/skills")
    mgr.add("alice/skills")
    assert mgr.list() == ["alice/skills"]


def test_invalid_repo_form_rejected(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    with pytest.raises(ValueError):
        mgr.add("not-a-valid-repo")


def test_taps_persist_across_instances(tmp_path):
    p = tmp_path / "taps.json"
    TapsManager(p).add("alice/skills")
    assert TapsManager(p).list() == ["alice/skills"]
