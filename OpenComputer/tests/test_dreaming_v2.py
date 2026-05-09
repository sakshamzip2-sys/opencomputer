"""Dreaming v2 three-gate consolidation tests (v1.1 plan-3 M6.4)."""

from __future__ import annotations

import datetime as _dt

import pytest

from opencomputer.agent.dreaming_v2 import (
    DreamCandidate,
    DreamingPipeline,
    DreamingV2Config,
    DreamOutcome,
    _cosine,
    _hash_event_for_dedup,
)
from plugin_sdk.embeddings import EmbeddingBatch, EmbeddingsUnsupportedError

# ─── pure helpers ────────────────────────────────────────────────────


def test_hash_event_id_is_stable_across_calls() -> None:
    h1 = _hash_event_for_dedup("user prefers postgres")
    h2 = _hash_event_for_dedup("user prefers postgres")
    assert h1 == h2


def test_hash_event_id_distinct_for_different_text() -> None:
    h1 = _hash_event_for_dedup("user prefers postgres")
    h2 = _hash_event_for_dedup("user prefers mysql")
    assert h1 != h2


def test_cosine_identical_vectors_returns_1() -> None:
    assert abs(_cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) - 1.0) < 1e-9


def test_cosine_orthogonal_vectors_returns_0() -> None:
    assert abs(_cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9


def test_cosine_zero_vector_returns_0() -> None:
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_mismatched_lengths_returns_0() -> None:
    assert _cosine([1.0, 0.0], [1.0]) == 0.0


# ─── pipeline helpers (test scaffolding) ────────────────────────────


def _make_candidate(text: str, *, ts: int = 0) -> DreamCandidate:
    return DreamCandidate(
        event_id=_hash_event_for_dedup(text),
        raw_text=text,
        timestamp_ns=ts,
    )


def _capture_promotes_and_holds() -> tuple[list[str], list[tuple[str, int]]]:
    promoted: list[str] = []
    held: list[tuple[str, int]] = []

    def promote(text: str) -> None:
        promoted.append(text)

    def hold(text: str, cap: int) -> None:
        held.append((text, cap))

    return promoted, held, promote, hold  # type: ignore[return-value]


def _stub_embed_fn(dim: int = 4):
    async def fn(texts: list[str]) -> EmbeddingBatch:
        vectors: list[list[float]] = []
        for t in texts:
            v = [0.0] * dim
            for word in t.lower().split():
                v[abs(hash(word)) % dim] += 1.0
            if all(x == 0.0 for x in v):
                v[0] = 1.0
            vectors.append(v)
        return EmbeddingBatch(vectors=vectors, dimensionality=dim, model_id="stub")

    return fn


# ─── routing — three gates ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_gates_pass_promotes_to_memory_md() -> None:
    promoted: list[str] = []
    held: list[tuple[str, int]] = []

    async def score(text: str) -> float:
        return 0.9

    def recall_count(eid: str) -> int:
        return 5

    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
    )
    cands = [_make_candidate("user lives in nyc")]
    summary = await pipeline.run_once(cands, existing_memories=[])
    assert len(summary.promoted) == 1
    assert promoted == ["user lives in nyc"]
    assert summary.promoted[0].outcome == DreamOutcome.PROMOTED


@pytest.mark.asyncio
async def test_low_score_routes_to_dreams_md() -> None:
    held: list[tuple[str, int]] = []
    promoted: list[str] = []

    async def score(text: str) -> float:
        return 0.3  # below 0.65 threshold

    def recall_count(eid: str) -> int:
        return 5  # high recall

    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
    )
    cands = [_make_candidate("low signal observation")]
    summary = await pipeline.run_once(cands, existing_memories=[])
    assert summary.held and summary.held[0].outcome == DreamOutcome.HELD
    assert len(held) == 1
    assert "score=0.30" in summary.held[0].rationale


@pytest.mark.asyncio
async def test_low_recall_routes_to_dreams_md() -> None:
    held: list[tuple[str, int]] = []
    promoted: list[str] = []

    async def score(text: str) -> float:
        return 0.9

    def recall_count(eid: str) -> int:
        return 0  # below min_recall_count=2

    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
    )
    cands = [_make_candidate("only seen once observation")]
    summary = await pipeline.run_once(cands, existing_memories=[])
    assert summary.held
    assert "recall=0<2" in summary.held[0].rationale


@pytest.mark.asyncio
async def test_high_diversity_drops_candidate() -> None:
    """Cosine to existing memory > threshold → DROPPED, no DREAMS.md write."""
    held: list[tuple[str, int]] = []
    promoted: list[str] = []

    async def score(text: str) -> float:
        return 0.9

    def recall_count(eid: str) -> int:
        return 5

    pipeline = DreamingPipeline(
        config=DreamingV2Config(
            enabled=True, diversity_threshold=0.5
        ),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
    )
    # Same text → cosine = 1.0 > 0.5 → drop
    cands = [_make_candidate("user prefers postgres")]
    existing = ["user prefers postgres"]
    summary = await pipeline.run_once(cands, existing_memories=existing)
    assert summary.dropped
    assert summary.dropped[0].outcome == DreamOutcome.DROPPED
    assert "diversity gate failed" in summary.dropped[0].rationale
    assert promoted == []
    assert held == []


# ─── budget + idempotency ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_promotions_per_run_capped() -> None:
    promoted: list[str] = []
    held: list[tuple[str, int]] = []

    async def score(text: str) -> float:
        return 0.9

    def recall_count(eid: str) -> int:
        return 5

    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True, max_promotions_per_run=3),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
    )
    cands = [_make_candidate(f"observation {i}") for i in range(10)]
    summary = await pipeline.run_once(cands, existing_memories=[])
    assert len(summary.promoted) == 3
    assert len(promoted) == 3


@pytest.mark.asyncio
async def test_idempotency_skips_already_processed_event_ids() -> None:
    promoted: list[str] = []
    held: list[tuple[str, int]] = []

    async def score(text: str) -> float:
        return 0.9

    def recall_count(eid: str) -> int:
        return 5

    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
    )
    cands = [
        _make_candidate("alpha"),
        _make_candidate("beta"),
        _make_candidate("gamma"),
    ]
    # Pre-mark alpha as processed
    already = {cands[0].event_id}
    summary = await pipeline.run_once(cands, existing_memories=[], already_processed_event_ids=already)
    assert summary.skipped_already_processed == 1
    assert summary.total_evaluated == 2
    # Only beta and gamma should appear in promoted
    assert "alpha" not in promoted


@pytest.mark.asyncio
async def test_disabled_pipeline_does_nothing() -> None:
    promoted: list[str] = []
    held: list[tuple[str, int]] = []

    async def score(text: str) -> float:
        return 0.9

    def recall_count(eid: str) -> int:
        return 5

    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=False),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
    )
    cands = [_make_candidate("alpha")]
    summary = await pipeline.run_once(cands, existing_memories=[])
    assert summary == type(summary)()  # default empty summary
    assert promoted == []


# ─── graceful degradation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_embed_fn_treats_diversity_as_zero() -> None:
    """Without embeddings, every candidate passes the diversity gate."""
    promoted: list[str] = []
    held: list[tuple[str, int]] = []

    async def score(text: str) -> float:
        return 0.9

    def recall_count(eid: str) -> int:
        return 5

    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=None,  # no embed
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
    )
    # Even a duplicate should promote (no diversity check possible)
    cands = [_make_candidate("user prefers postgres")]
    summary = await pipeline.run_once(
        cands, existing_memories=["user prefers postgres"]
    )
    assert len(summary.promoted) == 1


@pytest.mark.asyncio
async def test_embed_unsupported_treats_diversity_as_zero() -> None:
    promoted: list[str] = []
    held: list[tuple[str, int]] = []

    async def score(text: str) -> float:
        return 0.9

    def recall_count(eid: str) -> int:
        return 5

    async def unsupported(_: list[str]) -> EmbeddingBatch:
        raise EmbeddingsUnsupportedError("provider has no embeddings")

    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=unsupported,
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
    )
    cands = [_make_candidate("user prefers postgres")]
    summary = await pipeline.run_once(
        cands, existing_memories=["existing memory"]
    )
    assert len(summary.promoted) == 1


@pytest.mark.asyncio
async def test_score_fn_exception_treated_as_zero() -> None:
    promoted: list[str] = []
    held: list[tuple[str, int]] = []

    async def boom(text: str) -> float:
        raise RuntimeError("aux LLM down")

    def recall_count(eid: str) -> int:
        return 5

    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True),
        score_fn=boom,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
    )
    cands = [_make_candidate("alpha")]
    summary = await pipeline.run_once(cands, existing_memories=[])
    # Score=0 + recall=5 + novel → fails score gate, but recall+diversity pass
    # → goes to DREAMS.md (HELD).
    assert summary.held
    assert summary.held[0].score == 0.0


@pytest.mark.asyncio
async def test_promote_fn_failure_downgrades_to_held() -> None:
    held: list[tuple[str, int]] = []

    async def score(text: str) -> float:
        return 0.9

    def recall_count(eid: str) -> int:
        return 5

    def failing_promote(text: str) -> None:
        raise OSError("disk full")

    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=failing_promote,
        hold_fn=lambda t, cap: held.append((t, cap)),
    )
    cands = [_make_candidate("alpha")]
    summary = await pipeline.run_once(cands, existing_memories=[])
    # Should NOT raise; downgrades to HELD
    assert len(summary.held) == 1
    assert "promote failed" in summary.held[0].rationale


# ─── cron-miss catch-up (carry-forward audit note) ─────────────────


@pytest.mark.asyncio
async def test_catch_up_flag_set_when_last_run_too_old() -> None:
    """Carry-forward audit fix from M6.1 brainstorm: if last_run >
    2x cron_interval, mark this run as catch-up."""
    promoted: list[str] = []
    held: list[tuple[str, int]] = []

    async def score(text: str) -> float:
        return 0.9

    def recall_count(eid: str) -> int:
        return 5

    # Last run 5 days ago, cron interval 1 day → 5x > 2x = catch-up
    five_days_ago_ns = int(
        (_dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(days=5)).timestamp() * 1e9
    )
    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
        last_successful_run_ts_ns=five_days_ago_ns,
        cron_interval_seconds=24 * 60 * 60,
    )
    summary = await pipeline.run_once(
        [_make_candidate("alpha")], existing_memories=[]
    )
    assert summary.catch_up_run is True


@pytest.mark.asyncio
async def test_catch_up_flag_clear_when_recent_run() -> None:
    """1 hour ago < 2 day threshold → not a catch-up."""
    promoted: list[str] = []
    held: list[tuple[str, int]] = []

    async def score(text: str) -> float:
        return 0.9

    def recall_count(eid: str) -> int:
        return 5

    one_hour_ago_ns = int(
        (_dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(hours=1)).timestamp() * 1e9
    )
    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
        last_successful_run_ts_ns=one_hour_ago_ns,
        cron_interval_seconds=24 * 60 * 60,
    )
    summary = await pipeline.run_once(
        [_make_candidate("alpha")], existing_memories=[]
    )
    assert summary.catch_up_run is False


@pytest.mark.asyncio
async def test_catch_up_flag_clear_when_first_run() -> None:
    """No prior run → not a catch-up (first run is just first run)."""
    promoted: list[str] = []
    held: list[tuple[str, int]] = []

    async def score(text: str) -> float:
        return 0.9

    def recall_count(eid: str) -> int:
        return 5

    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True),
        score_fn=score,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
        last_successful_run_ts_ns=None,
    )
    summary = await pipeline.run_once(
        [_make_candidate("alpha")], existing_memories=[]
    )
    assert summary.catch_up_run is False


# ─── score clamping ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_score_clamps_to_unit_interval() -> None:
    """A misbehaving score_fn returning >1 or <0 must be clamped."""
    promoted: list[str] = []
    held: list[tuple[str, int]] = []

    async def crazy_score(text: str) -> float:
        return 99.0  # totally out of range

    def recall_count(eid: str) -> int:
        return 5

    pipeline = DreamingPipeline(
        config=DreamingV2Config(enabled=True),
        score_fn=crazy_score,
        recall_count_fn=recall_count,
        embed_fn=_stub_embed_fn(),
        promote_fn=lambda t: promoted.append(t),
        hold_fn=lambda t, cap: held.append((t, cap)),
    )
    summary = await pipeline.run_once(
        [_make_candidate("alpha")], existing_memories=[]
    )
    # Clamped to 1.0; passes score gate
    assert summary.promoted
    assert summary.promoted[0].score == 1.0
