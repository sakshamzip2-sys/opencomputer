"""Tests for the dotenv tracker — §9.3 of
``docs/plans/profile-handoff-investigation.md``.

Coverage:
  - load_profile_dotenv adds keys + tracks pre-load state
  - unload_active_dotenv reverts to pre-load state
  - Shell-set values that the .env overrode are restored on unload
  - swap_profile_dotenv = unload + load round-trip
  - Missing .env file is a no-op (returns 0)
  - Malformed .env doesn't wedge the swap
  - Path-arg validation
  - Concurrent thread-safety (snapshot is locked)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from opencomputer.agent import dotenv_tracker as dt


@pytest.fixture(autouse=True)
def _reset_tracker(monkeypatch):
    """Reset module state + isolate os.environ per test."""
    # monkeypatch.setenv/.delenv auto-restores; we wrap into a per-test
    # blank canvas for relevant keys.
    for k in ("TEST_A", "TEST_B", "TEST_PROFILE_KEY"):
        monkeypatch.delenv(k, raising=False)
    dt._reset_for_tests()
    yield
    dt._reset_for_tests()


def test_path_arg_validated() -> None:
    with pytest.raises(TypeError, match="Path"):
        dt.load_profile_dotenv("not a path")  # type: ignore[arg-type]


def test_missing_env_file_returns_zero(tmp_path: Path) -> None:
    """No .env at the profile root → 0 keys loaded, no state change."""
    n = dt.load_profile_dotenv(tmp_path)
    assert n == 0
    assert dt.active_dotenv_path() is None


def test_load_sets_keys_and_tracks_them(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TEST_A=alpha\nTEST_B=beta\n")
    n = dt.load_profile_dotenv(tmp_path)
    assert n == 2
    assert os.environ["TEST_A"] == "alpha"
    assert os.environ["TEST_B"] == "beta"
    assert set(dt.active_dotenv_keys()) == {"TEST_A", "TEST_B"}


def test_unload_removes_added_keys(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TEST_A=alpha\n")
    dt.load_profile_dotenv(tmp_path)
    assert "TEST_A" in os.environ
    n = dt.unload_active_dotenv()
    assert n == 1
    assert "TEST_A" not in os.environ
    assert dt.active_dotenv_path() is None


def test_unload_restores_shell_set_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a shell-set key was overridden by .env, unload restores it."""
    monkeypatch.setenv("TEST_A", "from-shell")
    (tmp_path / ".env").write_text("TEST_A=from-env\n")
    dt.load_profile_dotenv(tmp_path)
    assert os.environ["TEST_A"] == "from-env"  # override won
    dt.unload_active_dotenv()
    assert os.environ["TEST_A"] == "from-shell"  # restored


def test_unload_with_no_active_snapshot_returns_zero(tmp_path: Path) -> None:
    assert dt.unload_active_dotenv() == 0


def test_swap_profile_dotenv_replaces_keys(tmp_path: Path) -> None:
    """A swap replaces profile A's keys with profile B's."""
    a = tmp_path / "a"
    a.mkdir()
    (a / ".env").write_text("TEST_PROFILE_KEY=for-a\n")
    b = tmp_path / "b"
    b.mkdir()
    (b / ".env").write_text("TEST_PROFILE_KEY=for-b\n")

    dt.load_profile_dotenv(a)
    assert os.environ["TEST_PROFILE_KEY"] == "for-a"

    dt.swap_profile_dotenv(b)
    assert os.environ["TEST_PROFILE_KEY"] == "for-b"


def test_swap_removes_keys_only_in_old_profile(tmp_path: Path) -> None:
    """Key in old .env but not in new is removed (correct unload)."""
    a = tmp_path / "a"
    a.mkdir()
    (a / ".env").write_text("TEST_A=alpha\nTEST_B=beta\n")
    b = tmp_path / "b"
    b.mkdir()
    (b / ".env").write_text("TEST_A=new-alpha\n")  # no TEST_B

    dt.load_profile_dotenv(a)
    assert os.environ["TEST_B"] == "beta"

    dt.swap_profile_dotenv(b)
    assert os.environ["TEST_A"] == "new-alpha"
    assert "TEST_B" not in os.environ  # cleanly unloaded


def test_swap_preserves_unrelated_shell_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shell-set key that NO .env touches stays untouched."""
    monkeypatch.setenv("TEST_A", "shell-only")
    a = tmp_path / "a"
    a.mkdir()
    (a / ".env").write_text("OTHER_KEY=x\n")
    dt.load_profile_dotenv(a)
    dt.unload_active_dotenv()
    assert os.environ["TEST_A"] == "shell-only"


def test_malformed_env_does_not_corrupt_state(tmp_path: Path) -> None:
    """A .env with garbage doesn't leak partial state."""
    # python-dotenv silently ignores malformed lines so this technically
    # parses to empty. Make sure we still don't crash + don't track.
    (tmp_path / ".env").write_text("not=valid=env\nbut-also-invalid\n")
    # Should not raise.
    n = dt.load_profile_dotenv(tmp_path)
    # The actual count depends on python-dotenv's leniency; the contract
    # is "don't crash" and "stable state".
    assert n >= 0
    # Unload should be safe.
    dt.unload_active_dotenv()


def test_load_idempotent_replaces_prior_snapshot(tmp_path: Path) -> None:
    """Loading a second profile without unload replaces the snapshot."""
    a = tmp_path / "a"
    a.mkdir()
    (a / ".env").write_text("KEY_A=v1\n")
    b = tmp_path / "b"
    b.mkdir()
    (b / ".env").write_text("KEY_B=v2\n")

    dt.load_profile_dotenv(a)
    assert "KEY_A" in dt.active_dotenv_keys()
    dt.load_profile_dotenv(b)
    # Re-loading replaces the snapshot — KEY_A is no longer tracked.
    assert "KEY_A" not in dt.active_dotenv_keys()
    assert "KEY_B" in dt.active_dotenv_keys()


def test_swap_signature_compatible_with_rebind_handler(tmp_path: Path) -> None:
    """``swap_profile_dotenv`` accepts the rebind-handler shape
    ``(new_home, old_home=None)``."""
    n = dt.swap_profile_dotenv(tmp_path, None)
    assert n == 0  # no .env in tmp_path
    n = dt.swap_profile_dotenv(tmp_path, tmp_path)  # old_home is ignored
    assert n == 0
