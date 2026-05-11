"""Tests for ``opencomputer/cli_favorites.py`` — the
``oc favorites`` Typer subgroup that manages the scoped-models
short list used by the Alt+M keybinding.

The CLI must:

* List existing favorites, including count.
* Add a model id with validation (no empty, no duplicate, valid string).
* Remove a model id (no-op-with-warning when not present).
* Persist atomically — flock'd YAML write per OC's profile.yaml pattern.
* Survive missing favorites.yaml (treat as empty list).
* Honor the active profile (writes go to ``<profile_dir>/favorites.yaml``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from opencomputer.cli_favorites import favorites_app


@pytest.fixture
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "oc"
    home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(home))
    return home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _favorites_yaml(home: Path) -> Path:
    return home / "favorites.yaml"


def _read(home: Path) -> list[str]:
    p = _favorites_yaml(home)
    if not p.exists():
        return []
    return yaml.safe_load(p.read_text()).get("models", []) or []


class TestList:
    def test_empty_when_no_file(
        self, runner: CliRunner, fake_home: Path
    ) -> None:
        result = runner.invoke(favorites_app, ["list"])
        assert result.exit_code == 0
        assert "no favorites" in result.stdout.lower()

    def test_lists_existing(
        self, runner: CliRunner, fake_home: Path
    ) -> None:
        _favorites_yaml(fake_home).write_text(
            yaml.safe_dump({"models": ["a", "b", "c"]})
        )
        result = runner.invoke(favorites_app, ["list"])
        assert result.exit_code == 0
        for m in ("a", "b", "c"):
            assert m in result.stdout


class TestAdd:
    def test_add_creates_file(
        self, runner: CliRunner, fake_home: Path
    ) -> None:
        result = runner.invoke(favorites_app, ["add", "claude-opus-4-7"])
        assert result.exit_code == 0
        assert _read(fake_home) == ["claude-opus-4-7"]

    def test_add_appends(self, runner: CliRunner, fake_home: Path) -> None:
        runner.invoke(favorites_app, ["add", "a"])
        runner.invoke(favorites_app, ["add", "b"])
        assert _read(fake_home) == ["a", "b"]

    def test_add_rejects_duplicate(
        self, runner: CliRunner, fake_home: Path
    ) -> None:
        runner.invoke(favorites_app, ["add", "a"])
        result = runner.invoke(favorites_app, ["add", "a"])
        assert result.exit_code != 0
        # Error message goes to stderr via typer.echo(..., err=True);
        # CliRunner's combined .output includes both streams.
        assert "already" in result.output.lower()
        # The file content should be unchanged after the rejected add.
        assert _read(fake_home) == ["a"]

    def test_add_rejects_empty(
        self, runner: CliRunner, fake_home: Path
    ) -> None:
        result = runner.invoke(favorites_app, ["add", ""])
        # Empty string surfaces as a Typer validation error (no value
        # passed) OR as our own "required" error — both are non-zero.
        assert result.exit_code != 0

    def test_add_strips_whitespace(
        self, runner: CliRunner, fake_home: Path
    ) -> None:
        result = runner.invoke(favorites_app, ["add", "  spaced-model  "])
        assert result.exit_code == 0
        assert _read(fake_home) == ["spaced-model"]


class TestRemove:
    def test_remove_existing(
        self, runner: CliRunner, fake_home: Path
    ) -> None:
        runner.invoke(favorites_app, ["add", "a"])
        runner.invoke(favorites_app, ["add", "b"])
        result = runner.invoke(favorites_app, ["remove", "a"])
        assert result.exit_code == 0
        assert _read(fake_home) == ["b"]

    def test_remove_missing_warns(
        self, runner: CliRunner, fake_home: Path
    ) -> None:
        result = runner.invoke(favorites_app, ["remove", "not-there"])
        # No-op with warning — not an error.
        assert "not in favorites" in result.stdout.lower() or "not found" in result.stdout.lower()

    def test_remove_empty_file_is_safe(
        self, runner: CliRunner, fake_home: Path
    ) -> None:
        result = runner.invoke(favorites_app, ["remove", "a"])
        # No favorites yet — must not crash.
        assert result.exit_code == 0 or "not in favorites" in result.stdout.lower()


class TestPersistence:
    def test_yaml_shape_is_models_list(
        self, runner: CliRunner, fake_home: Path
    ) -> None:
        runner.invoke(favorites_app, ["add", "a"])
        runner.invoke(favorites_app, ["add", "b"])
        raw = yaml.safe_load(_favorites_yaml(fake_home).read_text())
        # Strict shape — Alt+M keybinding reads this directly.
        assert isinstance(raw, dict)
        assert "models" in raw
        assert raw["models"] == ["a", "b"]
