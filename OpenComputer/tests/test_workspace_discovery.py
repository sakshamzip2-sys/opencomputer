"""Tests for opencomputer.workspace.discovery — workspace-dir resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from opencomputer.workspace.discovery import (
    WorkspaceNotFoundError,
    discover_workspace_dir,
    is_valid_workspace_dir,
)


def _make_fake_workspace(root: Path) -> Path:
    """Create a directory that looks like a hermes-workspace checkout."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text('{"name": "fake"}', encoding="utf-8")
    (root / "server-entry.js").write_text("// fake", encoding="utf-8")
    return root


def test_is_valid_workspace_dir_accepts_full_marker(tmp_path: Path) -> None:
    ws = _make_fake_workspace(tmp_path / "ws")
    assert is_valid_workspace_dir(ws) is True


def test_is_valid_workspace_dir_rejects_missing_server_entry(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text("{}", encoding="utf-8")
    assert is_valid_workspace_dir(ws) is False


def test_is_valid_workspace_dir_rejects_missing_package_json(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "server-entry.js").write_text("//", encoding="utf-8")
    assert is_valid_workspace_dir(ws) is False


def test_is_valid_workspace_dir_rejects_file(tmp_path: Path) -> None:
    f = tmp_path / "not-a-dir"
    f.write_text("hi", encoding="utf-8")
    assert is_valid_workspace_dir(f) is False


def test_is_valid_workspace_dir_rejects_non_path() -> None:
    # Defensive: any non-Path goes through is_dir() and returns False.
    assert is_valid_workspace_dir("not-a-path") is False  # type: ignore[arg-type]


def test_explicit_arg_wins(tmp_path: Path) -> None:
    explicit = _make_fake_workspace(tmp_path / "explicit")
    profile_home = _make_fake_workspace(tmp_path / "profile" / "workspace").parent

    result = discover_workspace_dir(
        explicit=str(explicit),
        profile_home=profile_home,
        env={},
    )
    assert result == explicit.resolve()


def test_explicit_arg_invalid_raises_value_error(tmp_path: Path) -> None:
    bad = tmp_path / "does-not-exist"
    with pytest.raises(ValueError, match="not a valid hermes-workspace"):
        discover_workspace_dir(explicit=str(bad), env={})


def test_env_var_used_when_no_explicit(tmp_path: Path) -> None:
    ws = _make_fake_workspace(tmp_path / "from-env")
    result = discover_workspace_dir(
        explicit=None,
        profile_home=tmp_path / "no-such-profile",
        env={"OC_WORKSPACE_DIR": str(ws)},
    )
    assert result == ws.resolve()


def test_env_var_invalid_raises_value_error(tmp_path: Path) -> None:
    bad = tmp_path / "no-such-dir"
    with pytest.raises(ValueError, match=r"\$OC_WORKSPACE_DIR=.* is not a valid"):
        discover_workspace_dir(
            explicit=None,
            profile_home=tmp_path / "no-profile",
            env={"OC_WORKSPACE_DIR": str(bad)},
        )


def test_profile_home_wins_over_global(tmp_path: Path) -> None:
    profile = _make_fake_workspace(tmp_path / "profile" / "workspace").parent
    # Make a fake ~/.opencomputer/workspace too — the function should
    # pick the profile-local one.
    home_workspace = _make_fake_workspace(tmp_path / "fake-home" / ".opencomputer" / "workspace")
    monkeypatch_home = None
    try:
        # Reroute Path.home() so we don't touch the real homedir.
        monkeypatch_home = Path.home  # type: ignore[assignment]

        def _fake_home() -> Path:
            return tmp_path / "fake-home"

        Path.home = staticmethod(_fake_home)  # type: ignore[assignment]
        result = discover_workspace_dir(
            explicit=None,
            profile_home=profile,
            env={},
        )
        assert result == (profile / "workspace").resolve()
        assert result != home_workspace.resolve()
    finally:
        if monkeypatch_home is not None:
            Path.home = monkeypatch_home  # type: ignore[assignment]


def test_failure_lists_all_searched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "no-such-profile"
    # Reroute Path.home so global candidate misses too.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "no-such-home"))
    # Force the in-repo workspace candidate to a non-existent path —
    # OpenComputer/oc-workspace/ now exists in every source checkout, so
    # without this the discovery would succeed instead of failing.
    monkeypatch.setattr(
        "opencomputer.workspace.discovery.IN_REPO_WORKSPACE_PATH",
        tmp_path / "no-such-in-repo-workspace",
    )

    with pytest.raises(WorkspaceNotFoundError) as exc_info:
        discover_workspace_dir(
            explicit=None,
            profile_home=profile,
            env={},
        )

    searched = exc_info.value.searched
    # Should include profile-local, global, and the dev fallback.
    assert any("no-such-profile" in str(p) for p in searched)
    assert any(".opencomputer/workspace" in str(p) for p in searched)
    assert any("no-such-in-repo-workspace" in str(p) for p in searched)


def test_empty_env_var_treated_as_unset(tmp_path: Path) -> None:
    """OC_WORKSPACE_DIR='' should be treated as 'not set', not crash."""
    profile_ws = _make_fake_workspace(tmp_path / "profile" / "workspace")
    profile = profile_ws.parent
    result = discover_workspace_dir(
        explicit=None,
        profile_home=profile,
        env={"OC_WORKSPACE_DIR": ""},
    )
    assert result == profile_ws.resolve()


def test_empty_explicit_treated_as_unset(tmp_path: Path) -> None:
    """--workspace-dir '' should fall through to discovery."""
    profile_ws = _make_fake_workspace(tmp_path / "profile" / "workspace")
    profile = profile_ws.parent
    result = discover_workspace_dir(
        explicit="",
        profile_home=profile,
        env={},
    )
    assert result == profile_ws.resolve()
