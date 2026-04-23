"""Phase 12b.5 — Sub-project E, Task E5.

Tests for ``opencomputer plugin demand`` CLI subcommand.

The `demand` command surfaces demand signals recorded in the session DB
by the E2 tracker as a Rich table. Empty state prints an explainer.
Populated state aggregates same (plugin, tool, session) across turns
into ONE row with a count, sorted by count desc, and shows a footer
with the top-recommendation plugin + a copy-pasteable enable command.

All tests isolate ``OPENCOMPUTER_HOME`` → ``tmp_path`` so the session
DB path (`_home() / "sessions.db"`) lands under tmp_path and never
touches the user's real ``~/.opencomputer/``.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from typer.testing import CliRunner

from opencomputer.cli_plugin import plugin_app


def _runner() -> CliRunner:
    return CliRunner()


def _isolate_home(tmp_path: Path, monkeypatch) -> Path:
    """Point OPENCOMPUTER_HOME at tmp_path so sessions.db lands there."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    return tmp_path


def _seed_signals(db_path: Path, rows: list[tuple[str, str, str, int]]) -> None:
    """Insert `(plugin_id, tool_name, session_id, turn_index)` rows.

    Creates the plugin_demand table schema matching E2's tracker so the
    CLI can read them as if the tracker had written them.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plugin_demand (
                plugin_id   TEXT NOT NULL,
                tool_name   TEXT NOT NULL,
                session_id  TEXT NOT NULL,
                turn_index  INTEGER NOT NULL,
                ts          REAL NOT NULL
            )
            """
        )
        ts = time.time()
        conn.executemany(
            "INSERT INTO plugin_demand "
            "(plugin_id, tool_name, session_id, turn_index, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            [(p, t, s, ti, ts) for (p, t, s, ti) in rows],
        )
        conn.commit()


# ─── 1. empty state ───────────────────────────────────────────────────


def test_demand_empty_state_prints_explainer(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)

    result = _runner().invoke(plugin_app, ["demand"])

    assert result.exit_code == 0, result.stdout
    assert "No demand signals recorded" in result.stdout
    assert "opencomputer plugin enable" in result.stdout


# ─── 2. populated: table with counts ──────────────────────────────────


def test_demand_populated_shows_table_with_counts(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    db = profile_dir / "sessions.db"
    _seed_signals(
        db,
        [
            ("demo-editor", "Edit", "session-xyz", 0),
            ("demo-editor", "Edit", "session-xyz", 1),
            ("demo-editor", "Edit", "session-xyz", 2),
            ("demo-writer", "Write", "session-xyz", 0),
        ],
    )

    result = _runner().invoke(plugin_app, ["demand"])

    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "demo-editor" in out
    assert "Edit" in out
    assert "3" in out
    assert "demo-writer" in out
    assert "Write" in out
    assert "1" in out


# ─── 3. same-tool-across-turns aggregation ────────────────────────────


def test_demand_aggregates_same_tool_across_turns(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    db = profile_dir / "sessions.db"
    _seed_signals(
        db,
        [
            ("demo-editor", "Edit", "session-xyz", 0),
            ("demo-editor", "Edit", "session-xyz", 1),
            ("demo-editor", "Edit", "session-xyz", 2),
            ("demo-editor", "Edit", "session-xyz", 3),
        ],
    )

    result = _runner().invoke(plugin_app, ["demand"])

    assert result.exit_code == 0, result.stdout
    # Exactly one aggregated row of count=4; should NOT show 4 rows.
    # The row's count cell is "4" — assert it appears.
    assert "4" in result.stdout
    # A lightweight sanity check: the string "demo-editor" shouldn't
    # appear 4 times (which would imply 4 rows). It can appear in header
    # + one data row + maybe a footer recommendation.
    assert result.stdout.count("demo-editor") <= 3


# ─── 4. top-recommendation footer ─────────────────────────────────────


def test_demand_footer_shows_top_recommendation(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    db = profile_dir / "sessions.db"
    # demo-editor: 10 signals across 2 tools (Edit x5, MultiEdit x5)
    # demo-writer: 3 signals for Write
    rows: list[tuple[str, str, str, int]] = []
    for i in range(5):
        rows.append(("demo-editor", "Edit", "session-a", i))
    for i in range(5):
        rows.append(("demo-editor", "MultiEdit", "session-a", i))
    for i in range(3):
        rows.append(("demo-writer", "Write", "session-a", i))
    _seed_signals(db, rows)

    result = _runner().invoke(plugin_app, ["demand"])

    assert result.exit_code == 0, result.stdout
    assert "Top recommendation" in result.stdout
    assert "demo-editor" in result.stdout
    assert "10" in result.stdout
    # Copy-pasteable command line present
    assert "opencomputer plugin enable demo-editor" in result.stdout


# ─── 5. --since-turns filter ──────────────────────────────────────────


def test_demand_respects_since_turns_filter(tmp_path, monkeypatch):
    profile_dir = _isolate_home(tmp_path, monkeypatch)
    db = profile_dir / "sessions.db"
    # Three distinct turns — 0, 5, 10 — for the same plugin/tool/session.
    # since_turns=6 means min_turn = 10 - 6 = 4, so turns 5 and 10 qualify
    # (2 signals), turn 0 is excluded.
    _seed_signals(
        db,
        [
            ("demo-editor", "Edit", "s1", 0),
            ("demo-editor", "Edit", "s1", 5),
            ("demo-editor", "Edit", "s1", 10),
        ],
    )

    result = _runner().invoke(plugin_app, ["demand", "--since-turns", "6"])

    assert result.exit_code == 0, result.stdout
    # Aggregated count should be 2, not 3. "2" must appear in the
    # table row; "3" should NOT (no row has count=3).
    assert "demo-editor" in result.stdout
    assert "2" in result.stdout
    # Top-recommendation footer should also reflect 2 signals.
    # Guard against accidental "3 signals" leaking from unfiltered logic.
    assert "3 signals" not in result.stdout
