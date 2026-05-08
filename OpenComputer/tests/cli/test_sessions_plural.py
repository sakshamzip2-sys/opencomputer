"""Tests for C1-C4 — `oc sessions` plural alias + stats/export/rename."""

import json
from pathlib import Path

from typer.testing import CliRunner

from opencomputer.cli import app


def test_sessions_plural_dispatches() -> None:
    """`oc sessions list` and `oc session list` reach the same subapp."""
    r = CliRunner()
    out_s = r.invoke(app, ["session", "--help"])
    out_p = r.invoke(app, ["sessions", "--help"])
    assert out_s.exit_code == 0
    assert out_p.exit_code == 0
    # Plural alias help should mention the same subcommands.
    assert "list" in out_p.stdout
    assert "stats" in out_p.stdout


def test_stats_smoke(monkeypatch, tmp_path: Path) -> None:
    """`oc sessions stats` exits 0 and prints totals (even on empty DB)."""
    # Point HOME at tmp so the SessionDB is fresh for this test.
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r = CliRunner()
    res = r.invoke(app, ["sessions", "stats"])
    assert res.exit_code == 0, res.stdout
    assert "Total sessions" in res.stdout


def test_export_writes_jsonl(monkeypatch, tmp_path: Path) -> None:
    """`oc sessions export` writes one JSON object per line."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    out = tmp_path / "dump.jsonl"
    r = CliRunner()
    res = r.invoke(app, ["sessions", "export", str(out)])
    assert res.exit_code == 0, res.stdout
    assert out.exists()
    # Each non-blank line is parseable JSON.
    for line in out.read_text().splitlines():
        if line.strip():
            json.loads(line)
