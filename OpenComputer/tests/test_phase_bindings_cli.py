"""Tests for `oc bindings` CLI subgroup (Phase 4).

Covers:
* round-trip through ``list / show / add / remove / set-default / test``
* concurrent-write flock test (3 subprocesses, none lost)

The CLI uses ``_home() / "bindings.yaml"``; tests redirect via
``OPENCOMPUTER_HOME`` env var so each test sees a fresh tmp_path.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.bindings_config import load_bindings
from opencomputer.cli_bindings import app as bindings_app

runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point _home() at a fresh tmp_path so each test sees an empty config."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_empty_shows_hint(self, isolated_home: Path) -> None:
        result = runner.invoke(bindings_app, ["list"])
        assert result.exit_code == 0
        assert "no bindings" in result.stdout.lower()

    def test_after_add_shows_table(self, isolated_home: Path) -> None:
        # Seed a binding via `add`.
        add = runner.invoke(
            bindings_app,
            ["add", "coding", "--platform", "telegram", "--chat-id", "12345"],
        )
        assert add.exit_code == 0, add.stdout

        result = runner.invoke(bindings_app, ["list"])
        assert result.exit_code == 0
        # Rich's table can wrap content, so check character-by-character.
        flat = "".join(c for c in result.stdout if c.isalnum() or c in "=,")
        assert "coding" in flat
        assert "telegram" in flat
        assert "12345" in flat


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


class TestShow:
    def test_default_output(self, isolated_home: Path) -> None:
        result = runner.invoke(bindings_app, ["show"])
        assert result.exit_code == 0
        assert "default" in result.stdout.lower()
        assert "0" in result.stdout  # binding count

    def test_after_add_count_one(self, isolated_home: Path) -> None:
        runner.invoke(
            bindings_app,
            ["add", "stock", "--platform", "telegram", "--chat-id", "67890"],
        )
        result = runner.invoke(bindings_app, ["show"])
        assert result.exit_code == 0
        assert "1" in result.stdout


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


class TestAdd:
    def test_round_trip_writes_yaml(self, isolated_home: Path) -> None:
        result = runner.invoke(
            bindings_app,
            [
                "add",
                "coding",
                "--platform",
                "telegram",
                "--chat-id",
                "99",
                "--priority",
                "10",
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert "added" in result.stdout.lower()

        # Verify on disk.
        cfg = load_bindings(isolated_home / "bindings.yaml")
        assert len(cfg.bindings) == 1
        b = cfg.bindings[0]
        assert b.profile == "coding"
        assert b.match.platform == "telegram"
        assert b.match.chat_id == "99"
        assert b.priority == 10

    def test_two_adds_keep_both(self, isolated_home: Path) -> None:
        runner.invoke(bindings_app, ["add", "a", "--platform", "telegram"])
        runner.invoke(bindings_app, ["add", "b", "--platform", "discord"])
        cfg = load_bindings(isolated_home / "bindings.yaml")
        assert {b.profile for b in cfg.bindings} == {"a", "b"}


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


class TestRemove:
    def test_remove_by_index(self, isolated_home: Path) -> None:
        runner.invoke(bindings_app, ["add", "first", "--platform", "telegram"])
        runner.invoke(bindings_app, ["add", "second", "--platform", "discord"])

        result = runner.invoke(bindings_app, ["remove", "0"])
        assert result.exit_code == 0
        assert "removed" in result.stdout.lower()

        cfg = load_bindings(isolated_home / "bindings.yaml")
        assert len(cfg.bindings) == 1
        assert cfg.bindings[0].profile == "second"

    def test_remove_invalid_index_errors(self, isolated_home: Path) -> None:
        runner.invoke(bindings_app, ["add", "only", "--platform", "telegram"])
        result = runner.invoke(bindings_app, ["remove", "5"])
        assert result.exit_code == 1
        assert "invalid" in result.stdout.lower()


# ---------------------------------------------------------------------------
# set-default
# ---------------------------------------------------------------------------


class TestSetDefault:
    def test_round_trip(self, isolated_home: Path) -> None:
        result = runner.invoke(bindings_app, ["set-default", "personal"])
        assert result.exit_code == 0
        assert "personal" in result.stdout

        cfg = load_bindings(isolated_home / "bindings.yaml")
        assert cfg.default_profile == "personal"

    def test_preserves_existing_bindings(self, isolated_home: Path) -> None:
        runner.invoke(bindings_app, ["add", "coding", "--platform", "telegram"])
        runner.invoke(bindings_app, ["set-default", "personal"])
        cfg = load_bindings(isolated_home / "bindings.yaml")
        assert cfg.default_profile == "personal"
        assert len(cfg.bindings) == 1
        assert cfg.bindings[0].profile == "coding"


# ---------------------------------------------------------------------------
# test (debug command — Pass-2 F12)
# ---------------------------------------------------------------------------


class TestTestCmd:
    def test_no_bindings_returns_default(self, isolated_home: Path) -> None:
        result = runner.invoke(
            bindings_app,
            ["test", "--platform", "telegram", "--chat-id", "any"],
        )
        assert result.exit_code == 0
        # No bindings -> default_profile (which is "default")
        assert "default" in result.stdout.lower()

    def test_predicts_matched_profile(self, isolated_home: Path) -> None:
        runner.invoke(
            bindings_app,
            ["add", "coding", "--platform", "telegram", "--chat-id", "12345"],
        )
        result = runner.invoke(
            bindings_app,
            ["test", "--platform", "telegram", "--chat-id", "12345"],
        )
        assert result.exit_code == 0
        assert "coding" in result.stdout

    def test_unmatched_falls_through_to_default(self, isolated_home: Path) -> None:
        runner.invoke(bindings_app, ["set-default", "personal"])
        runner.invoke(
            bindings_app,
            ["add", "coding", "--platform", "telegram", "--chat-id", "12345"],
        )
        result = runner.invoke(
            bindings_app,
            ["test", "--platform", "telegram", "--chat-id", "99999"],
        )
        assert result.exit_code == 0
        assert "personal" in result.stdout


# ---------------------------------------------------------------------------
# concurrent-write flock test
# ---------------------------------------------------------------------------


def _add_in_subprocess(home: str, profile: str) -> None:
    """Helper: run `oc bindings add <profile>` in a subprocess.

    Uses ``python -m opencomputer.cli`` so this exercises the actual
    main entrypoint and the FileLock acquired by ``_save()``.
    """
    env = os.environ.copy()
    env["OPENCOMPUTER_HOME"] = home
    # Ensure the test's interpreter and PYTHONPATH propagate so the
    # subprocess can import the in-tree opencomputer package.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "opencomputer.cli",
            "bindings",
            "add",
            profile,
            "--platform",
            "telegram",
        ],
        env=env,
        check=True,
        timeout=30,
    )


def test_concurrent_adds_dont_lose_writes(tmp_path: Path) -> None:
    """Three parallel `oc bindings add` invocations must each land on disk.

    Without the FileLock, the read-modify-write of the YAML can drop
    one of the three appends on a fast multi-core box.
    """
    procs = [
        multiprocessing.Process(target=_add_in_subprocess, args=(str(tmp_path), p))
        for p in ("a", "b", "c")
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"subprocess for profile {p.name} crashed"

    cfg = load_bindings(tmp_path / "bindings.yaml")
    profiles = {b.profile for b in cfg.bindings}
    assert profiles == {"a", "b", "c"}, f"lost a write: got {profiles}"
