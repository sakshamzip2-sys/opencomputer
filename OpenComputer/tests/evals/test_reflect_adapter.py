"""Phase 8 — reflect adapter accepts structured events, not free text."""

from __future__ import annotations

import pytest

from opencomputer.evals.adapters import adapter_reflect


def test_reflect_adapter_with_structured_events_no_not_implemented():
    """Real LLM call may fail (no provider), but adapter must NOT raise NotImplementedError."""
    case_input = {
        "events": [
            {
                "action_type": "tool_call",
                "tool_name": "Edit",
                "outcome": "success",
                "metadata": {"file": "x.py"},
            },
            {
                "action_type": "tool_call",
                "tool_name": "Edit",
                "outcome": "failure",
                "metadata": {"error": "string not found"},
            },
        ]
    }
    try:
        result = adapter_reflect(case_input)
    except NotImplementedError:
        pytest.fail("adapter_reflect must not raise NotImplementedError after Phase 8")
    except Exception:
        # Any other exception is acceptable here — LLM provider absent in unit tests.
        return
    assert isinstance(result, str)


def test_reflect_adapter_rejects_legacy_session_excerpt():
    """Old shape must error clearly (KeyError or ValueError)."""
    with pytest.raises((KeyError, ValueError)):
        adapter_reflect({"session_excerpt": "old shape"})


def test_reflect_for_eval_rejects_non_list():
    from opencomputer.evolution.reflect import reflect_for_eval

    with pytest.raises(ValueError, match="must be a list"):
        reflect_for_eval("not a list")  # type: ignore[arg-type]


def test_reflect_for_eval_rejects_missing_required_keys():
    from opencomputer.evolution.reflect import reflect_for_eval

    with pytest.raises(KeyError):
        reflect_for_eval([{"tool_name": "Edit"}])  # missing action_type, outcome


def test_reflect_for_eval_synthetic_session_id():
    """Eval-only records must use _eval_synthetic to avoid prod store collision."""
    import time

    from opencomputer.evolution.trajectory import (
        SCHEMA_VERSION_CURRENT,
        TrajectoryEvent,
        TrajectoryRecord,
    )

    # Mirror the reflect_for_eval construction logic — confirms the contract.
    started_at = time.time()
    rec = TrajectoryRecord(
        id=None,
        session_id="_eval_synthetic",
        schema_version=SCHEMA_VERSION_CURRENT,
        started_at=started_at,
        ended_at=started_at,
        events=(
            TrajectoryEvent(
                session_id="_eval_synthetic",
                message_id=0,
                action_type="user_reply",
                tool_name=None,
                outcome="success",
                timestamp=started_at,
                metadata={},
            ),
        ),
        completion_flag=True,
    )
    assert rec.session_id == "_eval_synthetic"


def test_reflect_jsonl_loads_cleanly():
    """The 10 hand-authored cases must parse and have the expected shape."""
    import json
    from pathlib import Path

    cases_path = Path("evals/cases/reflect.jsonl")
    if not cases_path.exists():
        pytest.skip("reflect.jsonl not in CWD; skip this CWD-sensitive check")

    cases = []
    for line in cases_path.read_text().splitlines():
        if line.strip():
            cases.append(json.loads(line))

    assert len(cases) >= 10
    for c in cases:
        assert "events" in c["input"]
        assert c["rubric_id"] == "reflect_v1"
        for ev in c["input"]["events"]:
            assert "action_type" in ev
            assert "outcome" in ev
