"""P2-5: MostCitedBelowMedian/1 recommendation engine."""
from __future__ import annotations

import time

from opencomputer.agent.state import SessionDB
from opencomputer.evolution.policy_engine import MostCitedBelowMedianV1
from opencomputer.evolution.recommendation import NoOpReason


def _seed_episodic(db, name, ts=None):
    """Create an episodic row with the SessionDB API; returns its
    auto-generated INTEGER id (which we then refer to from
    recall_citations)."""
    ts = ts or (time.time() - 86400)
    db.create_session(f"sess_for_{name}", platform="cli", model="m")
    return db.record_episodic(
        session_id=f"sess_for_{name}", turn_index=0, summary=f"sum_{name}",
    )


def _seed_citation_with_score(db, ep_id, session, turn_idx, score):
    """Insert (turn_outcomes row) + (recall_citations linkage) so the
    engine sees one citation of ep_id with given turn_score."""
    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, started_at, platform, model) "
            "VALUES (?, ?, 'cli', 'm')",
            (session, now - 86400),
        )
        to_id = f"to_{ep_id}_{session}_{turn_idx}"
        conn.execute(
            "INSERT INTO turn_outcomes (id, session_id, turn_index, "
            "created_at, turn_score) VALUES (?, ?, ?, ?, ?)",
            (to_id, session, turn_idx, now - 86400, score),
        )
        conn.execute(
            "INSERT INTO recall_citations (id, session_id, turn_index, "
            "episodic_event_id, candidate_kind, candidate_text_id, "
            "bm25_score, adjusted_score, retrieved_at) VALUES "
            "(?, ?, ?, ?, 'episodic', NULL, -1.0, -1.0, ?)",
            (f"rc_{to_id}", session, turn_idx, ep_id, now - 86400),
        )


def _seed_memory_with_n_citations(db, name, n_cites, mean_score):
    """Returns the integer episodic id for downstream lookups."""
    ep_id = _seed_episodic(db, name)
    for i in range(n_cites):
        _seed_citation_with_score(db, ep_id, f"s_{name}_{i}", i, mean_score)
    return ep_id


def test_engine_returns_insufficient_data_when_no_citations(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    engine = MostCitedBelowMedianV1()
    rec = engine.recommend(db)
    assert rec.is_noop()
    assert rec.noop_reason == NoOpReason.INSUFFICIENT_DATA


def test_engine_returns_noop_on_quiet_corpus(tmp_path):
    """All memories scoring well — no candidate gap > threshold."""
    db = SessionDB(tmp_path / "s.db")
    for i in range(5):
        _seed_memory_with_n_citations(db, f"ep_{i}", n_cites=5, mean_score=0.7)

    engine = MostCitedBelowMedianV1(deviation_threshold=0.10)
    rec = engine.recommend(db)
    assert rec.is_noop()
    assert rec.noop_reason == NoOpReason.NO_CANDIDATE_BELOW_THRESHOLD


def test_engine_picks_lowest_mean_when_signal_present(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    _seed_memory_with_n_citations(db, "high", n_cites=5, mean_score=0.7)
    low_id = _seed_memory_with_n_citations(db, "low", n_cites=8, mean_score=0.30)
    _seed_memory_with_n_citations(db, "mid", n_cites=5, mean_score=0.55)

    engine = MostCitedBelowMedianV1(min_citations=5, deviation_threshold=0.10)
    rec = engine.recommend(db)
    assert not rec.is_noop()
    assert int(rec.target_id) == int(low_id)
    assert rec.knob_kind == "recall_penalty"
    assert rec.engine_version == "MostCitedBelowMedian/1"
    assert rec.new_value["recall_penalty"] > rec.prev_value["recall_penalty"]


def test_engine_respects_cooldown(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    low_id = _seed_memory_with_n_citations(db, "low", n_cites=8, mean_score=0.30)
    # Set as recently penalised — within cooldown
    with db._connect() as conn:
        conn.execute(
            "UPDATE episodic_events SET recall_penalty = 0.2, "
            "recall_penalty_updated_at = ? WHERE id = ?",
            (time.time() - 3 * 86400, low_id),
        )

    engine = MostCitedBelowMedianV1(
        min_citations=5, cooldown_days=7, deviation_threshold=0.10,
    )
    rec = engine.recommend(db)
    assert rec.is_noop()
    assert rec.noop_reason == NoOpReason.ALL_CANDIDATES_IN_COOLDOWN


def test_engine_caps_penalty_at_080(tmp_path):
    """Even repeated recommendations cap at 0.80, leaving recovery room."""
    db = SessionDB(tmp_path / "s.db")
    low_id = _seed_memory_with_n_citations(db, "low", n_cites=8, mean_score=0.30)
    _seed_memory_with_n_citations(db, "h1", n_cites=5, mean_score=0.7)
    _seed_memory_with_n_citations(db, "h2", n_cites=5, mean_score=0.7)
    with db._connect() as conn:
        conn.execute(
            "UPDATE episodic_events SET recall_penalty = 0.7, "
            "recall_penalty_updated_at = ? WHERE id = ?",
            (time.time() - 30 * 86400, low_id),
        )

    engine = MostCitedBelowMedianV1(
        min_citations=5, cooldown_days=7,
        deviation_threshold=0.10, penalty_step=0.20, penalty_cap=0.80,
    )
    rec = engine.recommend(db)
    if not rec.is_noop():
        assert rec.new_value["recall_penalty"] <= 0.80
