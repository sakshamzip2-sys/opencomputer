"""Production wiring for Dreaming v2 (v1.1 plan-3 M6.4 — cron + CLI).

This module sits between the pure-orchestration
:class:`opencomputer.agent.dreaming_v2.DreamingPipeline` (engine) and
the production substrate (SessionDB, MemoryManager, active provider,
profile filesystem).  Two entry points:

* :func:`run_dreaming_v2_tick` — invoked from
  :func:`opencomputer.cron.system_jobs.run_system_tick` once per cron
  tick.  Returns a serialisable summary dict.
* :func:`run_dreaming_v2_now` — coroutine the ``oc memory dream-v2``
  CLI command awaits to drive the same pipeline ad-hoc.

Both call into :func:`run_dreaming_v2_async` which builds the five
injectable callables (``score_fn``, ``recall_count_fn``, ``embed_fn``,
``promote_fn``, ``hold_fn``) from real dependencies.  Tests can swap
those dependencies via :func:`build_pipeline_with_dependencies` without
spinning up an LLM.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from opencomputer.agent.config import _home, default_config
from opencomputer.agent.config_store import load_config
from opencomputer.agent.dreaming_v2 import (
    DreamCandidate,
    DreamingPipeline,
    DreamingV2Config,
    DreamRunSummary,
)
from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message
from plugin_sdk.embeddings import EmbeddingBatch

logger = logging.getLogger("opencomputer.cron.dreaming_v2_tick")


# ─── persistent state (processed event-id ledger + last run ts) ────


def _state_path() -> Path:
    """``<profile_home>/cron/dreaming_v2_state.json``."""
    p = _home() / "cron"
    p.mkdir(parents=True, exist_ok=True)
    return p / "dreaming_v2_state.json"


def _load_state(path: Path | None = None) -> dict[str, Any]:
    p = path or _state_path()
    if not p.exists():
        return {"processed_event_ids": [], "last_run_ts_ns": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning(
            "dreaming_v2_state.json unreadable; starting from empty state"
        )
        return {"processed_event_ids": [], "last_run_ts_ns": None}


def _save_state(state: dict[str, Any], path: Path | None = None) -> None:
    p = path or _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ─── run-summary serialization (M3 audit fallback) ─────────────────
#
# The doc compared OC vs Hermes-SE and observed 222 events processed yet
# MEMORY.md was empty. Why? No persisted audit of gate decisions. We add
# a *counts-only* summary to state.json — small, additive, no schema
# migration. Per-candidate text never persists; only categorical
# rationales are parsed for fail-class counts.
#
# Why parse rationales rather than introspect outcomes? Because two
# different fail classes both produce HELD: score-too-low OR
# recall-too-low. The rationale string is the only carrier of WHICH
# gate(s) were decisive. The engine writes those strings
# deterministically (``DreamingPipeline.run_once`` emits literal
# ``score=…<…`` and ``recall=…<…`` substrings), so a regex over them is
# safe.
#
# Disjoint vs aggregate counters: a HELD candidate where BOTH gates
# failed has both substrings in its rationale. We split counts into
# disjoint buckets (``score_only`` / ``recall_only`` / ``both_gates``)
# so ``held == score_only + recall_only + both_gates`` is an invariant
# the dashboard / user can rely on without scratching their head over
# overlaps.

_SCORE_FAIL_RE = re.compile(r"score=[\d.]+<")
_RECALL_FAIL_RE = re.compile(r"recall=\d+<")


def summarize_run_for_state(
    summary: DreamRunSummary, *, run_ts_ns: int
) -> dict[str, Any]:
    """Map a ``DreamRunSummary`` to a JSON-safe counts dict.

    Persisted as ``state["last_summary"]`` so ``oc evolution dashboard``
    can show *why* nothing promoted without re-running the pipeline.
    Privacy: never includes ``raw_text`` or ``rationale`` per-record —
    only aggregate counts.

    HELD-bucket counts are disjoint::

        held == score_only + recall_only + both_gates + unattributed

    so the dashboard does not need to explain overlap to the operator.
    ``unattributed`` is always 0 under normal engine behavior; any
    non-zero value is logged at WARNING by this function and indicates
    the engine's rationale-string format has drifted.
    """
    score_only = 0
    recall_only = 0
    both_gates = 0
    unattributed = 0
    for r in summary.held:
        sf = bool(_SCORE_FAIL_RE.search(r.rationale))
        rf = bool(_RECALL_FAIL_RE.search(r.rationale))
        if sf and rf:
            both_gates += 1
        elif sf:
            score_only += 1
        elif rf:
            recall_only += 1
        else:
            # HELD with neither marker indicates the engine's rationale
            # format has drifted from what this regex expects. Loud-fail
            # per principal-engineer rule — count + warn so the operator
            # sees the breakdown invariant violation rather than the
            # mismatch hiding behind a silently-correct ``held`` total.
            unattributed += 1
    if unattributed:
        logger.warning(
            "dreaming_v2: %d held rationale(s) did not match either gate-fail "
            "regex; disjoint breakdown will under-count by this number. "
            "Engine rationale format may have changed — check "
            "DreamingPipeline.run_once.",
            unattributed,
        )

    return {
        "promoted": len(summary.promoted),
        "held": len(summary.held),
        "dropped": len(summary.dropped),
        "score_only": score_only,
        "recall_only": recall_only,
        "both_gates": both_gates,
        "unattributed": unattributed,
        "diversity_fail": len(summary.dropped),
        "evaluated": int(summary.total_evaluated),
        "catch_up_run": bool(summary.catch_up_run),
        "run_ts_ns": int(run_ts_ns),
    }


# ─── candidate fetcher ────────────────────────────────────────────


def _fetch_candidates(
    db: SessionDB, *, limit: int
) -> list[DreamCandidate]:
    """Pull recent un-dreamed episodic events as DreamCandidates.

    Uses ``dreamed_into IS NULL`` so previously-dreamed rows don't
    re-enter; the engine's idempotent ``processed_event_ids`` guard is
    a second defence in depth.
    """
    rows: list[Any] = []
    with db._connect() as conn:
        cur = conn.execute(
            """
            SELECT id, summary, timestamp
            FROM episodic_events
            WHERE dreamed_into IS NULL
              AND summary IS NOT NULL
              AND length(summary) > 0
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        rows = cur.fetchall()

    candidates: list[DreamCandidate] = []
    for r in rows:
        eid = str(r["id"])
        text = str(r["summary"])
        ts_ns = int(float(r["timestamp"]) * 1e9)
        candidates.append(
            DreamCandidate(
                event_id=eid,
                raw_text=text,
                timestamp_ns=ts_ns,
                metadata={"sqlite_row_id": r["id"]},
            )
        )
    return candidates


# ─── recall-count from SessionDB ──────────────────────────────────


def _build_recall_count_fn(db: SessionDB) -> Callable[[str], int]:
    """COUNT recall_citations rows per episodic_event_id.

    Returns a synchronous callable per the engine's contract.  Each
    invocation opens a short-lived connection so the function is
    thread-safe.
    """

    def recall_count_fn(event_id: str) -> int:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM recall_citations "
                "WHERE episodic_event_id = ?",
                (str(event_id),),
            ).fetchone()
        return int(row["n"]) if row else 0

    return recall_count_fn


# ─── score-fn via active provider ─────────────────────────────────


_SCORE_PROMPT = (
    "You are a memory-importance scorer.  You receive a single fact "
    "and return ONLY a number between 0.0 and 1.0 expressing whether "
    "this is a stable, durable fact worth promoting to long-term "
    "memory.\n"
    "\n"
    "Return 0.0 for ephemeral / one-off / debug-trace content.\n"
    "Return 1.0 for stable preferences, identity facts, recurring "
    "decisions, key contacts, or domain knowledge worth recalling.\n"
    "\n"
    "Respond with ONLY the number, no prose, no JSON, no explanation."
)

_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _build_score_fn_from_provider(
    provider: Any,
    *,
    model: str,
) -> Callable[[str], Awaitable[float]]:
    """Wrap ``BaseProvider.complete`` for use as the score gate.

    Failures bubble up to the engine's per-candidate try/except so a
    single LLM hiccup doesn't fail the whole run.
    """

    async def score_fn(text: str) -> float:
        resp = await provider.complete(
            model=model,
            messages=[Message(role="user", content=text.strip())],
            system=_SCORE_PROMPT,
            max_tokens=8,
            temperature=0.0,
            site="dreaming_v2_score",
        )
        out = ""
        # ProviderResponse.text canonical shape; fall back to repr for
        # exotic providers that store text elsewhere.
        if hasattr(resp, "text") and isinstance(resp.text, str):
            out = resp.text
        elif hasattr(resp, "content") and isinstance(resp.content, str):
            out = resp.content
        m = _FLOAT_RE.search(out)
        if not m:
            logger.warning(
                "dreaming_v2 score_fn: no numeric in response %r; treating "
                "as 0.0",
                out[:80],
            )
            return 0.0
        try:
            return float(m.group(0))
        except ValueError:
            return 0.0

    return score_fn


# ─── embed-fn via active provider ─────────────────────────────────


def _build_embed_fn_from_provider(
    provider: Any,
) -> Callable[[list[str]], Awaitable[EmbeddingBatch]] | None:
    """Adapter to the M6.6 embed contract.

    Returns ``None`` when the provider doesn't implement embeddings
    (raises :class:`EmbeddingsUnsupportedError`); the engine then
    treats diversity as 0 (favour over-promotion over silent drops).
    """

    async def embed_fn(texts: list[str]) -> EmbeddingBatch:
        return await provider.embed(texts=texts)

    return embed_fn


# ─── promote-fn (MEMORY.md) + hold-fn (DREAMS.md) ────────────────


def _build_promote_fn(memory: MemoryManager) -> Callable[[str], None]:
    def promote_fn(text: str) -> None:
        # Always promote with a date-prefixed bullet so the
        # consolidation timeline is visible in the file itself.
        today = _dt.date.today().isoformat()
        block = f"\n- {today} (dreamed): {text.strip()}\n"
        memory.append_declarative(block)

    return promote_fn


def _build_hold_fn(profile_home: Path) -> Callable[[str, int], None]:
    """Append to ``<profile_home>/DREAMS.md`` with a hard byte cap.

    Each entry is one paragraph beginning with ``- YYYY-MM-DD:``.
    Oldest entries evict from the FRONT (FIFO) when the cap is
    breached.  Atomic write via temp-then-rename so a crash mid-write
    leaves the previous file intact.
    """
    dreams_path = profile_home / "DREAMS.md"

    def hold_fn(text: str, max_bytes: int) -> None:
        today = _dt.date.today().isoformat()
        new_entry = f"- {today}: {text.strip()}"

        existing_entries: list[str] = []
        if dreams_path.exists():
            for raw in dreams_path.read_text(encoding="utf-8").split("\n\n"):
                line = raw.strip()
                if line.startswith("- "):
                    existing_entries.append(line)

        entries = [*existing_entries, new_entry]

        # FIFO eviction: drop oldest until we fit (or only the new
        # entry remains — even if it alone exceeds the cap, we keep it
        # so the caller observes the write).
        while len(entries) > 1:
            payload = "\n\n".join(entries) + "\n"
            if max_bytes <= 0 or len(payload.encode("utf-8")) <= max_bytes:
                break
            entries = entries[1:]

        payload = "\n\n".join(entries) + "\n"
        tmp = dreams_path.with_suffix(".md.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, dreams_path)

    return hold_fn


# ─── existing-memories block ──────────────────────────────────────


def _read_existing_memories(memory: MemoryManager) -> list[str]:
    """Split MEMORY.md into paragraph entries for the diversity gate.

    Uses the same heuristic as :class:`opencomputer.agent.memory_index
    .BM25Index`: paragraphs separated by 1+ blank lines OR a heading
    boundary.
    """
    text = memory.read_declarative()
    if not text:
        return []
    parts = re.split(r"\n\s*\n+", text.strip())
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out


# ─── pipeline assembly ────────────────────────────────────────────


@dataclass
class DreamingV2Dependencies:
    """Bundle of injectable substrates used by
    :func:`build_pipeline_with_dependencies`.

    Tests pass fakes; production callers call
    :func:`build_production_dependencies` to wire real ones.
    """

    profile_home: Path
    memory: MemoryManager
    db: SessionDB
    provider: Any | None
    model: str
    config: DreamingV2Config


def build_production_dependencies(
    *,
    profile_home: Path | None = None,
) -> DreamingV2Dependencies:
    """Wire real substrates from the active profile + config."""
    cfg = load_config()
    home = profile_home or _home()

    memory = MemoryManager(
        declarative_path=cfg.memory.declarative_path,
        skills_path=cfg.memory.skills_path,
        memory_char_limit=cfg.memory.memory_char_limit,
    )
    db = SessionDB(cfg.session.db_path)

    # Provider is best-effort: when the operator hasn't configured one
    # the score gate falls through (engine treats raises as 0.0 score).
    provider: Any | None = None
    try:
        from opencomputer.cli import _resolve_provider  # local import — avoids cycle

        provider = _resolve_provider(
            cfg.model.provider, api_mode=getattr(cfg.model, "api_mode", None)
        )
    except Exception as exc:  # noqa: BLE001 — telemetry guard
        logger.warning(
            "dreaming_v2: could not resolve provider %r (%s); score "
            "gate will treat all scores as 0.0",
            cfg.model.provider,
            exc,
        )
        provider = None

    # 2026-05-11 — apply EvolutionOrchestrator's persisted tuning when
    # present. The orchestrator subscribes to ``skill_review_decision``
    # events and adjusts ``dreaming_v2_score_threshold`` +
    # ``dreaming_v2_min_recall`` based on the rolling accept-rate window.
    # Falls back to base Config defaults on any read failure so dreaming
    # works without an orchestrator running.
    score_threshold = cfg.memory.dreaming_v2_score_threshold
    min_recall_count = cfg.memory.dreaming_v2_min_recall_count
    try:
        from opencomputer.agent.evolution_orchestrator import (
            DEFAULT_TUNING,
            load_tuning,
        )

        tuning = load_tuning(home)
        # Only apply tuning values that have actually been moved off
        # their defaults — preserves operator-overrides in config.yaml
        # for the unchanged values. The orchestrator and base Config
        # share the same defaults (0.65 / 2), so a no-op tune leaves
        # both fields untouched.
        if (
            tuning.dreaming_v2_score_threshold
            != DEFAULT_TUNING.dreaming_v2_score_threshold
        ):
            score_threshold = tuning.dreaming_v2_score_threshold
        if (
            tuning.dreaming_v2_min_recall
            != DEFAULT_TUNING.dreaming_v2_min_recall
        ):
            min_recall_count = tuning.dreaming_v2_min_recall
        logger.debug(
            "dreaming_v2: applied tuning (score=%.2f, recall=%d) — "
            "config defaults were (%.2f, %d)",
            score_threshold,
            min_recall_count,
            cfg.memory.dreaming_v2_score_threshold,
            cfg.memory.dreaming_v2_min_recall_count,
        )
    except Exception:  # noqa: BLE001 — tuning read is best-effort
        logger.debug(
            "dreaming_v2: tuning read failed; using config defaults",
            exc_info=True,
        )

    pipeline_cfg = DreamingV2Config(
        enabled=cfg.memory.dreaming_v2_enabled,
        score_threshold=score_threshold,
        min_recall_count=min_recall_count,
        diversity_threshold=cfg.memory.dreaming_v2_diversity_threshold,
        max_promotions_per_run=cfg.memory.dreaming_v2_max_promotions_per_run,
        dreams_md_max_bytes=cfg.memory.dreaming_v2_dreams_md_max_bytes,
    )

    return DreamingV2Dependencies(
        profile_home=home,
        memory=memory,
        db=db,
        provider=provider,
        model=cfg.model.model,
        config=pipeline_cfg,
    )


def build_pipeline_with_dependencies(
    deps: DreamingV2Dependencies,
    *,
    last_successful_run_ts_ns: int | None = None,
    cron_interval_seconds: float = 24 * 60 * 60,
) -> DreamingPipeline:
    """Assemble a :class:`DreamingPipeline` from
    :class:`DreamingV2Dependencies`.

    The five injectable callables are bound to:

    - ``score_fn``: provider.complete() with a dedicated judge prompt
      (or zero-score fallback if no provider).
    - ``recall_count_fn``: SessionDB query against recall_citations.
    - ``embed_fn``: provider.embed() (or None when unsupported).
    - ``promote_fn``: MemoryManager.append_declarative(...).
    - ``hold_fn``: append to ``<profile_home>/DREAMS.md`` with cap.
    """
    if deps.provider is not None:
        score_fn: Callable[[str], Awaitable[float]] = (
            _build_score_fn_from_provider(deps.provider, model=deps.model)
        )
        embed_fn: Callable[[list[str]], Awaitable[EmbeddingBatch]] | None = (
            _build_embed_fn_from_provider(deps.provider)
        )
    else:
        async def _zero_score(_text: str) -> float:
            return 0.0

        score_fn = _zero_score
        embed_fn = None

    return DreamingPipeline(
        config=deps.config,
        score_fn=score_fn,
        recall_count_fn=_build_recall_count_fn(deps.db),
        embed_fn=embed_fn,
        promote_fn=_build_promote_fn(deps.memory),
        hold_fn=_build_hold_fn(deps.profile_home),
        last_successful_run_ts_ns=last_successful_run_ts_ns,
        cron_interval_seconds=cron_interval_seconds,
    )


# ─── tick entry points ────────────────────────────────────────────


async def run_dreaming_v2_async(
    *,
    deps: DreamingV2Dependencies,
    candidate_limit: int = 50,
    state_path: Path | None = None,
) -> DreamRunSummary:
    """Run one Dreaming v2 pass.  Async because of provider IO."""
    if not deps.config.enabled:
        logger.info("dreaming_v2: disabled by config; skipping")
        return DreamRunSummary()

    state = _load_state(state_path)
    last_run_ts_ns = state.get("last_run_ts_ns")
    pipeline = build_pipeline_with_dependencies(
        deps,
        last_successful_run_ts_ns=(
            int(last_run_ts_ns) if last_run_ts_ns else None
        ),
    )

    candidates = _fetch_candidates(deps.db, limit=candidate_limit)
    existing_memories = _read_existing_memories(deps.memory)
    already_processed = set(state.get("processed_event_ids", []))

    summary = await pipeline.run_once(
        candidates=candidates,
        existing_memories=existing_memories,
        already_processed_event_ids=already_processed,
    )

    # Mark all evaluated candidates as processed (regardless of outcome,
    # so a HELD or DROPPED candidate doesn't loop on every tick).
    for result_group in (summary.promoted, summary.held, summary.dropped):
        for r in result_group:
            already_processed.add(r.candidate.event_id)

    # Mark promoted candidates as dreamed_into in episodic_events so the
    # cluster-level dreaming.py knows they're already consolidated.
    if summary.promoted:
        with deps.db._connect() as conn:
            for r in summary.promoted:
                row_id = r.candidate.metadata.get("sqlite_row_id")
                if row_id is not None:
                    conn.execute(
                        "UPDATE episodic_events SET dreamed_into = ? "
                        "WHERE id = ? AND dreamed_into IS NULL",
                        (row_id, row_id),
                    )

    state["processed_event_ids"] = sorted(already_processed)
    now_ns = int(_dt.datetime.now(tz=_dt.UTC).timestamp() * 1e9)
    state["last_run_ts_ns"] = now_ns
    # Audit fallback (M3): persist counts-only summary so the dashboard
    # can show why the last run did/didn't promote. Wrapped in try so
    # any future shape change to DreamRunSummary degrades cleanly —
    # losing the audit display is strictly better than losing the run.
    try:
        state["last_summary"] = summarize_run_for_state(
            summary, run_ts_ns=now_ns
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dreaming_v2: failed to serialize last_summary: %s; "
            "state still saved without it",
            exc,
        )
    _save_state(state, state_path)

    return summary


def run_dreaming_v2_tick() -> dict[str, Any]:
    """Synchronous cron-tick wrapper.  Called from ``run_system_tick``.

    Returns a flat summary dict suitable for the system_jobs telemetry.
    """
    try:
        deps = build_production_dependencies()
    except Exception as exc:  # noqa: BLE001
        logger.warning("dreaming_v2 tick: dep build failed: %s", exc)
        return {"error": f"deps: {exc}"}

    if not deps.config.enabled:
        return {"status": "disabled"}

    cfg = default_config()
    candidate_limit = getattr(
        cfg.memory, "dreaming_v2_candidate_fetch_limit", 50
    )

    def _run_in_fresh_loop() -> DreamRunSummary:
        # The coroutine is constructed *inside* asyncio.run's call so it
        # cannot be orphaned if asyncio.run rejects (it never will here:
        # this helper is invoked on a thread with no running loop).
        return asyncio.run(
            run_dreaming_v2_async(
                deps=deps, candidate_limit=int(candidate_limit)
            )
        )

    # Co-tenant cron path: ``oc gateway`` runs the cron scheduler as an
    # asyncio task on its main loop, so this function runs inside a
    # running event loop. ``asyncio.run`` then raises before iterating
    # the coroutine, orphaning it (RuntimeWarning: coroutine never
    # awaited) and silently no-op'ing the dream pass. Dispatch to a
    # worker thread that owns its own loop. .result() blocks the cron
    # tick coroutine until the dream pass completes — acceptable since
    # the other system_jobs callbacks are already synchronous.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        summary = _run_in_fresh_loop()
    else:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="dreaming_v2_tick"
        ) as pool:
            summary = pool.submit(_run_in_fresh_loop).result()

    return _summarise_for_cron(summary)


def _summarise_for_cron(summary: DreamRunSummary) -> dict[str, Any]:
    return {
        "promoted": len(summary.promoted),
        "held": len(summary.held),
        "dropped": len(summary.dropped),
        "skipped_already_processed": summary.skipped_already_processed,
        "total_evaluated": summary.total_evaluated,
        "catch_up_run": summary.catch_up_run,
    }


# Kept reachable for downstream consumers that want full payloads.
_ = asdict


__all__ = [
    "DreamingV2Dependencies",
    "build_pipeline_with_dependencies",
    "build_production_dependencies",
    "run_dreaming_v2_async",
    "run_dreaming_v2_tick",
]
