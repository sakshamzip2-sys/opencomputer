"""P2-14: full Phase 2 v0 reversibility loop, end to end.

Walks the system through:
  1. Seed corpus with one underperforming + healthy peers + recall_citations.
  2. Phase A engine_tick → pending_approval row, HMAC chain valid.
  3. Telegram subscriber receives pending_approval event.
  4. /policy-approve → pending_evaluation, recall_penalty applied.
  5. Seed degraded post-change turns (N≥10, mean below baseline-1σ).
  6. auto_revert → reverted, penalty rolled back, PolicyRevertedEvent fires.
  7. HMAC chain still valid after revert.
"""
from __future__ import annotations

import sys
import time

import pytest

from opencomputer.agent.feature_flags import FeatureFlags
from opencomputer.agent.policy_audit import PolicyAuditLogger
from opencomputer.agent.slash_commands_impl.policy import (
    handle_policy_approve,
)
from opencomputer.agent.state import SessionDB
from opencomputer.cron.auto_revert import run_auto_revert_due
from opencomputer.cron.policy_engine_tick import (
    EngineTickResult,
    run_engine_tick,
)
from opencomputer.ingestion.bus import get_default_bus
from plugin_sdk.ingestion import (
    PolicyChangeEvent,
    PolicyRevertedEvent,
)


def _hmac():
    return b"k" * 32


def _seed_corpus(db):
    """One underperforming memory + 2 healthy peers, all with citations."""
    sid_low = "ses_low"
    db.create_session(sid_low, platform="cli", model="m")
    ep_low = db.record_episodic(
        session_id=sid_low, turn_index=0, summary="low",
    )

    with db._connect() as conn:
        # 8 citations of ep_low at score 0.30
        for i in range(8):
            tsid = f"s_low_{i}"
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, started_at, platform, "
                "model) VALUES (?, ?, 'cli', 'm')",
                (tsid, time.time() - 86400),
            )
            conn.execute(
                "INSERT INTO turn_outcomes (id, session_id, turn_index, "
                "created_at, turn_score) VALUES (?, ?, ?, ?, 0.30)",
                (f"to_low_{i}", tsid, i, time.time() - 86400),
            )
            conn.execute(
                "INSERT INTO recall_citations (id, session_id, turn_index, "
                "episodic_event_id, candidate_kind, candidate_text_id, "
                "bm25_score, adjusted_score, retrieved_at) VALUES "
                "(?, ?, ?, ?, 'episodic', NULL, -1.0, -1.0, ?)",
                (f"rc_low_{i}", tsid, i, ep_low, time.time() - 86400),
            )

        # Healthy peers
        for j in range(2):
            sid_h = f"ses_h_{j}"
            db.create_session(sid_h, platform="cli", model="m")
            ep_h = db.record_episodic(
                session_id=sid_h, turn_index=0, summary=f"h_{j}",
            )
            for i in range(5):
                tsid = f"s_h_{j}_{i}"
                conn.execute(
                    "INSERT OR IGNORE INTO sessions (id, started_at, platform, "
                    "model) VALUES (?, ?, 'cli', 'm')",
                    (tsid, time.time() - 86400),
                )
                conn.execute(
                    "INSERT INTO turn_outcomes (id, session_id, turn_index, "
                    "created_at, turn_score) VALUES (?, ?, ?, ?, 0.7)",
                    (f"to_h_{j}_{i}", tsid, i, time.time() - 86400),
                )
                conn.execute(
                    "INSERT INTO recall_citations (id, session_id, turn_index, "
                    "episodic_event_id, candidate_kind, candidate_text_id, "
                    "bm25_score, adjusted_score, retrieved_at) VALUES "
                    "(?, ?, ?, ?, 'episodic', NULL, -1.0, -1.0, ?)",
                    (f"rc_h_{j}_{i}", tsid, i, ep_h, time.time() - 86400),
                )
    return ep_low


@pytest.mark.asyncio
async def test_full_loop_phase_a_approval_then_statistical_revert(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.enabled", True)
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 100)
    flags.write("policy_engine.daily_change_budget", 5)
    flags.write("policy_engine.min_eligible_turns_for_revert", 10)

    ep_low = _seed_corpus(db)

    # ── Phase A: engine drafts, lands as pending_approval ──
    bus = get_default_bus()
    received_changes: list[PolicyChangeEvent] = []
    sub_change = bus.subscribe(
        "policy_change", lambda e: received_changes.append(e),
    )

    result = run_engine_tick(db=db, flags=flags, hmac_key=_hmac())
    assert result == EngineTickResult.DRAFTED_PENDING

    with db._connect() as conn:
        row = conn.execute(
            "SELECT id, status, approval_mode FROM policy_changes "
            "ORDER BY ts_drafted DESC LIMIT 1"
        ).fetchone()
        change_id = row["id"]
    assert row["status"] == "pending_approval"
    assert row["approval_mode"] == "explicit"

    # Bus event fired
    assert len(received_changes) == 1
    assert received_changes[0].status == "pending_approval"

    # HMAC chain valid
    with db._connect() as conn:
        log = PolicyAuditLogger(conn, _hmac())
        assert log.verify_chain() is True

    # ── User approves ──
    out = await handle_policy_approve(
        db=db, args=change_id, hmac_key=_hmac(),
    )
    assert "approved" in out.text.lower()

    with db._connect() as conn:
        row = conn.execute(
            "SELECT status FROM policy_changes WHERE id = ?",
            (change_id,),
        ).fetchone()
        penalty = conn.execute(
            "SELECT recall_penalty FROM episodic_events WHERE id = ?",
            (ep_low,),
        ).fetchone()[0]
    assert row["status"] == "pending_evaluation"
    assert abs(penalty - 0.20) < 1e-9

    # ── Seed degraded post-change turns ──
    applied_at = time.time() - 3600
    with db._connect() as conn:
        # Re-set ts_applied so post turns are after it
        conn.execute(
            "UPDATE policy_changes SET ts_applied = ?, "
            "pre_change_baseline_mean = 0.6, pre_change_baseline_std = 0.1 "
            "WHERE id = ?",
            (applied_at, change_id),
        )
        db.create_session("ses_degraded", platform="cli", model="m")
        for i in range(12):
            conn.execute(
                "INSERT INTO turn_outcomes (id, session_id, turn_index, "
                "created_at, turn_score) VALUES (?, ?, ?, ?, 0.4)",
                (f"to_post_{i}", "ses_degraded", i,
                 applied_at + 60 * (i + 1)),
            )

    # ── auto_revert fires ──
    received_reverts: list[PolicyRevertedEvent] = []
    sub_revert = bus.subscribe(
        "policy_reverted", lambda e: received_reverts.append(e),
    )

    transitions = run_auto_revert_due(
        db=db, flags=flags, hmac_key=_hmac(),
    )
    assert transitions >= 1

    with db._connect() as conn:
        row = conn.execute(
            "SELECT status, reverted_reason FROM policy_changes WHERE id = ?",
            (change_id,),
        ).fetchone()
        penalty = conn.execute(
            "SELECT recall_penalty FROM episodic_events WHERE id = ?",
            (ep_low,),
        ).fetchone()[0]
    assert row["status"] == "reverted"
    assert "statistical" in (row["reverted_reason"] or "").lower()
    assert penalty == 0.0  # rolled back

    # PolicyRevertedEvent fired
    assert len(received_reverts) == 1
    assert received_reverts[0].change_id == change_id

    # Chain STILL valid after the revert
    with db._connect() as conn:
        log = PolicyAuditLogger(conn, _hmac())
        assert log.verify_chain() is True

    sub_change.unsubscribe()
    sub_revert.unsubscribe()


@pytest.mark.asyncio
async def test_telegram_notifier_pings_on_pending_approval(tmp_path):
    """End-to-end: engine_tick → bus → telegram notifier → admin DM."""
    sys.path.insert(0, "extensions/telegram")
    from policy_notifier import register_policy_notifier  # noqa: E402

    db = SessionDB(tmp_path / "s.db")
    flags = FeatureFlags(tmp_path / "f.json")
    flags.write("policy_engine.auto_approve_after_n_safe_decisions", 100)
    _seed_corpus(db)

    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    bus = get_default_bus()
    sub = register_policy_notifier(
        bus=bus, admin_chat_id="42", send_fn=fake_send,
    )

    import asyncio
    result = run_engine_tick(db=db, flags=flags, hmac_key=_hmac())
    assert result == EngineTickResult.DRAFTED_PENDING
    # Notifier handler is sync but spawns the actual send via
    # asyncio.create_task — yield to the loop so it runs.
    await asyncio.sleep(0)

    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == "42"
    assert "policy-approve" in text.lower()
    assert "MostCitedBelowMedian/1" in text

    sub.unsubscribe()
