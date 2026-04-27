"""V3.A-T10 — /scrape slash command tests.

The /scrape command is registered as a built-in (non-plugin) slash command
in :mod:`opencomputer.agent.slash_commands`. It dispatches the same way
plugin commands do — through ``opencomputer.agent.slash_dispatcher`` —
so tests use the convenience helper ``dispatch_slash`` rather than wiring
up a full agent loop.

Each test that touches the snapshot directory pins ``OPENCOMPUTER_HOME``
to ``tmp_path`` so we never write to the real ``~/.opencomputer``.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


def _invoke_scrape_command(args: str = "") -> str:
    """Helper to call the scrape command via the public dispatch surface.

    ``args`` is appended after ``/scrape`` (no leading slash on its own).
    Returns the command's output string. Imported locally so each test
    re-imports against fresh module state.
    """
    from opencomputer.agent.slash_commands import dispatch_slash

    message = f"/scrape {args}".strip()
    return dispatch_slash(message)


def test_scrape_command_is_registered():
    """The /scrape command should appear in the slash command registry."""
    from opencomputer.agent.slash_commands import (
        get_registered_commands,
        register_builtin_slash_commands,
    )

    register_builtin_slash_commands()
    cmds = get_registered_commands()
    names = {getattr(c, "name", "") for c in cmds}
    assert "scrape" in names


def test_scrape_command_invokes_run_scrape(tmp_path: Path, monkeypatch):
    """Running /scrape should call run_scrape and return a summary."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.skills.profile_scraper import scraper as scraper_mod

    fake_snapshot = MagicMock()
    fake_snapshot.facts = ()
    fake_snapshot.sources_attempted = ("identity", "projects")
    fake_snapshot.sources_succeeded = ("identity",)
    fake_snapshot.started_at = 1.0
    fake_snapshot.ended_at = 2.5

    with patch.object(scraper_mod, "run_scrape", return_value=fake_snapshot) as mock_run:
        result = _invoke_scrape_command(args="")

    mock_run.assert_called_once()
    # Summary should reference the scrape outcome — keyword test like
    # the spec asks for, plus a concrete claim that duration shows up.
    lower = result.lower()
    assert "scraped" in lower or "snapshot" in lower
    # Duration should be 1.5 (2.5 - 1.0). Format is "1.5s".
    assert "1.5" in result


def test_scrape_command_passes_full_flag(tmp_path: Path, monkeypatch):
    """`/scrape --full` should propagate full=True to run_scrape."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.skills.profile_scraper import scraper as scraper_mod

    fake_snapshot = MagicMock()
    fake_snapshot.facts = ()
    fake_snapshot.sources_attempted = ("identity",)
    fake_snapshot.sources_succeeded = ("identity",)
    fake_snapshot.started_at = 0.0
    fake_snapshot.ended_at = 0.5

    with patch.object(scraper_mod, "run_scrape", return_value=fake_snapshot) as mock_run:
        _invoke_scrape_command(args="--full")

    mock_run.assert_called_once_with(full=True)


def test_scrape_diff_compares_latest_two_snapshots(tmp_path: Path, monkeypatch):
    """`/scrape --diff` reads the last two snapshot files and returns a diff."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    out_dir = tmp_path / "profile_scraper"
    out_dir.mkdir()

    (out_dir / "snapshot_1000.json").write_text(json.dumps({
        "facts": [{
            "field": "name", "value": "Alice", "source": "x",
            "confidence": 1.0, "timestamp": 1000.0,
        }],
        "started_at": 1000.0, "ended_at": 1001.0,
        "sources_attempted": ["identity"], "sources_succeeded": ["identity"],
    }))
    (out_dir / "snapshot_2000.json").write_text(json.dumps({
        "facts": [{
            "field": "name", "value": "Bob", "source": "x",
            "confidence": 1.0, "timestamp": 2000.0,
        }],
        "started_at": 2000.0, "ended_at": 2001.0,
        "sources_attempted": ["identity"], "sources_succeeded": ["identity"],
    }))

    result = _invoke_scrape_command(args="--diff")
    # The diff should mention the changed field and at least one of the
    # values. We treat it as a "changed" entry since the field is shared.
    assert "name" in result
    assert "Alice" in result or "Bob" in result


def test_scrape_diff_with_no_prior_snapshot(tmp_path: Path, monkeypatch):
    """`/scrape --diff` with zero snapshots tells the user to run scrape first."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = _invoke_scrape_command(args="--diff")
    lower = result.lower()
    # Output should explain that there's nothing to diff.
    assert "no" in lower or "first" in lower
    assert "snapshot" in lower or "scrape" in lower


def test_scrape_diff_with_only_one_snapshot(tmp_path: Path, monkeypatch):
    """`/scrape --diff` with only one snapshot says no diff possible yet."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    out_dir = tmp_path / "profile_scraper"
    out_dir.mkdir()
    (out_dir / "snapshot_1000.json").write_text(json.dumps({
        "facts": [],
        "started_at": 1000.0, "ended_at": 1001.0,
        "sources_attempted": ["identity"], "sources_succeeded": ["identity"],
    }))

    result = _invoke_scrape_command(args="--diff")
    lower = result.lower()
    # Should mention "one" or "no diff" or similar.
    assert "one" in lower or "no diff" in lower or "first" in lower


def test_scrape_handles_run_scrape_failure(tmp_path: Path, monkeypatch):
    """If run_scrape raises, /scrape returns a friendly error string."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.skills.profile_scraper import scraper as scraper_mod

    with patch.object(
        scraper_mod, "run_scrape", side_effect=RuntimeError("disk full")
    ):
        result = _invoke_scrape_command(args="")

    lower = result.lower()
    assert "fail" in lower or "error" in lower or "runtimeerror" in lower
    # Doesn't propagate the exception — the dispatch returns a string.
    assert isinstance(result, str)


def test_scrape_diff_added_and_removed_fields(tmp_path: Path, monkeypatch):
    """`/scrape --diff` reports added + removed entries when fields differ."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    out_dir = tmp_path / "profile_scraper"
    out_dir.mkdir()

    (out_dir / "snapshot_1000.json").write_text(json.dumps({
        "facts": [{
            "field": "git_repo", "value": "/repo/old", "source": "fs",
            "confidence": 1.0, "timestamp": 1000.0,
        }],
        "started_at": 1000.0, "ended_at": 1001.0,
        "sources_attempted": ["projects"], "sources_succeeded": ["projects"],
    }))
    (out_dir / "snapshot_2000.json").write_text(json.dumps({
        "facts": [{
            "field": "github_repo", "value": "user/new", "source": "gh",
            "confidence": 1.0, "timestamp": 2000.0,
        }],
        "started_at": 2000.0, "ended_at": 2001.0,
        "sources_attempted": ["git_activity"], "sources_succeeded": ["git_activity"],
    }))

    result = _invoke_scrape_command(args="--diff")
    # Different fields → one added, one removed.
    assert "github_repo" in result
    assert "git_repo" in result
