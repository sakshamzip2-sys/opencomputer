"""Tests for profile.yaml flock concurrency protection."""

from __future__ import annotations

import threading

import pytest
import yaml

from opencomputer.profiles_lock import profile_yaml_lock


def test_lock_serializes_concurrent_writes(tmp_path):
    """Two threads each appending a unique key — both must land."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    yaml_path = profile_dir / "profile.yaml"
    yaml_path.write_text("plugins:\n  enabled: []\n")

    def append_key(key: str) -> None:
        with profile_yaml_lock(profile_dir):
            data = yaml.safe_load(yaml_path.read_text())
            data["plugins"]["enabled"].append(key)
            yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))

    t1 = threading.Thread(target=append_key, args=("alpha",))
    t2 = threading.Thread(target=append_key, args=("beta",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    final = yaml.safe_load(yaml_path.read_text())
    assert "alpha" in final["plugins"]["enabled"]
    assert "beta" in final["plugins"]["enabled"]


def test_lock_releases_on_exception(tmp_path):
    """Lock must release even if the wrapped block raises."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    with pytest.raises(ValueError, match="boom"):
        with profile_yaml_lock(profile_dir):
            raise ValueError("boom")

    # Subsequent acquire must succeed (no orphaned lock)
    with profile_yaml_lock(profile_dir):
        pass


def test_lock_creates_dotfile(tmp_path):
    """The lock file should be .profile.lock (hidden, separate from profile.yaml)."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    with profile_yaml_lock(profile_dir):
        assert (profile_dir / ".profile.lock").exists()


def test_lock_no_fcntl_falls_back(tmp_path, monkeypatch):
    """When fcntl is None (Windows), the lock should still be a no-op (or msvcrt)."""
    import opencomputer.profiles_lock as mod

    monkeypatch.setattr(mod, "fcntl", None)
    monkeypatch.setattr(mod, "msvcrt", None)

    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    # Should not raise even with no locking primitive available
    with profile_yaml_lock(profile_dir):
        pass
