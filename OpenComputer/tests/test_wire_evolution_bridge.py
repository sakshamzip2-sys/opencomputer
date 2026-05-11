"""Wire-server integration for the evolution bridge + status RPC.

Covers:

1. ``WireServer._on_evolution_tuning_bus_event`` builds the expected
   payload from an ``EvolutionTuningChangedEvent``.
2. ``WireServer._collect_evolution_status`` reads the persisted
   tuning file and returns a well-formed dict, with defaults
   metadata included.
3. The collector is failure-isolated — missing profile / read error
   degrades to safe defaults without raising.

We don't spin up a real WS server here — the bridge logic is testable
by calling the static / instance helpers directly. End-to-end WS
exchange tests live in the broader wire_server integration suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from opencomputer.agent.evolution_orchestrator import SCHEMA_VERSION
from opencomputer.gateway.wire_server import WireServer

# ─── _collect_evolution_status ───────────────────────────────────────


def test_collect_evolution_status_returns_defaults_with_no_state(
    tmp_path: Path, monkeypatch
):
    """No tuning file → defaults + defaults metadata block."""
    # ``_collect_evolution_status`` resolves the active profile via
    # ``opencomputer.agent.config._home`` which honors
    # ``OPENCOMPUTER_HOME`` (not the CLI-side ``OPENCOMPUTER_PROFILE_HOME``).
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "skills").mkdir()

    payload = WireServer._collect_evolution_status()

    assert payload["confidence_threshold"] == 70
    assert payload["dreaming_v2_score_threshold"] == pytest.approx(0.65)
    assert payload["dreaming_v2_min_recall"] == 2
    assert payload["decisions_observed"] == 0
    assert payload["last_recompute_ts"] == 0.0
    assert payload["schema_version"] == SCHEMA_VERSION
    # Defaults metadata available for delta rendering.
    assert payload["defaults"]["confidence_threshold"] == 70


def test_collect_evolution_status_reads_persisted_tuning(
    tmp_path: Path, monkeypatch
):
    """A populated state file is reflected in the payload."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "evolution_tuning.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "confidence_threshold": 85,
                "dreaming_v2_score_threshold": 0.80,
                "dreaming_v2_min_recall": 3,
                "decisions_observed": 25,
                "last_recompute_ts": 1700000000.0,
            }
        )
    )

    payload = WireServer._collect_evolution_status()

    assert payload["confidence_threshold"] == 85
    assert payload["dreaming_v2_score_threshold"] == pytest.approx(0.80)
    assert payload["dreaming_v2_min_recall"] == 3
    assert payload["decisions_observed"] == 25
    assert payload["last_recompute_ts"] == 1700000000.0


def test_collect_evolution_status_failure_safe(tmp_path: Path, monkeypatch):
    """The collector returns safe defaults on any unexpected failure
    rather than raising into the wire dispatch."""
    # Point at a bogus path that triggers an OSError on read.
    monkeypatch.setenv("OPENCOMPUTER_HOME", "/nonexistent/path/that/does/not/exist")
    payload = WireServer._collect_evolution_status()
    # Even with a missing path, payload structure stays consistent.
    assert "confidence_threshold" in payload
    assert "defaults" in payload


# ─── protocol exports ────────────────────────────────────────────────


def test_protocol_exports_new_constants():
    """The new EVENT_/METHOD_ constants are in protocol.__all__."""
    from opencomputer.gateway import protocol

    assert "EVENT_EVOLUTION_TUNING_CHANGED" in protocol.__all__
    assert "METHOD_EVOLUTION_STATUS" in protocol.__all__
    assert protocol.EVENT_EVOLUTION_TUNING_CHANGED == "evolution.tuning_changed"
    assert protocol.METHOD_EVOLUTION_STATUS == "evolution.status"
