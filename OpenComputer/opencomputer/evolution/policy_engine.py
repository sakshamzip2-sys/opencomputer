"""Phase 2 v0 recommendation engine: ``MostCitedBelowMedian/1``.

EXPLICITLY DUMB. v0-on-purpose. Does not learn. Does not detect drift.
Replaceable: future engines (e.g. ``OutcomeRegressionTree/1``) emit a
different ``recommendation_engine_version`` into ``policy_changes`` so
cohorts can be A/B compared without losing historical context.

Algorithm:

    eligibility:
        - Memory was cited (returned by recall_synthesizer) at least
          ``min_citations`` times in the last 14 days.
        - AND not adjusted in the last ``cooldown_days`` (avoid
          double-penalising same memory before signal stabilises).

    selection:
        - Compute mean downstream turn_score across each candidate's
          citation turns.
        - Pick the candidate with the LOWEST mean.
        - Tie-breakers: higher citation count, older
          recall_penalty_updated_at.

    no-op gate:
        - If gap (corpus_median - candidate_mean) <
          deviation_threshold, recommend zero changes.

    recommendation:
        - Increase recall_penalty by penalty_step (default +0.20).
        - Cap total at penalty_cap (default 0.80) leaving recovery room.
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass

from opencomputer.evolution.recommendation import (
    NoOpReason,
    Recommendation,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MostCitedBelowMedianV1:
    min_citations: int = 5
    cooldown_days: float = 7.0
    deviation_threshold: float = 0.10
    penalty_step: float = 0.20
    penalty_cap: float = 0.80

    @property
    def version(self) -> str:
        return "MostCitedBelowMedian/1"

    def recommend(self, db) -> Recommendation:
        cutoff = time.time() - 14 * 86400
        cooldown_cutoff = time.time() - self.cooldown_days * 86400

        with db._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    e.id,
                    e.recall_penalty,
                    e.recall_penalty_updated_at,
                    COUNT(DISTINCT (t.session_id || '|' || t.turn_index)) AS citation_count,
                    AVG(t.turn_score) AS mean_score
                FROM episodic_events e
                JOIN recall_citations rc ON rc.episodic_event_id = e.id
                JOIN turn_outcomes t
                    ON t.session_id = rc.session_id
                    AND t.turn_index = rc.turn_index
                    AND t.turn_score IS NOT NULL
                WHERE rc.retrieved_at >= ?
                GROUP BY e.id
                HAVING citation_count >= ?
                  AND mean_score IS NOT NULL
                  AND (e.recall_penalty_updated_at IS NULL
                       OR e.recall_penalty_updated_at < ?)
                """,
                (cutoff, self.min_citations, cooldown_cutoff),
            ).fetchall()

        if not rows:
            # Distinguish "not enough data" from "everyone in cooldown"
            with db._connect() as conn:
                any_eligible = conn.execute(
                    "SELECT 1 FROM episodic_events e "
                    "JOIN recall_citations rc ON rc.episodic_event_id = e.id "
                    "WHERE rc.retrieved_at >= ? LIMIT 1",
                    (cutoff,),
                ).fetchone()
            if any_eligible:
                return Recommendation.noop(NoOpReason.ALL_CANDIDATES_IN_COOLDOWN)
            return Recommendation.noop(NoOpReason.INSUFFICIENT_DATA)

        scores = [r["mean_score"] for r in rows]
        if len(scores) < 3:
            return Recommendation.noop(NoOpReason.INSUFFICIENT_DATA)
        corpus_median = statistics.median(scores)

        # Sort: lowest mean first; ties broken by higher citation count,
        # then older recall_penalty_updated_at.
        rows_sorted = sorted(
            rows,
            key=lambda r: (
                r["mean_score"],
                -r["citation_count"],
                r["recall_penalty_updated_at"] or 0,
            ),
        )
        winner = rows_sorted[0]
        ep_id = winner["id"]
        prev_penalty = winner["recall_penalty"] or 0.0
        n_cites = winner["citation_count"]
        mean_score = winner["mean_score"]

        gap = corpus_median - mean_score
        if gap < self.deviation_threshold:
            return Recommendation.noop(NoOpReason.NO_CANDIDATE_BELOW_THRESHOLD)

        new_penalty = min(self.penalty_cap, prev_penalty + self.penalty_step)
        if new_penalty <= prev_penalty:
            return Recommendation.noop(NoOpReason.NO_CANDIDATE_BELOW_THRESHOLD)

        return Recommendation(
            knob_kind="recall_penalty",
            target_id=ep_id,
            prev_value={"recall_penalty": prev_penalty},
            new_value={"recall_penalty": new_penalty},
            reason=(
                f"{self.version}: cited {n_cites}× in 14d, "
                f"mean turn_score {mean_score:.3f} vs corpus median "
                f"{corpus_median:.3f} (gap {gap:.3f} > threshold "
                f"{self.deviation_threshold:.2f})"
            ),
            expected_effect=(
                "reduce surfacing of low-utility memory; expect mean "
                "turn_score on subsequent eligibility set to rise toward "
                "corpus median"
            ),
            engine_version=self.version,
            rollback_hook={
                "action": "set",
                "field": "recall_penalty",
                "value": prev_penalty,
            },
        )
