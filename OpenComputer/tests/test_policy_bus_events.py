"""P2-12: PolicyChangeEvent + PolicyRevertedEvent on the default bus."""
from __future__ import annotations

import time

import pytest

from opencomputer.agent.feature_flags import FeatureFlags
from opencomputer.agent.state import SessionDB
from opencomputer.cron.auto_revert import run_auto_revert_due
from opencomputer.cron.policy_engine_tick import run_engine_tick
from opencomputer.ingestion.bus import get_default_bus
from plugin_sdk.ingestion import (
    PolicyChangeEvent,
    PolicyRevertedEvent,
)


def _seed_underperforming(db, name="low", n_cites=8):
    sid = f"sess_{name}"
    db.create_session(sid, platform="cli", model="m")
    ep = db.record_episodic(session_id=sid, turn_index=0, summary="x")
    with db._connect() as conn:
        for i in range(n_cites):
            tsid = f"s_{name}_{i}"
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, started_at, platform, "
                "model) VALUES (?, ?, 'cli', 'm')",
                (tsid, time.time() - 86400),
            )
            conn.execute(
                "INSERT INTO turn_outcomes (id, session_id, turn_index, "
                "created_at, turn_score) VALUES (?, ?, ?, ?, 0.30)",
                (f"to_{name}_{i}", tsid, i, time.time() - 86400),
            )
            conn.execute(
                "INSERT INTO recall_citations (id, session_id, turn_index, "
                "episodic_event_id, candidate_kind, candidate_text_id, "
                "bm25_score, adjusted_score, retrieved_at) VALUES "
                "(?, ?, ?, ?, 'episodic', NULL, -1.0, -1.0, ?)",
                (f"rc_{name}_{i}", tsid, i, ep, time.time() - 86400),
            )
        for j in range(2):
            other_sid = f"sess_o_{j}"
            db.create_session(other_sid, platform="cli", model="m")
            o_ep = db.record_episodic(
                session_id=other_sid, turn_index=0, summary=f"o{j}",
            )
            for i in range(5):
                tsid = f"so_{j}_{i}"
                conn.execute(
                    "INSERT OR IGNORE INTO sessions (id, started_at, platform, "
                    "model) VALUES (?, ?, 'cli', 'm')",
                    (tsid, time.time() - 86400),
                )
                conn.execute(
                    "INSERT INTO turn_outcomes (id, session_id, turn_index, "
                    "created_at, turn_score) VALUES (?, ?, ?, ?, 0.7)",
                    (f"to_o_{j}_{i}", tsid, i, time.time() - 86400),
                )
                conn.execute(
                    "INSERT INTO recall_citations (id, session_id, turn_index, "
                    "episodic_event_id, candidate_kind, candidate_text_id, "
                    "bm25_score, adjusted_score, retrieved_at) VALUES "
                    "(?, ?, ?, ?, 'episodic', NULL, -1.0, -1.0, ?)",
                    (f"rc_o_{j}_{i}", tsid, i, o_ep, time.time() - 86400),
                )
    return ep


@pytest.mark.asyncio
async def test_engine_tick_publishes_policy_change_event(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 100)
    _seed_underperforming(db)

    received: list[PolicyChangeEvent] = []
    bus = get_default_bus()
    sub = bus.subscribe(
        "policy_change", lambda e: received.append(e),
    )

    run_engine_tick(db=db, flags=flags, hmac_key=b"k" * 32)

    assert len(received) == 1
    evt = received[0]
    assert evt.event_type == "policy_change"
    assert evt.knob_kind == "recall_penalty"
    assert evt.status == "pending_approval"
    assert evt.approval_mode == "explicit"
    assert evt.engine_version == "MostCitedBelowMedian/1"

    sub.unsubscribe()


@pytest.mark.asyncio
async def test_auto_revert_publishes_reverted_event(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    db.create_session("ses_t", platform="cli", model="m")
    ep = db.record_episodic(session_id="ses_t", turn_index=0, summary="x")

    applied = time.time() - 86400
    with db._connect() as conn:
        conn.execute(
            "UPDATE episodic_events SET recall_penalty = 0.2, "
            "recall_penalty_updated_at = ? WHERE id = ?",
            (applied, ep),
        )
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status, pre_change_baseline_mean, "
            "pre_change_baseline_std) VALUES ('c1', ?, ?, 'recall_penalty', ?, "
            "'{\"recall_penalty\":0.0}', '{\"recall_penalty\":0.2}', 'r', 'e', "
            "'{\"action\":\"set\",\"field\":\"recall_penalty\","
            "\"value\":0.0}', 'MostCitedBelowMedian/1', 'auto_ttl', '0', "
            "'h', 'pending_evaluation', 0.6, 0.1)",
            (applied, applied, ep),
        )
        db.create_session("ses_p", platform="cli", model="m")
        for i in range(12):
            conn.execute(
                "INSERT INTO turn_outcomes (id, session_id, turn_index, "
                "created_at, turn_score) VALUES (?, ?, ?, ?, 0.4)",
                (f"tp_{i}", "ses_p", i, applied + 600 * i),
            )

    received: list[PolicyRevertedEvent] = []
    bus = get_default_bus()
    sub = bus.subscribe(
        "policy_reverted", lambda e: received.append(e),
    )

    run_auto_revert_due(db=db, flags=flags, hmac_key=b"k" * 32)

    assert len(received) == 1
    assert received[0].change_id == "c1"
    assert "statistical" in received[0].reverted_reason.lower()

    sub.unsubscribe()
