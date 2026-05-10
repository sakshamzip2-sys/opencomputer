"""Tests for ``oc checkpoints`` Typer subapp."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

HARNESS = Path(__file__).resolve().parents[1] / "extensions" / "coding-harness"
sys.path.insert(0, str(HARNESS))
from rewind.checkpoint import Checkpoint  # type: ignore[import-not-found]  # noqa: E402
from rewind.store import RewindStore  # type: ignore[import-not-found]  # noqa: E402

from opencomputer.cli_checkpoints import checkpoints_app  # noqa: E402

runner = CliRunner()


@pytest.fixture
def harness_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    return tmp_path / "harness"


def _populate(harness: Path, sid: str, n: int) -> None:
    rw = harness / sid / "rewind"
    store = RewindStore(rw, workspace_root=harness)
    for i in range(n):
        store.save(Checkpoint.from_files({f"f{i}": b"x" * 100}, label=f"l{i}"))
        time.sleep(0.005)


def test_status_empty(harness_dir: Path) -> None:
    result = runner.invoke(checkpoints_app, ["status"])
    assert result.exit_code == 0
    assert "no checkpoint" in result.output.lower()


def test_status_populated(harness_dir: Path) -> None:
    _populate(harness_dir, "s1", n=3)
    result = runner.invoke(checkpoints_app, ["status"])
    assert result.exit_code == 0
    assert "s1" in result.output


def test_prune_dry_run(harness_dir: Path) -> None:
    _populate(harness_dir, "s1", n=5)
    result = runner.invoke(
        checkpoints_app,
        ["prune", "--max-count", "2", "--dry-run"],
    )
    assert result.exit_code == 0
    assert "would" in result.output.lower() or "dry" in result.output.lower()
    rw = harness_dir / "s1" / "rewind"
    actual = sum(1 for c in rw.iterdir() if c.is_dir() and (c / "meta.json").exists())
    assert actual == 5


def test_prune_actual_drops(harness_dir: Path) -> None:
    _populate(harness_dir, "s1", n=5)
    result = runner.invoke(checkpoints_app, ["prune", "--max-count", "2"])
    assert result.exit_code == 0
    rw = harness_dir / "s1" / "rewind"
    actual = sum(1 for c in rw.iterdir() if c.is_dir() and (c / "meta.json").exists())
    assert actual == 2


def test_prune_session_filter(harness_dir: Path) -> None:
    _populate(harness_dir, "s1", n=3)
    _populate(harness_dir, "s2", n=3)
    runner.invoke(checkpoints_app, ["prune", "--max-count", "1", "--session", "s1"])
    rw1 = harness_dir / "s1" / "rewind"
    rw2 = harness_dir / "s2" / "rewind"
    n1 = sum(1 for c in rw1.iterdir() if c.is_dir() and (c / "meta.json").exists())
    n2 = sum(1 for c in rw2.iterdir() if c.is_dir() and (c / "meta.json").exists())
    assert n1 == 1
    assert n2 == 3


def test_clear_yes(harness_dir: Path) -> None:
    _populate(harness_dir, "s1", n=2)
    result = runner.invoke(checkpoints_app, ["clear", "--yes"])
    assert result.exit_code == 0
    rw = harness_dir / "s1" / "rewind"
    actual = sum(1 for c in rw.iterdir() if c.is_dir() and (c / "meta.json").exists())
    assert actual == 0


def test_clear_no_yes_no_tty_aborts(harness_dir: Path) -> None:
    _populate(harness_dir, "s1", n=2)
    result = runner.invoke(checkpoints_app, ["clear"])
    assert result.exit_code != 0
    rw = harness_dir / "s1" / "rewind"
    actual = sum(1 for c in rw.iterdir() if c.is_dir() and (c / "meta.json").exists())
    assert actual == 2
