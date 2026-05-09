"""M5.1 — `oc session checkpoints <id>` subcommand.

Pins the contract added 2026-05-09: a per-session view of the
existing on-disk RewindStore data
(``~/.opencomputer/harness/<session_id>/rewind/<checkpoint_id>/``)
that lets a user pick a checkpoint by id without going through the
cross-session ``oc checkpoints`` admin surface.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from opencomputer.cli_session import session_app


def _seed_rewind_dir(harness_root: Path, session_id: str, *, count: int) -> None:
    """Write ``count`` synthetic checkpoint dirs under harness_root.

    Mirrors the on-disk layout used by RewindStore.save() but without
    invoking RewindStore — keeps the test independent of the
    coding-harness extension's full save path.
    """
    rwd = harness_root / session_id / "rewind"
    rwd.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        cp_dir = rwd / f"cp{i:04d}deadbeef" / "files"
        cp_dir.mkdir(parents=True)
        # A file that contributes to size + count
        (cp_dir / "src__foo.py").write_bytes(b"hello world\n" * (i + 1))
        # Metadata file — RewindStore.load() reads 'paths' (list), not 'files'
        meta = {
            "id": f"cp{i:04d}deadbeef",
            "label": f"before-edit-{i}",
            "created_at": f"2026-05-09T0{i}:00:00+00:00",
            "paths": ["src/foo.py"],
            "excluded_files": [],
        }
        (rwd / f"cp{i:04d}deadbeef" / "meta.json").write_text(json.dumps(meta))


def _patch_harness_root(monkeypatch, target: Path) -> None:
    """Point both checkpoint_admin AND cli_session at a tmp harness root."""
    monkeypatch.setattr(
        "opencomputer.checkpoint_admin.harness_root", lambda: target
    )


class TestSessionCheckpointsHappyPath:
    def test_lists_checkpoints_for_a_session(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _patch_harness_root(monkeypatch, tmp_path)
        _seed_rewind_dir(tmp_path, "sess-aaaa", count=3)

        runner = CliRunner()
        result = runner.invoke(session_app, ["checkpoints", "sess-aaaa"])
        assert result.exit_code == 0, result.stdout
        # Newest-first ordering — cp0002 has the latest created_at
        assert "cp0002deadbe" in result.stdout
        assert "before-edit-2" in result.stdout
        assert "Checkpoints — session sess-aaa" in result.stdout

    def test_json_mode_outputs_parseable_object(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _patch_harness_root(monkeypatch, tmp_path)
        _seed_rewind_dir(tmp_path, "sess-bbbb", count=2)

        runner = CliRunner()
        result = runner.invoke(
            session_app, ["checkpoints", "sess-bbbb", "--json"]
        )
        assert result.exit_code == 0, result.stdout
        obj = json.loads(result.stdout.strip())
        assert obj["session_id"] == "sess-bbbb"
        assert len(obj["checkpoints"]) == 2
        # Newest-first
        assert obj["checkpoints"][0]["label"] == "before-edit-1"
        assert obj["checkpoints"][1]["label"] == "before-edit-0"
        assert obj["checkpoints"][0]["file_count"] == 1
        assert obj["checkpoints"][0]["size_bytes"] > 0

    def test_limit_flag_truncates(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _patch_harness_root(monkeypatch, tmp_path)
        _seed_rewind_dir(tmp_path, "sess-cccc", count=5)

        runner = CliRunner()
        result = runner.invoke(
            session_app, ["checkpoints", "sess-cccc", "--limit", "2"]
        )
        assert result.exit_code == 0
        assert "showing 2 of 5" in result.stdout
        assert "pass --limit" in result.stdout


class TestSessionCheckpointsEmpty:
    def test_no_rewind_dir_returns_friendly_message(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _patch_harness_root(monkeypatch, tmp_path)
        # Session never had a checkpoint written

        runner = CliRunner()
        result = runner.invoke(session_app, ["checkpoints", "sess-empty"])
        assert result.exit_code == 0
        assert "no checkpoints" in result.stdout
        assert "sess-emp" in result.stdout

    def test_no_rewind_dir_json_returns_empty_list(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _patch_harness_root(monkeypatch, tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            session_app, ["checkpoints", "sess-empty", "--json"]
        )
        assert result.exit_code == 0
        obj = json.loads(result.stdout.strip())
        assert obj == {"session_id": "sess-empty", "checkpoints": []}

    def test_existing_dir_with_zero_checkpoints(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _patch_harness_root(monkeypatch, tmp_path)
        # Create the rewind dir but no checkpoint subdirs
        (tmp_path / "sess-blank" / "rewind").mkdir(parents=True)

        runner = CliRunner()
        result = runner.invoke(session_app, ["checkpoints", "sess-blank"])
        assert result.exit_code == 0
        assert "no checkpoints recorded" in result.stdout
