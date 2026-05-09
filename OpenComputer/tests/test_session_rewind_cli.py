"""M5.3 — `oc session rewind <id>` subcommand.

Pins the contract added 2026-05-09:

* Non-interactive flow: ``--at <checkpoint_id>`` selects directly.
* Default ``--mode files`` restores file contents from the existing
  RewindStore (the auto_checkpoint hook's output).
* ``--mode conv_only`` / ``summarize_from`` return clear
  not-yet-implemented errors (M5.2 deferral).
* Interactive picker requires a TTY; no-TTY → friendly error
  asking for ``--at``.
* Confirmation prompt unless ``--yes``.
* Missing session, no checkpoints, unknown checkpoint id all return
  exit code 1 with a Rich message.

Tests stub the harness root + the actual restore call so they don't
touch real disk state outside ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_session import session_app


def _seed_rewind_dir(harness_root: Path, session_id: str, *, count: int) -> None:
    """Mirror the auto_checkpoint on-disk layout for `count` checkpoints."""
    rwd = harness_root / session_id / "rewind"
    rwd.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        cp_dir = rwd / f"cp{i:04d}deadbeef" / "files"
        cp_dir.mkdir(parents=True)
        (cp_dir / "src__foo.py").write_bytes(b"hello world\n" * (i + 1))
        meta = {
            "id": f"cp{i:04d}deadbeef",
            "label": f"before-edit-{i}",
            "created_at": f"2026-05-09T0{i}:00:00+00:00",
            "paths": ["src/foo.py"],
            "excluded_files": [],
        }
        (rwd / f"cp{i:04d}deadbeef" / "meta.json").write_text(json.dumps(meta))


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch harness_root → tmp_path so tests are isolated."""
    monkeypatch.setattr(
        "opencomputer.checkpoint_admin.harness_root", lambda: tmp_path
    )
    return tmp_path


# ─── --at + --yes (non-interactive happy path) ───────────────────────────


class TestRewindNonInteractive:
    def test_at_with_yes_restores(self, harness: Path) -> None:
        _seed_rewind_dir(harness, "sess-aaa", count=3)

        runner = CliRunner()
        result = runner.invoke(
            session_app,
            ["rewind", "sess-aaa", "--at", "cp0001", "--yes"],
        )
        assert result.exit_code == 0, result.stdout
        assert "restored" in result.stdout
        assert "cp0001deadbe" in result.stdout

    def test_at_without_yes_aborts_on_no(self, harness: Path) -> None:
        _seed_rewind_dir(harness, "sess-bbb", count=1)

        runner = CliRunner()
        # input='\n' → empty answer → not 'y' → abort
        result = runner.invoke(
            session_app, ["rewind", "sess-bbb", "--at", "cp0000"], input="\n"
        )
        assert result.exit_code == 1
        assert "aborted" in result.stdout or "Aborted" in result.stdout

    def test_at_unknown_id_returns_exit_1(self, harness: Path) -> None:
        _seed_rewind_dir(harness, "sess-ccc", count=1)

        runner = CliRunner()
        result = runner.invoke(
            session_app, ["rewind", "sess-ccc", "--at", "ZZZZ", "--yes"]
        )
        assert result.exit_code == 1
        assert "no checkpoint matches" in result.stdout


# ─── --mode handling ─────────────────────────────────────────────────────


class TestModeHandling:
    def test_invalid_mode_exits_2(self, harness: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            session_app,
            ["rewind", "sess-x", "--at", "cp0", "--yes", "--mode", "yolo"],
        )
        assert result.exit_code == 2
        assert "unknown --mode" in result.stdout

    def test_conv_only_mode_returns_not_implemented(self, harness: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            session_app,
            ["rewind", "sess-x", "--at", "cp0", "--yes", "--mode", "conv_only"],
        )
        assert result.exit_code == 2
        assert "not yet implemented" in result.stdout
        assert "M5.2" in result.stdout

    def test_summarize_from_mode_returns_not_implemented(
        self, harness: Path
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(
            session_app,
            ["rewind", "sess-x", "--at", "cp0", "--yes", "--mode", "summarize_from"],
        )
        assert result.exit_code == 2
        assert "not yet implemented" in result.stdout


# ─── empty / missing session paths ───────────────────────────────────────


class TestEmptyPaths:
    def test_missing_session_returns_exit_1(self, harness: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            session_app, ["rewind", "sess-ghost", "--at", "cp0", "--yes"]
        )
        assert result.exit_code == 1
        assert "no rewind store" in result.stdout

    def test_empty_rewind_dir_returns_exit_1(self, harness: Path) -> None:
        # Create an empty rewind dir
        (harness / "sess-empty" / "rewind").mkdir(parents=True)
        runner = CliRunner()
        result = runner.invoke(
            session_app, ["rewind", "sess-empty", "--at", "cp0", "--yes"]
        )
        assert result.exit_code == 1
        assert "no checkpoints" in result.stdout


# ─── id-prefix resolution ────────────────────────────────────────────────


class TestPrefixResolution:
    def test_4char_prefix_unique_resolves(self, harness: Path) -> None:
        _seed_rewind_dir(harness, "sess-pref", count=1)
        # cp0000deadbeef — 'cp00' is a 4-char unique prefix
        runner = CliRunner()
        result = runner.invoke(
            session_app, ["rewind", "sess-pref", "--at", "cp00", "--yes"]
        )
        assert result.exit_code == 0
        assert "restored" in result.stdout

    def test_3char_prefix_does_not_resolve(self, harness: Path) -> None:
        _seed_rewind_dir(harness, "sess-pref", count=1)
        # 'cp0' is only 3 chars; matcher requires 4+
        runner = CliRunner()
        result = runner.invoke(
            session_app, ["rewind", "sess-pref", "--at", "cp0", "--yes"]
        )
        assert result.exit_code == 1
        assert "no checkpoint matches" in result.stdout


# ─── interactive picker requires TTY ─────────────────────────────────────


class TestInteractivePickerRequiresTTY:
    def test_no_tty_no_at_returns_friendly_error(
        self, harness: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_rewind_dir(harness, "sess-tty", count=1)
        # Ensure isatty is False — the CliRunner already disables it.
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        runner = CliRunner()
        result = runner.invoke(session_app, ["rewind", "sess-tty"])
        assert result.exit_code == 1
        assert "interactive picker requires a TTY" in result.stdout
        assert "--at" in result.stdout
