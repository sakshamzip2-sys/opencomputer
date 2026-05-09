"""Dreaming v2 — three-gate consolidation INTO MEMORY.md (v1.1 plan-3 M6.4).

Distinct from :mod:`opencomputer.agent.dreaming` (Round 2A P-18 episodic
clustering, which summarizes old episodic events into per-cluster
synopses inside SessionDB).  Dreaming v2 promotes high-signal episodic
events into the user's declarative MEMORY.md so future turns can
recall them via the M6.1/M6.2/M6.3 active-memory layers.

The three gates (Plan 3 M6.4 spec):

1. **Score gate** — an aux-LLM judges importance 0-1.  Events below
   ``score_threshold`` (default 0.65) are dropped or held.
2. **Recall-count gate** — count of cross-session recalls of this
   event via the ``recall_citations`` table.  Below ``min_recall_count``
   (default 2) means the user / agent never came back to this fact;
   probably not worth promoting.
3. **Diversity gate** — cosine similarity to the nearest existing
   MEMORY.md entry.  Above ``diversity_threshold`` (default 0.8) means
   it's effectively a duplicate; reject.

Routing:

- All three gates pass → promote to MEMORY.md (capped at
  ``max_promotions_per_run``).
- Score fails BUT recall-count + diversity pass → write to DREAMS.md
  (the lower-confidence holding pen, capped at ``dreams_md_max_bytes``).
- Diversity fails → drop with audit log line.

Operational:

- **Idempotency** via ``event_id`` deduplication: the runner remembers
  which events it already processed (by sha256 of canonical event
  string) and skips duplicates in the same DB.
- **Cron-miss catch-up** (carry-forward audit note from M6.1
  brainstorm).  If ``last_successful_run`` is older than
  ``2 * cron_interval``, the next invocation does ONE catch-up pass
  with a higher event-fetch limit before the normal pass.  Capped at
  one catch-up per real run so a long outage doesn't loop forever.

This module is the *engine*.  The cron registration + production
audit-log integration land in their separate skill/CLI files; this
module is fully testable in isolation by injecting the score / recall /
embed callables.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from plugin_sdk.embeddings import EmbeddingBatch, EmbeddingsUnsupportedError

logger = logging.getLogger("opencomputer.agent.dreaming_v2")


class DreamOutcome(Enum):
    """Routing decision for a single candidate event."""

    PROMOTED = "promoted"  # passed all 3 gates → MEMORY.md
    HELD = "held"          # failed score but passed others → DREAMS.md
    DROPPED = "dropped"    # failed diversity → drop + audit


@dataclass(frozen=True, slots=True)
class DreamCandidate:
    """One episodic event under evaluation.

    ``event_id`` is a stable hash of the event used for idempotency.
    ``raw_text`` is the prose the candidate would become as a
    MEMORY.md entry — the gates score this directly.
    """

    event_id: str
    raw_text: str
    timestamp_ns: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DreamGateResult:
    """Per-candidate gate outcome with full rationale for audit."""

    candidate: DreamCandidate
    outcome: DreamOutcome
    score: float
    recall_count: int
    diversity_score: float  # cosine to nearest existing memory; 1.0 = no embeddings
    rationale: str


@dataclass(frozen=True, slots=True)
class DreamRunSummary:
    """Aggregate result of one Dreaming v2 pass."""

    promoted: tuple[DreamGateResult, ...] = ()
    held: tuple[DreamGateResult, ...] = ()
    dropped: tuple[DreamGateResult, ...] = ()
    skipped_already_processed: int = 0
    total_evaluated: int = 0
    catch_up_run: bool = False
    """True when this run was a catch-up pass after a missed cron
    interval (cron-miss recovery; carry-forward audit note from
    M6.1 brainstorm)."""


@dataclass(frozen=True, slots=True)
class DreamingV2Config:
    """Tunables for the M6.4 Dreaming pipeline.

    Defaults match the Plan 3 spec verbatim.  Cron-miss thresholds are
    derived from the cron interval at runtime — pass it in from the
    cron skill / scheduler.
    """

    enabled: bool = False  # opt-in initially per Plan 3
    score_threshold: float = 0.65
    min_recall_count: int = 2
    diversity_threshold: float = 0.8
    """Cosine similarity above this is rejected as too-similar."""
    max_promotions_per_run: int = 20
    dreams_md_max_bytes: int = 16384
    """Hard cap on DREAMS.md.  Oldest entries evict on overflow."""
    cron_miss_factor: float = 2.0
    """If last_run > cron_miss_factor * cron_interval, run one catch-up."""


# ─── injectable callables (testability) ────────────────────────────


# Returns 0-1 importance score for one candidate's raw_text.
ScoreFn = Callable[[str], Awaitable[float]]
# Returns the cross-session recall count for a given event_id.
RecallCountFn = Callable[[str], int]
# Returns embeddings for a list of texts (the M6.6 contract).
EmbedFn = Callable[[list[str]], Awaitable[EmbeddingBatch]]
# Persists a promoted candidate to MEMORY.md.  Caller-provided so
# tests can capture without writing real files.
PromoteFn = Callable[[str], None]
# Persists a held candidate to DREAMS.md.
HoldFn = Callable[[str, int], None]  # (text, dreams_max_bytes)


# ─── pipeline ──────────────────────────────────────────────────────


def _hash_event_for_dedup(text: str) -> str:
    """Stable id for an event used for dedup across runs.  Sha256 over
    raw text — the same fact phrased identically dedupes; trivial
    rephrasings won't but the diversity gate catches those."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length float vectors."""
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    a_norm = 0.0
    b_norm = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        a_norm += x * x
        b_norm += y * y
    if a_norm == 0.0 or b_norm == 0.0:
        return 0.0
    return dot / ((a_norm**0.5) * (b_norm**0.5))


async def _max_diversity_against(
    candidate_text: str,
    existing_memories: list[str],
    *,
    embed_fn: EmbedFn | None,
) -> float:
    """Return the maximum cosine similarity between the candidate and
    any existing MEMORY.md entry.  Higher = more similar = more likely
    duplicate.

    When ``embed_fn`` is None or raises ``EmbeddingsUnsupportedError``,
    returns 0.0 (treats every candidate as "novel enough").  This is
    the BM25-only-fallback degraded mode for providers without
    embeddings — better to over-promote than to silently drop
    novel facts because we can't compute similarity.
    """
    if not existing_memories:
        return 0.0
    if embed_fn is None:
        return 0.0
    try:
        batch = await embed_fn([candidate_text, *existing_memories])
    except EmbeddingsUnsupportedError:
        logger.debug(
            "Dreaming v2 diversity gate: provider lacks embeddings; "
            "candidates will not be checked for duplication"
        )
        return 0.0
    except Exception as exc:  # noqa: BLE001 — never crash the pipeline
        logger.warning(
            "Dreaming v2 diversity-gate embed call failed (%s: %s); "
            "treating as novel",
            type(exc).__name__,
            exc,
        )
        return 0.0
    if not batch.vectors or len(batch.vectors) < 1 + len(existing_memories):
        return 0.0
    cand_vec = batch.vectors[0]
    max_sim = 0.0
    for mem_vec in batch.vectors[1:]:
        sim = _cosine(cand_vec, mem_vec)
        if sim > max_sim:
            max_sim = sim
    return max_sim


@dataclass
class DreamingPipeline:
    """The three-gate consolidation engine.

    Pure orchestration; stateless except for an optional in-memory
    ``processed_event_ids`` set kept by the caller across runs (the
    real cron skill persists it in the audit table).
    """

    config: DreamingV2Config
    score_fn: ScoreFn
    recall_count_fn: RecallCountFn
    embed_fn: EmbedFn | None
    promote_fn: PromoteFn
    hold_fn: HoldFn
    last_successful_run_ts_ns: int | None = None
    cron_interval_seconds: float = 24 * 60 * 60  # default daily

    async def run_once(
        self,
        candidates: list[DreamCandidate],
        *,
        existing_memories: list[str],
        already_processed_event_ids: set[str] | None = None,
    ) -> DreamRunSummary:
        """Evaluate candidates through three gates; route accordingly.

        Returns a :class:`DreamRunSummary` regardless of outcomes — the
        caller persists it for audit.  Idempotent: candidates whose
        ``event_id`` appears in ``already_processed_event_ids`` are
        skipped and counted in ``skipped_already_processed``.
        """
        if not self.config.enabled:
            logger.info("Dreaming v2 disabled by config; skipping run")
            return DreamRunSummary()

        skip_set = already_processed_event_ids or set()
        promoted: list[DreamGateResult] = []
        held: list[DreamGateResult] = []
        dropped: list[DreamGateResult] = []
        skipped = 0

        catch_up = self._is_catch_up_due()

        for cand in candidates:
            if cand.event_id in skip_set:
                skipped += 1
                continue

            if len(promoted) >= self.config.max_promotions_per_run:
                # Hit promotion cap; remaining candidates roll over to
                # next run.  Don't even score them — saves aux-LLM cost.
                break

            # Score gate
            try:
                score = float(await self.score_fn(cand.raw_text))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dreaming v2 score_fn raised %s: %s; treating as 0.0",
                    type(exc).__name__,
                    exc,
                )
                score = 0.0
            score = max(0.0, min(1.0, score))  # clamp

            # Recall-count gate
            try:
                recall_count = int(self.recall_count_fn(cand.event_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dreaming v2 recall_count_fn raised %s: %s; treating as 0",
                    type(exc).__name__,
                    exc,
                )
                recall_count = 0
            recall_count = max(0, recall_count)

            # Diversity gate (against current MEMORY.md)
            diversity = await _max_diversity_against(
                cand.raw_text,
                existing_memories,
                embed_fn=self.embed_fn,
            )

            # Routing logic
            score_ok = score >= self.config.score_threshold
            recall_ok = recall_count >= self.config.min_recall_count
            diversity_ok = diversity < self.config.diversity_threshold

            if not diversity_ok:
                # Hard reject — too similar to an existing memory.
                outcome = DreamOutcome.DROPPED
                rationale = (
                    f"diversity gate failed: cosine={diversity:.3f} "
                    f">= threshold={self.config.diversity_threshold}"
                )
                dropped.append(
                    DreamGateResult(
                        candidate=cand,
                        outcome=outcome,
                        score=score,
                        recall_count=recall_count,
                        diversity_score=diversity,
                        rationale=rationale,
                    )
                )
                continue

            if score_ok and recall_ok:
                # Promoted to MEMORY.md.
                outcome = DreamOutcome.PROMOTED
                rationale = (
                    f"all gates passed: score={score:.2f}, "
                    f"recall={recall_count}, diversity={diversity:.3f}"
                )
                try:
                    self.promote_fn(cand.raw_text)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Dreaming v2 promote_fn raised %s: %s; "
                        "downgrading to HELD",
                        type(exc).__name__,
                        exc,
                    )
                    held.append(
                        DreamGateResult(
                            candidate=cand,
                            outcome=DreamOutcome.HELD,
                            score=score,
                            recall_count=recall_count,
                            diversity_score=diversity,
                            rationale=(
                                f"promote failed ({type(exc).__name__}); "
                                f"holding instead"
                            ),
                        )
                    )
                    continue
                promoted.append(
                    DreamGateResult(
                        candidate=cand,
                        outcome=outcome,
                        score=score,
                        recall_count=recall_count,
                        diversity_score=diversity,
                        rationale=rationale,
                    )
                )
                continue

            # Failed score-or-recall but passed diversity → DREAMS.md
            # (lower-confidence holding pen).  Recall-only failures (low
            # recall, ok score) also land here — they may earn promotion
            # in a future pass once recall accumulates.
            outcome = DreamOutcome.HELD
            why = []
            if not score_ok:
                why.append(f"score={score:.2f}<{self.config.score_threshold}")
            if not recall_ok:
                why.append(
                    f"recall={recall_count}<{self.config.min_recall_count}"
                )
            rationale = "held: " + ", ".join(why)
            try:
                self.hold_fn(cand.raw_text, self.config.dreams_md_max_bytes)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dreaming v2 hold_fn raised %s: %s; downgrading to DROPPED",
                    type(exc).__name__,
                    exc,
                )
                dropped.append(
                    DreamGateResult(
                        candidate=cand,
                        outcome=DreamOutcome.DROPPED,
                        score=score,
                        recall_count=recall_count,
                        diversity_score=diversity,
                        rationale=(
                            f"hold failed ({type(exc).__name__}); dropping"
                        ),
                    )
                )
                continue
            held.append(
                DreamGateResult(
                    candidate=cand,
                    outcome=outcome,
                    score=score,
                    recall_count=recall_count,
                    diversity_score=diversity,
                    rationale=rationale,
                )
            )

        return DreamRunSummary(
            promoted=tuple(promoted),
            held=tuple(held),
            dropped=tuple(dropped),
            skipped_already_processed=skipped,
            total_evaluated=len(candidates) - skipped,
            catch_up_run=catch_up,
        )

    def _is_catch_up_due(self) -> bool:
        """Carry-forward audit note (M6.1 brainstorm): if the last
        successful run is older than ``cron_miss_factor * cron_interval``,
        the next run is a catch-up pass.  We just record the flag here;
        the catch-up effect (larger fetch limit) is the caller's
        responsibility — they pass more candidates in.
        """
        if self.last_successful_run_ts_ns is None:
            return False
        now_ns = _dt.datetime.now(tz=_dt.UTC).timestamp() * 1e9
        elapsed_s = (now_ns - self.last_successful_run_ts_ns) / 1e9
        return elapsed_s > self.config.cron_miss_factor * self.cron_interval_seconds


__all__ = [
    "DreamCandidate",
    "DreamGateResult",
    "DreamOutcome",
    "DreamRunSummary",
    "DreamingPipeline",
    "DreamingV2Config",
]
