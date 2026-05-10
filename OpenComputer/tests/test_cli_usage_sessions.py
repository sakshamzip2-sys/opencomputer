"""``oc usage sessions`` — per-session SessionDB view with compaction counts.

Spec: ``docs/superpowers/specs/2026-05-10-cc-usage-context-visibility-design.md`` §4.6.

The existing ``oc usage`` callback (PR #420 Wave 5 T5) reads from
``llm_events.jsonl`` and surfaces provider/model rollups but has no
per-session view and no compaction-count column. This subcommand fills
that gap by reading directly from SessionDB. Both views co-exist.

Subcommand args:

  - ``oc usage sessions``                       — last 50 sessions
  - ``oc usage sessions --session-id <id>``     — single-session row
  - ``oc usage sessions --limit N``             — clamped to [1, 1000]
  - ``oc usage sessions --model <m>``           — filter by sessions.model
  - ``oc usage sessions --provider <p>``        — filter by llm_calls.provider
  - ``oc usage sessions --since <ISO date>``    — sessions started after
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.state import SessionDB
from opencomputer.cli_usage import usage_app


def _seed(tmp_path: Path) -> tuple[SessionDB, str, str]:
    db = SessionDB(tmp_path / "sessions.db")
    a = db.allocate_session_id()
    db.create_session(a, platform="cli", model="claude-opus-4-7")
    db.add_tokens(session_id=a, input_tokens=1_000, output_tokens=200, cache_read_tokens=50)
    db.increment_compaction_count(a)

    b = db.allocate_session_id()
    db.create_session(b, platform="cli", model="claude-sonnet-4-6")
    db.add_tokens(session_id=b, input_tokens=2_000, output_tokens=500)

    with db._connect() as conn:
        conn.execute(
            "INSERT INTO llm_calls(session_id, ts, provider, model, input_tokens, output_tokens, cost_usd, batch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (a, time.time(), "anthropic", "claude-opus-4-7", 1_000, 200, 0.05, 0),
        )
        conn.execute(
            "INSERT INTO llm_calls(session_id, ts, provider, model, input_tokens, output_tokens, cost_usd, batch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (b, time.time(), "openai", "gpt-4o", 2_000, 500, 0.04, 0),
        )
    return db, a, b


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    return tmp_path


def test_sessions_renders_table_with_two_rows(profile_home):
    _seed(profile_home)
    result = CliRunner().invoke(usage_app, ["sessions"])
    assert result.exit_code == 0, result.output
    assert "claude-opus-4-7" in result.output
    assert "claude-sonnet-4-6" in result.output


def test_sessions_renders_compaction_count_column(profile_home):
    _seed(profile_home)
    result = CliRunner().invoke(usage_app, ["sessions"])
    assert result.exit_code == 0
    # Either a "compactions" column header or the count value (1) appears
    assert "compactions" in result.output.lower() or "compact" in result.output.lower()


def test_sessions_filter_by_session_id(profile_home):
    _, a, _b = _seed(profile_home)
    result = CliRunner().invoke(usage_app, ["sessions", "--session-id", a])
    assert result.exit_code == 0, result.output
    assert "claude-opus-4-7" in result.output
    assert "claude-sonnet-4-6" not in result.output


def test_sessions_unknown_session_id_renders_empty_state(profile_home):
    _seed(profile_home)
    result = CliRunner().invoke(usage_app, ["sessions", "--session-id", "nope"])
    assert result.exit_code == 0
    assert (
        "no sessions" in result.output.lower()
        or "no rows" in result.output.lower()
        or "not found" in result.output.lower()
    )


def test_sessions_filter_by_model(profile_home):
    _seed(profile_home)
    result = CliRunner().invoke(
        usage_app, ["sessions", "--model", "claude-opus-4-7"]
    )
    assert result.exit_code == 0
    assert "claude-opus-4-7" in result.output
    assert "claude-sonnet-4-6" not in result.output


def test_sessions_filter_by_provider(profile_home):
    _seed(profile_home)
    result = CliRunner().invoke(
        usage_app, ["sessions", "--provider", "anthropic"]
    )
    assert result.exit_code == 0
    assert "claude-opus-4-7" in result.output
    # Session b's provider was openai — should be filtered out
    assert "claude-sonnet-4-6" not in result.output


def test_sessions_limit_caps_rows(profile_home):
    db = SessionDB(profile_home / "sessions.db")
    for i in range(5):
        sid = db.allocate_session_id()
        db.create_session(sid, platform="cli", model=f"model-x{i}")
    result = CliRunner().invoke(usage_app, ["sessions", "--limit", "2"])
    assert result.exit_code == 0
    # Most-recent first → model-x3, model-x4 visible; model-x0 not.
    assert "model-x0" not in result.output


def test_sessions_invalid_limit_does_not_crash(profile_home):
    _seed(profile_home)
    result = CliRunner().invoke(usage_app, ["sessions", "--limit", "0"])
    assert "Traceback" not in result.output


def test_sessions_empty_db_renders_empty_state(profile_home):
    SessionDB(profile_home / "sessions.db")  # empty DB
    result = CliRunner().invoke(usage_app, ["sessions"])
    assert result.exit_code == 0
    assert (
        "no sessions" in result.output.lower()
        or "empty" in result.output.lower()
    )


def test_sessions_missing_db_does_not_crash(profile_home):
    """No sessions.db at all — must not crash; render empty state."""
    result = CliRunner().invoke(usage_app, ["sessions"])
    assert "Traceback" not in result.output
    assert result.exit_code == 0


def test_existing_usage_callback_still_works(profile_home):
    """Stability: the existing top-level ``oc usage`` (no subcommand)
    continues to render its JSONL-based view. We don't break the
    shipped surface."""
    result = CliRunner().invoke(usage_app, [])
    assert result.exit_code == 0
    # When no llm_events.jsonl exists, existing CLI shows "no llm events".
    assert "no llm events" in result.output.lower() or "events" in result.output.lower()


def test_sessions_renders_cost_when_present(profile_home):
    _seed(profile_home)
    result = CliRunner().invoke(usage_app, ["sessions"])
    assert result.exit_code == 0
    # Seed inserted cost_usd=0.05 for session a, 0.04 for b. Either appears.
    assert "0.05" in result.output or "$0.05" in result.output or "0.09" in result.output


def test_sessions_renders_cost_dash_when_missing(profile_home):
    """Sessions with NO llm_calls rows should render cost as '—' (or
    similar) rather than '$0.00' which would lie about pricing data."""
    db = SessionDB(profile_home / "sessions.db")
    sid = db.allocate_session_id()
    db.create_session(sid, platform="cli", model="m")
    db.add_tokens(session_id=sid, input_tokens=10, output_tokens=5)
    # No llm_calls inserted.
    result = CliRunner().invoke(usage_app, ["sessions"])
    assert result.exit_code == 0
    # Cost column should show the not-priced indicator, not $0.00
    assert "$0.00" not in result.output
