"""SQLite run history for the eval harness.

Append-only log of every eval run. Retention enforced at write time
(default 100/site). One row per (site, run); per-case detail stored
as JSON in ``case_runs_json`` for drilldown.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from opencomputer.evals.runner import RunReport

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    accuracy REAL NOT NULL,
    correct INTEGER NOT NULL,
    incorrect INTEGER NOT NULL,
    parse_failures INTEGER NOT NULL,
    infra_failures INTEGER NOT NULL,
    total INTEGER NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    grader_model TEXT,
    cost_usd REAL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    case_runs_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_site_ts ON eval_runs(site_name, timestamp DESC);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def record_run(
    report: RunReport,
    *,
    db_path: Path,
    model: str,
    provider: str,
    grader_model: str | None = None,
    retention_limit: int = 100,
) -> int:
    """Insert one run; prune to retention_limit. Returns row id."""
    case_runs_payload = json.dumps(
        [
            {
                "case_id": c.case_id,
                "correct": c.correct,
                "error_category": c.error_category,
                "input": c.input,
                "expected": c.expected,
                "actual": c.actual,
                "parse_error": c.parse_error,
            }
            for c in report.case_runs
        ],
        default=str,
    )
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO eval_runs
            (site_name, timestamp, accuracy, correct, incorrect, parse_failures,
             infra_failures, total, model, provider, grader_model, cost_usd,
             input_tokens, output_tokens, case_runs_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.site_name,
                datetime.now(UTC).isoformat(),
                report.accuracy,
                report.correct,
                report.incorrect,
                report.parse_failures,
                report.infra_failures,
                report.total,
                model,
                provider,
                grader_model,
                report.cost_usd,
                report.input_tokens,
                report.output_tokens,
                case_runs_payload,
            ),
        )
        new_id = cur.lastrowid
    prune_to_limit(report.site_name, limit=retention_limit, db_path=db_path)
    return new_id or 0


def load_recent_runs(site_name: str, *, db_path: Path, limit: int = 50) -> list[dict]:
    """Return list of run dicts, newest first."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM eval_runs WHERE site_name = ? ORDER BY timestamp DESC LIMIT ?",
            (site_name, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def prune_to_limit(site_name: str, *, limit: int, db_path: Path) -> int:
    """Keep newest ``limit`` rows for site_name; delete the rest."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            DELETE FROM eval_runs
            WHERE site_name = ?
              AND id NOT IN (
                SELECT id FROM eval_runs
                WHERE site_name = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
              )
            """,
            (site_name, site_name, limit),
        )
        return cur.rowcount


def list_sites_with_history(db_path: Path) -> list[str]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT site_name FROM eval_runs ORDER BY site_name"
        ).fetchall()
    return [r["site_name"] for r in rows]
