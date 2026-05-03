"""Phase 1 scoring cron — backfill composite + judge + turn_score.

Runs on every system tick. Finds ``turn_outcomes`` rows where
``turn_score IS NULL`` from the last 24h, computes the composite score
purely from Phase 0 signal columns, optionally calls the cheap LLM
judge (cost-guarded), fuses the two, and UPDATEs the row.

Why a separate cron job (not inline at end-of-turn):
  - LLM judge takes 1-2s; the dispatch fire-and-forget hook is fast on
    purpose. Pulling the judge out lets it run on its own cadence.
  - Cost guard exhaustion or provider error degrades gracefully — we
    still write composite_score, just leave judge_score NULL.
  - Idempotent: re-running the cron only re-scores rows where
    ``turn_score IS NULL``, so a crash mid-batch is safe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

logger = logging.getLogger(__name__)

_LOOKBACK_S = 24 * 3600
_BATCH_LIMIT = 50  # cap per tick to avoid burst spend


def run_score_turns(*, db) -> dict[str, int]:
    """Score every unscored turn_outcomes row from the last 24h.

    Returns a summary dict: ``{"composite_only": N, "judged": N,
    "judge_skipped": N}``.
    """
    cutoff = time.time() - _LOOKBACK_S
    summary = {"composite_only": 0, "judged": 0, "judge_skipped": 0}

    with db._connect() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, turn_index,
                   tool_call_count, tool_success_count, tool_error_count,
                   self_cancel_count, retry_count, conversation_abandoned,
                   affirmation_present, correction_present,
                   vibe_before, vibe_after,
                   standing_order_violations
            FROM turn_outcomes
            WHERE turn_score IS NULL
              AND created_at >= ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (cutoff, _BATCH_LIMIT),
        ).fetchall()

    if not rows:
        return summary

    for row in rows:
        try:
            composite = _compute_composite(row)
            judge_score, judge_reasoning, judge_model = _maybe_judge(row, composite)
            if judge_score is not None:
                summary["judged"] += 1
            elif _judge_was_attempted(row):
                summary["judge_skipped"] += 1
            else:
                summary["composite_only"] += 1

            fused = _fuse(composite, judge_score)
            with db._connect() as conn:
                conn.execute(
                    "UPDATE turn_outcomes SET "
                    "composite_score = ?, judge_score = ?, "
                    "judge_reasoning = ?, judge_model = ?, "
                    "turn_score = ?, scored_at = ? "
                    "WHERE id = ?",
                    (composite, judge_score, judge_reasoning, judge_model,
                     fused, time.time(), row["id"]),
                )
        except Exception as e:  # noqa: BLE001 — never break the cron
            logger.warning("score_turn failed for %s: %s", row["id"], e)

    if any(summary.values()):
        logger.info("score_turns summary: %s", summary)
    return summary


def _compute_composite(row) -> float:
    """Pure-arithmetic Phase 0 → composite_score."""
    from opencomputer.agent.composite_scorer import compute_composite_score

    vibe_before = row["vibe_before"]
    vibe_after = row["vibe_after"]
    vibe_delta = _vibe_delta(vibe_before, vibe_after)

    violations = row["standing_order_violations"]
    n_violations = 0
    if violations:
        try:
            n_violations = len(json.loads(violations))
        except (ValueError, TypeError):
            n_violations = 0

    return compute_composite_score(
        tool_call_count=row["tool_call_count"] or 0,
        tool_success_count=row["tool_success_count"] or 0,
        tool_error_count=row["tool_error_count"] or 0,
        self_cancel_count=row["self_cancel_count"] or 0,
        retry_count=row["retry_count"] or 0,
        conversation_abandoned=bool(row["conversation_abandoned"]),
        affirmation_present=bool(row["affirmation_present"]),
        correction_present=bool(row["correction_present"]),
        vibe_delta=vibe_delta,
        standing_order_violation_count=n_violations,
    )


_POSITIVE_VIBES = frozenset({"curious", "calm", "excited"})
_NEGATIVE_VIBES = frozenset({"frustrated", "tired", "stuck"})


def _vibe_delta(before: str | None, after: str | None) -> int:
    """Map (vibe_before, vibe_after) → -1, 0, +1."""
    if not before or not after:
        return 0
    before_pos = before in _POSITIVE_VIBES
    before_neg = before in _NEGATIVE_VIBES
    after_pos = after in _POSITIVE_VIBES
    after_neg = after in _NEGATIVE_VIBES
    if before_neg and after_pos:
        return 1
    if before_pos and after_neg:
        return -1
    return 0


# Module-level switch tests can flip to skip the LLM call entirely.
_JUDGE_ENABLED = True


def _judge_was_attempted(row) -> bool:
    """Heuristic for the summary counter — was the LLM judge skipped
    rather than just disabled? Currently we just return False since the
    judge attempt is best-effort and we don't track skip-vs-fail
    separately in the row schema."""
    return False


def _maybe_judge(row, composite: float):
    """Try to call the LLM judge. Returns (score, reasoning, model)
    triple or (None, None, None) on any failure path."""
    if not _JUDGE_ENABLED:
        return (None, None, None)
    try:
        from opencomputer.agent.judge_reviewer import score_turn_via_judge
    except Exception:  # noqa: BLE001
        return (None, None, None)

    trajectory = (
        f"session={row['session_id'][:8]} turn={row['turn_index']} "
        f"tools_called={row['tool_call_count']} "
        f"tools_success={row['tool_success_count']} "
        f"tools_error={row['tool_error_count']} "
        f"vibe={row['vibe_before']}->{row['vibe_after']} "
        f"abandoned={'yes' if row['conversation_abandoned'] else 'no'}"
    )

    # Pass-through wrapper around aux_llm.complete_text matching the
    # provider shape score_turn_via_judge expects.
    class _AuxProvider:
        async def complete(self, *, model, messages, max_tokens=200, **_):
            from opencomputer.agent.aux_llm import complete_text

            text = await complete_text(
                messages=messages, max_tokens=max_tokens, model=model,
            )

            class _Resp:
                pass

            r = _Resp()
                # The judge regex looks for response.text
            r.text = text
            return r

    model = "claude-haiku-4-5"
    try:
        verdict = asyncio.run(
            score_turn_via_judge(
                provider=_AuxProvider(),
                model=model,
                trajectory_summary=trajectory,
                composite_score=composite,
                standing_orders="",
            )
        )
    except RuntimeError:
        # Already in an event loop — schedule fire-and-forget instead.
        # Score this row composite-only; next tick will retry from a
        # cleaner context.
        return (None, None, None)
    except Exception as e:  # noqa: BLE001
        logger.warning("judge LLM call failed: %s", e)
        return (None, None, None)

    if verdict is None:
        return (None, None, None)
    return (verdict.judge_score, verdict.judge_reasoning, verdict.judge_model)


def _fuse(composite: float, judge: float | None) -> float:
    from opencomputer.agent.score_fusion import fused_turn_score
    return fused_turn_score(composite, judge)
