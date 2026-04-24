"""Snapshot and unit tests for opencomputer/evolution/prompts/reflect.j2.

All tests are pure unit tests — no LLM calls, no I/O beyond reading the fixture
file. The Jinja2 environment is built inline to mirror what ReflectionEngine will
use (trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from opencomputer.evolution.trajectory import TrajectoryEvent, TrajectoryRecord

TEMPLATE_DIR = Path(__file__).parent.parent / "opencomputer" / "evolution" / "prompts"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "evolution"


@pytest.fixture
def env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot_record() -> TrajectoryRecord:
    """Return the canonical record used for the snapshot fixture."""
    ev1 = TrajectoryEvent(
        session_id="sess-snapshot",
        message_id=1,
        action_type="tool_call",
        tool_name="Read",
        outcome="success",
        timestamp=1699999950.0,
        metadata={"path": "/src/foo.py"},
    )
    ev2 = TrajectoryEvent(
        session_id="sess-snapshot",
        message_id=2,
        action_type="user_reply",
        tool_name=None,
        outcome="success",
        timestamp=1700000000.0,
        metadata={},
    )
    return TrajectoryRecord(
        id=42,
        session_id="sess-snapshot",
        schema_version=1,
        started_at=1699999900.0,
        ended_at=1700000000.0,
        events=(ev1, ev2),
        completion_flag=True,
    )


# ---------------------------------------------------------------------------
# 1. Template loads without syntax errors
# ---------------------------------------------------------------------------


def test_reflect_template_loads(env: Environment) -> None:
    """env.get_template('reflect.j2') returns a Template with no syntax error."""
    from jinja2 import Template

    t = env.get_template("reflect.j2")
    assert isinstance(t, Template)


# ---------------------------------------------------------------------------
# 2. Empty records renders without crashing
# ---------------------------------------------------------------------------


def test_reflect_template_renders_empty_records(env: Environment) -> None:
    """Rendering with records=[] succeeds; output contains '0 total'."""
    t = env.get_template("reflect.j2")
    rendered = t.render(records=[], model_hint="claude-opus-4-7", now=0.0)
    assert "0 total" in rendered


# ---------------------------------------------------------------------------
# 3. Snapshot test — single record
# ---------------------------------------------------------------------------


def test_reflect_template_renders_single_record(env: Environment) -> None:
    """Rendered output for the canonical single-record case matches the snapshot fixture."""
    t = env.get_template("reflect.j2")
    record = _make_snapshot_record()
    rendered = t.render(
        records=[record],
        model_hint="claude-opus-4-7",
        now=1700000060.0,
    )
    fixture_path = FIXTURE_DIR / "reflect_template_basic.expected.txt"
    expected = fixture_path.read_text()
    assert rendered == expected


# ---------------------------------------------------------------------------
# 4. Completion marker
# ---------------------------------------------------------------------------


def test_reflect_template_includes_completion_marker(env: Environment) -> None:
    """completion_flag=True → '✓' in output; completion_flag=False → '✗' in output."""
    t = env.get_template("reflect.j2")

    def _record(flag: bool) -> TrajectoryRecord:
        return TrajectoryRecord(
            id=1,
            session_id="sess-test",
            schema_version=1,
            started_at=1000.0,
            ended_at=2000.0,
            events=(),
            completion_flag=flag,
        )

    rendered_true = t.render(records=[_record(True)], model_hint="m", now=2000.0)
    assert "✓" in rendered_true

    rendered_false = t.render(records=[_record(False)], model_hint="m", now=2000.0)
    assert "✗" in rendered_false


# ---------------------------------------------------------------------------
# 5. Empty metadata — no "{}" in output
# ---------------------------------------------------------------------------


def test_reflect_template_omits_metadata_when_empty(env: Environment) -> None:
    """An event with metadata={} must not include '{}' in the rendered output."""
    t = env.get_template("reflect.j2")
    ev = TrajectoryEvent(
        session_id="sess-test",
        message_id=1,
        action_type="tool_call",
        tool_name="Write",
        outcome="success",
        timestamp=1000.0,
        metadata={},
    )
    record = TrajectoryRecord(
        id=1,
        session_id="sess-test",
        schema_version=1,
        started_at=1000.0,
        ended_at=2000.0,
        events=(ev,),
        completion_flag=True,
    )
    rendered = t.render(records=[record], model_hint="m", now=2000.0)
    assert "{}" not in rendered


# ---------------------------------------------------------------------------
# 6. Non-empty metadata — JSON present in output
# ---------------------------------------------------------------------------


def test_reflect_template_includes_metadata_json_when_present(env: Environment) -> None:
    """An event with metadata={'count': 3} renders '{"count": 3}' in the output."""
    t = env.get_template("reflect.j2")
    ev = TrajectoryEvent(
        session_id="sess-test",
        message_id=1,
        action_type="tool_call",
        tool_name="Bash",
        outcome="success",
        timestamp=1000.0,
        metadata={"count": 3},
    )
    record = TrajectoryRecord(
        id=1,
        session_id="sess-test",
        schema_version=1,
        started_at=1000.0,
        ended_at=2000.0,
        events=(ev,),
        completion_flag=True,
    )
    rendered = t.render(records=[record], model_hint="m", now=2000.0)
    assert '{"count": 3}' in rendered


# ---------------------------------------------------------------------------
# 7. No-events marker
# ---------------------------------------------------------------------------


def test_reflect_template_renders_no_events_marker(env: Environment) -> None:
    """A record with events=() → '(no events recorded)' appears in the output."""
    t = env.get_template("reflect.j2")
    record = TrajectoryRecord(
        id=1,
        session_id="sess-test",
        schema_version=1,
        started_at=1000.0,
        ended_at=2000.0,
        events=(),
        completion_flag=False,
    )
    rendered = t.render(records=[record], model_hint="m", now=2000.0)
    assert "(no events recorded)" in rendered
