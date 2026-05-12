"""Test the augmented `oc evolution dashboard` surface.

Phase 5 of the senior-engineer workflow on self-evolution-comparison.md:
the doc claimed B3 was blocked + skill-evolution was off, but ground truth
shows everything is wired and on. The user needs ONE command that surfaces
*what's actually firing* without reading 7,000+ LOC.

These tests cover:
1. `last_summary` field written by ``run_dreaming_v2_async`` so the
   dashboard has data to read (M3 audit-log fallback — no schema change).
2. Augmented ``oc evolution dashboard`` reads four new sources gracefully:
   - skill-evolution heartbeat freshness
   - ``_proposed/`` candidate count
   - dreaming-v2 ``last_summary`` (gate-fail counts)
   - DREAMS.md size vs cap
3. Empty-state rendering — every source missing renders cleanly, no crash.
4. Malformed JSON in state files degrades gracefully (parses to defaults).

Privacy: nothing reads transcript content. All reads are counts / sizes /
timestamps / pre-redacted candidate slugs.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.dreaming_v2 import (
    DreamCandidate,
    DreamGateResult,
    DreamOutcome,
    DreamRunSummary,
)
from opencomputer.evolution.entrypoint import evolution_app
from opencomputer.evolution.storage import apply_pending

runner = CliRunner()


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path: Path) -> Path:
    """Fresh OPENCOMPUTER_HOME with empty evolution DB."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    evo_dir = tmp_path / "evolution"
    evo_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(evo_dir / "trajectory.sqlite"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)
    conn.close()
    return tmp_path


# ── M3: last_summary persisted to dreaming_v2_state.json ────────────


def test_save_state_includes_last_summary_when_provided(
    isolated_home: Path,
) -> None:
    """Smallest possible change: state save accepts a summary blob.

    We persist *counts only* — never per-candidate text. The audit-plan
    phase chose this over a SQLite audit table because:
      (a) read-side only needs counts to be useful;
      (b) state.json already exists, no migration risk;
      (c) bounded size — counts compress to ~200 bytes.
    """
    from opencomputer.cron.dreaming_v2_tick import _load_state, _save_state

    state_path = isolated_home / "cron" / "dreaming_v2_state.json"
    summary = {
        "promoted": 0,
        "held": 5,
        "dropped": 2,
        "score_only": 3,
        "recall_only": 1,
        "both_gates": 1,
        "diversity_fail": 2,
        "catch_up_run": False,
        "evaluated": 7,
        "run_ts_ns": 1_700_000_000_000_000_000,
    }
    _save_state(
        {
            "processed_event_ids": ["1", "2"],
            "last_run_ts_ns": 1_700_000_000_000_000_000,
            "last_summary": summary,
        },
        state_path,
    )
    reloaded = _load_state(state_path)
    assert reloaded["last_summary"] == summary


def test_load_state_tolerates_missing_last_summary(isolated_home: Path) -> None:
    """Old state files (pre-this-change) must still load — backward compat."""
    from opencomputer.cron.dreaming_v2_tick import _load_state, _save_state

    state_path = isolated_home / "cron" / "dreaming_v2_state.json"
    _save_state(
        {"processed_event_ids": ["1"], "last_run_ts_ns": 17}, state_path
    )
    reloaded = _load_state(state_path)
    # No last_summary key — must not raise.
    assert "last_summary" not in reloaded or reloaded["last_summary"] is None


def test_summarize_run_counts_per_gate_failure_class() -> None:
    """The summary builder maps DreamRunSummary → JSON-safe counts dict.

    Pure function; no IO; deterministic.
    """
    from opencomputer.cron.dreaming_v2_tick import summarize_run_for_state

    def _cand(eid: str) -> DreamCandidate:
        return DreamCandidate(event_id=eid, raw_text="...", timestamp_ns=0)

    summary = DreamRunSummary(
        promoted=(
            DreamGateResult(
                candidate=_cand("a"),
                outcome=DreamOutcome.PROMOTED,
                score=0.9,
                recall_count=3,
                diversity_score=0.4,
                rationale="all gates passed",
            ),
        ),
        held=(
            DreamGateResult(
                candidate=_cand("b"),
                outcome=DreamOutcome.HELD,
                score=0.2,
                recall_count=4,
                diversity_score=0.4,
                rationale="held: score=0.20<0.65",
            ),
            DreamGateResult(
                candidate=_cand("c"),
                outcome=DreamOutcome.HELD,
                score=0.7,
                recall_count=0,
                diversity_score=0.4,
                rationale="held: recall=0<2",
            ),
        ),
        dropped=(
            DreamGateResult(
                candidate=_cand("d"),
                outcome=DreamOutcome.DROPPED,
                score=0.8,
                recall_count=3,
                diversity_score=0.95,
                rationale="diversity gate failed: cosine=0.950 >= threshold=0.8",
            ),
        ),
        skipped_already_processed=2,
        total_evaluated=4,
        catch_up_run=False,
    )

    out = summarize_run_for_state(summary, run_ts_ns=42)

    assert out["promoted"] == 1
    assert out["held"] == 2
    assert out["dropped"] == 1
    # Disjoint HELD buckets — must sum exactly to held.
    assert out["score_only"] == 1  # rationale has score=...<... only
    assert out["recall_only"] == 1  # rationale has recall=...<... only
    assert out["both_gates"] == 0  # neither held example had both
    assert out["score_only"] + out["recall_only"] + out["both_gates"] == out["held"]
    assert out["diversity_fail"] == 1
    assert out["evaluated"] == 4
    assert out["catch_up_run"] is False
    assert out["run_ts_ns"] == 42


def test_summarize_run_disjoint_buckets_both_gates() -> None:
    """A HELD result where rationale lists BOTH score and recall failures
    is counted ONCE in the ``both_gates`` bucket — not in either single bucket.
    This preserves the invariant ``held = score_only + recall_only + both_gates``.
    """
    from opencomputer.cron.dreaming_v2_tick import summarize_run_for_state

    cand = DreamCandidate(event_id="x", raw_text="", timestamp_ns=0)
    summary = DreamRunSummary(
        promoted=(),
        held=(
            DreamGateResult(
                candidate=cand,
                outcome=DreamOutcome.HELD,
                score=0.1,
                recall_count=0,
                diversity_score=0.4,
                rationale="held: score=0.10<0.65, recall=0<2",
            ),
        ),
        dropped=(),
        total_evaluated=1,
    )
    out = summarize_run_for_state(summary, run_ts_ns=0)
    assert out["score_only"] == 0
    assert out["recall_only"] == 0
    assert out["both_gates"] == 1
    assert out["score_only"] + out["recall_only"] + out["both_gates"] == out["held"]


# ── M2: augmented dashboard rendering ───────────────────────────────


def test_dashboard_renders_empty_state_cleanly(isolated_home: Path) -> None:
    """No skill heartbeat, no _proposed/, no dreams state → no crash."""
    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    # The new "Operational" section must appear even with no data.
    assert "Operational" in result.stdout
    assert "skill-evolution" in result.stdout
    # Empty-state markers — "no data" or em-dash, not Python tracebacks.
    assert "Traceback" not in result.stdout


def test_dashboard_shows_skill_evolution_heartbeat_when_present(
    isolated_home: Path,
) -> None:
    """Heartbeat file exists → dashboard reports recency."""
    hb = isolated_home / "skills" / "evolution_heartbeat"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text(str(time.time()))

    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    # We surface "active" when heartbeat is fresh (<1h old).
    assert "active" in result.stdout.lower()


def test_dashboard_counts_proposed_skills(isolated_home: Path) -> None:
    """N candidate dirs in _proposed/ → N shown."""
    proposed = isolated_home / "skills" / "_proposed"
    for slug in ("idea-a", "idea-b", "idea-c"):
        d = proposed / slug
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nslug: x\n---\n")
    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    assert "3" in result.stdout  # candidate count surfaces somewhere


def test_dashboard_renders_dreaming_v2_last_summary(
    isolated_home: Path,
) -> None:
    """When last_summary present → counts visible on dashboard."""
    state_path = isolated_home / "cron" / "dreaming_v2_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "processed_event_ids": ["1", "2"],
                "last_run_ts_ns": int(time.time() * 1e9),
                "last_summary": {
                    "promoted": 0,
                    "held": 47,
                    "dropped": 3,
                    "score_fail": 40,
                    "recall_fail": 7,
                    "diversity_fail": 3,
                    "evaluated": 50,
                    "catch_up_run": False,
                    "run_ts_ns": int(time.time() * 1e9),
                },
            }
        )
    )
    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    # The promoted/held/dropped trio must render.
    for n in ("47", "3", "0"):
        assert n in result.stdout
    # Gate-fail context surfaces (so user sees WHY).
    assert "score" in result.stdout.lower()


def test_dashboard_tolerates_malformed_state_json(isolated_home: Path) -> None:
    """A bad state.json must not crash the whole dashboard."""
    state_path = isolated_home / "cron" / "dreaming_v2_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not json")

    result = runner.invoke(evolution_app, ["dashboard"])
    # Dashboard MUST stay green — the rest of the dashboard is still useful.
    assert result.exit_code == 0
    assert "Traceback" not in result.stdout


def test_dashboard_reads_dreams_md_size_and_cap(
    isolated_home: Path,
) -> None:
    """DREAMS.md size vs cap is shown — surfaces "rotating noise" condition."""
    dreams = isolated_home / "DREAMS.md"
    dreams.write_text("x" * 16_000)  # near 16 KB cap

    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    # Either size or "DREAMS" surfaces in output.
    assert "DREAMS" in result.stdout or "dreams" in result.stdout.lower()


# ── Adversarial-input tests ─────────────────────────────────────────


def test_dashboard_tolerates_state_json_as_list(isolated_home: Path) -> None:
    """state.json holds a JSON list instead of dict → log warning, no crash."""
    state_path = isolated_home / "cron" / "dreaming_v2_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps([1, 2, 3]))

    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    assert "Traceback" not in result.stdout
    # Should mention shape unexpected in fallback message.
    assert "shape unexpected" in result.stdout.lower() or "—" in result.stdout


def test_dashboard_tolerates_last_summary_as_string(
    isolated_home: Path,
) -> None:
    """last_summary is a string not dict → log warning, no crash."""
    state_path = isolated_home / "cron" / "dreaming_v2_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "processed_event_ids": [],
                "last_run_ts_ns": 0,
                "last_summary": "I am not a dict",
            }
        )
    )

    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    assert "Traceback" not in result.stdout


def test_dashboard_tolerates_proposed_being_a_file(isolated_home: Path) -> None:
    """_proposed/ exists but is a regular file → log + fallback row."""
    proposed = isolated_home / "skills" / "_proposed"
    proposed.parent.mkdir(parents=True, exist_ok=True)
    proposed.write_text("oops")  # someone touched the wrong path

    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    assert "Traceback" not in result.stdout


def test_dashboard_tolerates_dreams_md_as_directory(
    isolated_home: Path,
) -> None:
    """DREAMS.md exists but is a directory → log + fallback row, no crash."""
    dreams = isolated_home / "DREAMS.md"
    dreams.mkdir(parents=True)

    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    assert "Traceback" not in result.stdout


def test_dashboard_tolerates_heartbeat_in_the_future(
    isolated_home: Path,
) -> None:
    """Clock skew — heartbeat mtime > now → reported as fallback, log WARN."""
    import os
    import time as _time

    hb = isolated_home / "skills" / "evolution_heartbeat"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("0")
    future = _time.time() + 86400 * 30  # 30 days in the future
    os.utime(hb, (future, future))

    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    assert "Traceback" not in result.stdout


def test_dashboard_handles_numeric_strings_in_state(
    isolated_home: Path,
) -> None:
    """Hand-edited state file with string-typed counts must coerce, not crash."""
    state_path = isolated_home / "cron" / "dreaming_v2_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "processed_event_ids": [],
                "last_run_ts_ns": 0,
                "last_summary": {
                    "promoted": "0",  # string
                    "held": "7",  # string
                    "dropped": "1",
                    "score_only": "5",
                    "recall_only": "1",
                    "both_gates": "1",
                    "diversity_fail": "1",
                    "evaluated": "8",
                    "catch_up_run": False,
                    "run_ts_ns": 0,
                },
            }
        )
    )
    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    assert "Traceback" not in result.stdout
    assert "held=7" in result.stdout


def test_safe_int_handles_adversarial_values() -> None:
    """The int coercion helper is the load-bearing input validator."""
    from opencomputer.evolution.cli import _safe_int

    assert _safe_int(None) == 0
    assert _safe_int(None, default=7) == 7
    assert _safe_int(0) == 0
    assert _safe_int(42) == 42
    assert _safe_int(3.7) == 3
    assert _safe_int(True) == 1
    assert _safe_int(False) == 0
    assert _safe_int("42") == 42
    assert _safe_int("3.7") == 3
    assert _safe_int("not a number") == 0
    assert _safe_int("not a number", default=99) == 99
    assert _safe_int(["list"]) == 0
    assert _safe_int({"dict": 1}) == 0
    assert _safe_int(object()) == 0


def test_dashboard_tolerates_last_summary_as_list(isolated_home: Path) -> None:
    """Adversarial: last_summary is a JSON list, not dict. Must not crash."""
    state_path = isolated_home / "cron" / "dreaming_v2_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "processed_event_ids": [],
                "last_run_ts_ns": 0,
                "last_summary": [1, 2, 3],
            }
        )
    )

    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    assert "Traceback" not in result.stdout
    assert "last_summary shape unexpected" in result.stdout or "—" in result.stdout


def test_dashboard_tolerates_non_utf8_state_json(isolated_home: Path) -> None:
    """Adversarial: state.json contains non-UTF-8 bytes (e.g. latin-1 garbage).
    Naive ``read_text(encoding='utf-8')`` would raise UnicodeDecodeError —
    must be caught explicitly so the rest of the dashboard still renders.
    """
    state_path = isolated_home / "cron" / "dreaming_v2_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # 0xFF is invalid as the start byte of any UTF-8 sequence.
    state_path.write_bytes(b'{"processed_event_ids": []\xff}')

    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    assert "Traceback" not in result.stdout


def test_dashboard_renders_catch_up_run_when_true(isolated_home: Path) -> None:
    """A catch-up tick (after a missed cron interval) must surface in the
    dashboard so the operator can correlate odd batch sizes with outages.
    """
    state_path = isolated_home / "cron" / "dreaming_v2_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "processed_event_ids": [],
                "last_run_ts_ns": 0,
                "last_summary": {
                    "promoted": 1,
                    "held": 0,
                    "dropped": 0,
                    "score_only": 0,
                    "recall_only": 0,
                    "both_gates": 0,
                    "unattributed": 0,
                    "diversity_fail": 0,
                    "evaluated": 1,
                    "catch_up_run": True,
                    "run_ts_ns": 0,
                },
            }
        )
    )

    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    assert "catch-up" in result.stdout.lower()


def test_summarize_run_warns_on_unattributed_held_rationale(
    caplog,
) -> None:
    """If the engine's rationale format drifts (HELD with neither
    ``score=…<…`` nor ``recall=…<…``), ``summarize_run_for_state`` must
    WARN — never silently under-count the disjoint breakdown.
    """
    import logging

    from opencomputer.cron.dreaming_v2_tick import summarize_run_for_state

    cand = DreamCandidate(event_id="z", raw_text="", timestamp_ns=0)
    summary = DreamRunSummary(
        held=(
            DreamGateResult(
                candidate=cand,
                outcome=DreamOutcome.HELD,
                score=0.9,
                recall_count=2,
                diversity_score=0.4,
                # Rationale string with no score=/recall= markers — drifted.
                rationale="held: some new reason format",
            ),
        ),
        total_evaluated=1,
    )

    with caplog.at_level(logging.WARNING, logger="opencomputer.cron.dreaming_v2_tick"):
        out = summarize_run_for_state(summary, run_ts_ns=0)

    assert out["held"] == 1
    assert out["score_only"] == 0
    assert out["recall_only"] == 0
    assert out["both_gates"] == 0
    assert out["unattributed"] == 1
    # Invariant still holds with unattributed bucket.
    assert (
        out["score_only"] + out["recall_only"] + out["both_gates"] + out["unattributed"]
        == out["held"]
    )
    assert any("unattributed" in r.message.lower() or "did not match" in r.message
               for r in caplog.records)
