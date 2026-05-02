"""Tests for the `oc update` CLI subcommand.

The subcommand is a typer-wrapped action that detects pip-vs-git
install and either prints the upgrade command (PyPI) or runs
``git fetch`` + ``git pull --ff-only`` (git checkout).
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli import app


@pytest.fixture
def runner():
    return CliRunner()


def test_pypi_install_prints_pip_upgrade_command(runner, tmp_path, monkeypatch):
    """When .git is missing (PyPI install), command MUST print the
    `pip install -U opencomputer` upgrade command and exit cleanly.

    Trick: redirect cli's project_root to a tmp dir without .git.
    """
    monkeypatch.setattr(
        "opencomputer.cli.Path",
        _path_factory_returning(tmp_path / "synth-cli/cli.py"),
    )
    # tmp_path has no .git → triggers PyPI branch

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "pip install -U opencomputer" in result.output
    assert "PyPI" in result.output


def test_git_checkout_already_up_to_date(runner, tmp_path, monkeypatch):
    """git fetch shows 0 commits behind → ``Already up to date`` and no pull."""
    fake_repo = tmp_path / "checkout"
    (fake_repo / ".git").mkdir(parents=True)

    monkeypatch.setattr(
        "opencomputer.cli.Path",
        _path_factory_returning(fake_repo / "synth-cli/cli.py"),
    )

    pull_called = []

    def fake_run(cmd, **kw):
        if "fetch" in cmd:
            return MagicMock(returncode=0, stderr="")
        if "rev-list" in cmd:
            return MagicMock(returncode=0, stdout="0\n")
        if "pull" in cmd:
            pull_called.append(True)
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 0
    assert "Already up to date" in result.output
    assert pull_called == []


def test_git_checkout_pulls_when_commits_behind(runner, tmp_path, monkeypatch):
    """git fetch shows N>0 commits → pull and report success."""
    fake_repo = tmp_path / "checkout"
    (fake_repo / ".git").mkdir(parents=True)

    monkeypatch.setattr(
        "opencomputer.cli.Path",
        _path_factory_returning(fake_repo / "synth-cli/cli.py"),
    )

    def fake_run(cmd, **kw):
        if "fetch" in cmd:
            return MagicMock(returncode=0, stderr="")
        if "rev-list" in cmd:
            return MagicMock(returncode=0, stdout="3\n")
        if "pull" in cmd:
            return MagicMock(returncode=0, stderr="")
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 0
    assert "Found 3 new commit" in result.output
    assert "Updated to latest main" in result.output


def test_git_fetch_failure_exits_nonzero(runner, tmp_path, monkeypatch):
    """A failed `git fetch` must surface error + exit nonzero."""
    fake_repo = tmp_path / "checkout"
    (fake_repo / ".git").mkdir(parents=True)

    monkeypatch.setattr(
        "opencomputer.cli.Path",
        _path_factory_returning(fake_repo / "synth-cli/cli.py"),
    )

    def fake_run(cmd, **kw):
        if "fetch" in cmd:
            return MagicMock(
                returncode=1,
                stderr="fatal: Could not resolve host: github.com",
            )
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 1
    assert "fetch failed" in result.output
    assert "Could not resolve host" in result.output


def test_git_pull_diverged_exits_nonzero(runner, tmp_path, monkeypatch):
    """ff-only pull failure must NOT silently rebase — surface + exit 1."""
    fake_repo = tmp_path / "checkout"
    (fake_repo / ".git").mkdir(parents=True)

    monkeypatch.setattr(
        "opencomputer.cli.Path",
        _path_factory_returning(fake_repo / "synth-cli/cli.py"),
    )

    def fake_run(cmd, **kw):
        if "fetch" in cmd:
            return MagicMock(returncode=0, stderr="")
        if "rev-list" in cmd:
            return MagicMock(returncode=0, stdout="2\n")
        if "pull" in cmd:
            return MagicMock(
                returncode=1,
                stderr="fatal: Not possible to fast-forward, aborting.\n",
            )
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 1
    assert "Pull failed" in result.output
    assert "Resolve manually" in result.output


def test_git_command_timeout_exits_nonzero(runner, tmp_path, monkeypatch):
    """Timed-out git commands must NOT hang — propagate as an actionable error."""
    fake_repo = tmp_path / "checkout"
    (fake_repo / ".git").mkdir(parents=True)

    monkeypatch.setattr(
        "opencomputer.cli.Path",
        _path_factory_returning(fake_repo / "synth-cli/cli.py"),
    )

    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 30)

    with patch("subprocess.run", side_effect=fake_run):
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 1
    assert "timed out" in result.output


# --- helpers ----------------------------------------------------------------


def _path_factory_returning(synthetic_file):
    """Return a callable that, when invoked with the cli __file__, returns
    a `Path` whose ``.resolve().parents[1]`` is the fake project root.

    Other Path() calls (e.g. ``cfg.session.db_path``) flow through to the
    real ``pathlib.Path`` so unrelated code keeps working.
    """
    from pathlib import Path as _P

    real_path = _P
    synthetic = real_path(str(synthetic_file))

    def _factory(*args, **kwargs):
        if (
            len(args) == 1
            and isinstance(args[0], str)
            and args[0].endswith("cli.py")
            and not args[0].startswith(str(synthetic.parent.parent))
        ):
            return synthetic
        return real_path(*args, **kwargs)

    return _factory
