"""Tests for ``oc gateway diagnose`` — M1 / T1.6 + T1.7.

The command reads the ``gateway_parity_log`` telemetry table and renders
it two ways: a per-turn table (default) and a fire-rate rollup
(``--rollup``).
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from opencomputer.cli_gateway import _parse_since, gateway_app
from opencomputer.gateway.parity_probe import ParityProbe

runner = CliRunner()


@pytest.fixture()
def audit_home(tmp_path, monkeypatch):
    """A tmp profile home; ``_profile_home`` is patched to point at it."""
    monkeypatch.setattr(
        "opencomputer.cli_gateway._profile_home", lambda: tmp_path
    )
    return tmp_path


def _seed(audit_home, *, turns_with_override: int, plain_turns: int) -> None:
    """Write `turns_with_override` turns where prompt_override fired
    plus `plain_turns` turns where nothing fired."""
    db = audit_home / "audit.db"
    tid = 1
    for _ in range(turns_with_override):
        p = ParityProbe(session_id="sess1234", turn_id=tid, platform="telegram")
        p.observe("prompt_override", True, {"template": "stocks"})
        p.flush(db)
        tid += 1
    for _ in range(plain_turns):
        ParityProbe(
            session_id="sess1234", turn_id=tid, platform="telegram"
        ).flush(db)
        tid += 1


# ── _parse_since ─────────────────────────────────────────────────────


def test_parse_since_none() -> None:
    assert _parse_since(None) is None
    assert _parse_since("") is None


def test_parse_since_units() -> None:
    import time

    now = time.time()
    assert _parse_since("1h") == pytest.approx(now - 3600, abs=5)
    assert _parse_since("2d") == pytest.approx(now - 172800, abs=5)
    assert _parse_since("90") == pytest.approx(now - 90, abs=5)  # bare = seconds


def test_parse_since_rejects_garbage() -> None:
    import typer

    with pytest.raises(typer.BadParameter):
        _parse_since("banana")


# ── empty state ──────────────────────────────────────────────────────


def test_diagnose_empty_db_is_friendly(audit_home) -> None:
    result = runner.invoke(gateway_app, ["diagnose"])
    assert result.exit_code == 0
    assert "No gateway parity telemetry yet" in result.stdout


def test_diagnose_rollup_empty_db_is_friendly(audit_home) -> None:
    result = runner.invoke(gateway_app, ["diagnose", "--rollup"])
    assert result.exit_code == 0
    assert "No gateway parity telemetry yet" in result.stdout


# ── per-turn mode ────────────────────────────────────────────────────


def test_diagnose_per_turn_lists_turns(audit_home) -> None:
    _seed(audit_home, turns_with_override=2, plain_turns=1)
    result = runner.invoke(gateway_app, ["diagnose"])
    assert result.exit_code == 0
    assert "recent turns" in result.stdout
    assert "telegram" in result.stdout
    # The plain turn shows the full-parity marker.
    assert "full parity" in result.stdout


def test_diagnose_json_is_machine_readable(audit_home) -> None:
    _seed(audit_home, turns_with_override=1, plain_turns=0)
    result = runner.invoke(gateway_app, ["diagnose", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "rows" in payload
    assert len(payload["rows"]) == 10  # one turn × 10 mechanisms


def test_diagnose_session_filter(audit_home) -> None:
    _seed(audit_home, turns_with_override=1, plain_turns=0)
    other = ParityProbe(session_id="other999", turn_id=1, platform="discord")
    other.flush(audit_home / "audit.db")
    result = runner.invoke(
        gateway_app, ["diagnose", "--json", "--session", "sess1234"]
    )
    payload = json.loads(result.stdout)
    assert {r["session_id"] for r in payload["rows"]} == {"sess1234"}


# ── rollup mode ──────────────────────────────────────────────────────


def test_diagnose_rollup_shows_fire_rate(audit_home) -> None:
    # prompt_override fires on 3 of 4 turns → 75%.
    _seed(audit_home, turns_with_override=3, plain_turns=1)
    result = runner.invoke(gateway_app, ["diagnose", "--rollup"])
    assert result.exit_code == 0
    assert "rollup" in result.stdout
    assert "75%" in result.stdout


def test_diagnose_rollup_json_priority_ordering(audit_home) -> None:
    _seed(audit_home, turns_with_override=3, plain_turns=1)
    result = runner.invoke(gateway_app, ["diagnose", "--rollup", "--json"])
    payload = json.loads(result.stdout)
    scores = [r["priority_score"] for r in payload["rollup"]]
    assert scores == sorted(scores, reverse=True)
    # prompt_override (severity 4, fire-rate .75) → priority 3.0, the top.
    assert payload["rollup"][0]["mechanism_id"] == "prompt_override"
    assert payload["rollup"][0]["priority_score"] == pytest.approx(3.0)
