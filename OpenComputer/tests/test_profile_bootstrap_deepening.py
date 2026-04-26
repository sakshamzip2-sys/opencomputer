"""Deepening loop tests — window progression + cursor persistence."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from opencomputer.profile_bootstrap.deepening import (
    DEFAULT_WINDOWS,
    DeepeningCursor,
    DeepeningResult,
    load_cursor,
    run_deepening,
    save_cursor,
)


def test_default_windows_progression():
    assert DEFAULT_WINDOWS == (7, 30, 90, 365, 0)
    # 0 == all-time (sentinel value)


def test_save_then_load_cursor(tmp_path: Path):
    cursor = DeepeningCursor(
        last_window_days=30, last_started_at=1714000000.0, completed_windows=(7,),
    )
    cursor_path = tmp_path / "cursor.json"
    save_cursor(cursor, path=cursor_path)
    loaded = load_cursor(path=cursor_path)
    assert loaded.last_window_days == 30
    assert loaded.completed_windows == (7,)


def test_load_cursor_returns_default_when_absent(tmp_path: Path):
    cursor_path = tmp_path / "missing.json"
    cursor = load_cursor(path=cursor_path)
    assert cursor.last_window_days == 0
    assert cursor.completed_windows == ()


def test_load_cursor_returns_default_on_corrupt_json(tmp_path: Path):
    cursor_path = tmp_path / "bad.json"
    cursor_path.write_text("not json {{{")
    cursor = load_cursor(path=cursor_path)
    assert cursor.last_window_days == 0


def test_run_deepening_advances_to_next_window(tmp_path: Path):
    """Cursor at completed=[7], calling run advances to 30 next."""
    cursor_path = tmp_path / "cursor.json"
    save_cursor(
        DeepeningCursor(last_window_days=7, last_started_at=0.0, completed_windows=(7,)),
        path=cursor_path,
    )

    fake_idle = MagicMock(return_value=MagicMock(idle=True))
    fake_scan_files = MagicMock(return_value=[])
    fake_scan_git = MagicMock(return_value=[])
    fake_extract = MagicMock(return_value=False)
    fake_idle_check = MagicMock(return_value=MagicMock(idle=True))

    with patch(
        "opencomputer.profile_bootstrap.deepening.check_idle",
        fake_idle_check,
    ), patch(
        "opencomputer.profile_bootstrap.deepening.scan_recent_files",
        fake_scan_files,
    ), patch(
        "opencomputer.profile_bootstrap.deepening.scan_git_log",
        fake_scan_git,
    ), patch(
        "opencomputer.profile_bootstrap.deepening.extract_and_emit_motif",
        fake_extract,
    ):
        result = run_deepening(
            cursor_path=cursor_path,
            scan_roots=[tmp_path],
            git_repos=[],
            max_artifacts_per_window=10,
        )

    assert isinstance(result, DeepeningResult)
    assert result.window_processed_days == 30  # advanced from 7
    loaded = load_cursor(path=cursor_path)
    assert 30 in loaded.completed_windows


def test_run_deepening_skips_when_not_idle(tmp_path: Path):
    cursor_path = tmp_path / "cursor.json"
    fake_status = MagicMock()
    fake_status.idle = False
    fake_status.reason = "CPU at 80%"

    with patch(
        "opencomputer.profile_bootstrap.deepening.check_idle",
        return_value=fake_status,
    ):
        result = run_deepening(
            cursor_path=cursor_path, scan_roots=[tmp_path], git_repos=[],
        )
    assert result.skipped_reason == "CPU at 80%"
    assert result.artifacts_processed == 0


def test_run_deepening_force_bypasses_idle_check(tmp_path: Path):
    cursor_path = tmp_path / "cursor.json"
    fake_status = MagicMock()
    fake_status.idle = False
    fake_status.reason = "CPU at 80%"

    with patch(
        "opencomputer.profile_bootstrap.deepening.check_idle",
        return_value=fake_status,
    ), patch(
        "opencomputer.profile_bootstrap.deepening.scan_recent_files",
        return_value=[],
    ), patch(
        "opencomputer.profile_bootstrap.deepening.scan_git_log",
        return_value=[],
    ):
        result = run_deepening(
            cursor_path=cursor_path,
            scan_roots=[tmp_path],
            git_repos=[],
            force=True,
        )
    assert result.skipped_reason == ""
