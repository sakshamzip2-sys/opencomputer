"""Phase 0 + Phase 2 v0 system cron jobs.

Distinct from ``cron/jobs.py`` (user-defined cron), these are SYSTEM
jobs that fire on a fixed cadence whenever the cron scheduler ticks.
The functions are individually safe to call repeatedly — each is
either idempotent on its own state or gated by data-availability
checks that produce a no-op when there's nothing to do.

The five jobs:

  - ``sweep_self_cancels``   — every tick (5 min); detects undo-pairs
                               in the last 30 min of tool_usage.
  - ``sweep_abandonments``   — every tick; marks the LAST turn of any
                               session inactive for 24h.
  - ``decay_sweep``          — every tick; transitions
                               ``active`` → ``expired_decayed`` once
                               effective penalty drops below 0.05.
  - ``auto_revert``          — every tick; statistical revert on
                               ``pending_evaluation`` rows once
                               eligible_n ≥ 10.
  - ``policy_engine_tick``   — every tick; the engine's daily-budget
                               check ensures only N decisions land per
                               24h regardless of tick frequency.

All five run inside a single ``run_system_tick`` call. Errors in any
one job are caught + logged; remaining jobs still execute.
"""
from __future__ import annotations

import logging

from opencomputer.agent.config import _home
from opencomputer.agent.config_store import default_config
from opencomputer.agent.feature_flags import FeatureFlags
from opencomputer.agent.policy_audit_key import get_policy_audit_hmac_key
from opencomputer.agent.state import SessionDB
from opencomputer.cron.auto_revert import run_auto_revert_due
from opencomputer.cron.decay_sweep import run_decay_sweep
from opencomputer.cron.dreaming_v2_tick import run_dreaming_v2_tick
from opencomputer.cron.policy_digest import run_policy_digest
from opencomputer.cron.policy_engine_tick import run_engine_tick
from opencomputer.cron.prune_turn_outcomes import run_prune_turn_outcomes
from opencomputer.cron.score_turns import run_score_turns
from opencomputer.cron.turn_outcomes_sweep import (
    sweep_abandonments,
    sweep_self_cancels,
)

logger = logging.getLogger("opencomputer.cron.system_jobs")


def run_system_tick() -> dict[str, str | int]:
    """Run every system job once. Returns a per-job summary dict.

    Each job is wrapped so a single failure can't cascade. Returns a
    dict like ``{"sweep_self_cancels": 3, "auto_revert": 1, ...}`` —
    counts where applicable, ``"error: <msg>"`` strings on failures,
    or ``"skipped: kill_switch_off"`` for engine_tick when the
    feature flag is off.
    """
    cfg = default_config()
    db = SessionDB(cfg.session.db_path)
    flags = FeatureFlags(_home() / "feature_flags.json")
    hmac_key = get_policy_audit_hmac_key(_home())

    summary: dict[str, str | int] = {}

    # P0 sweeps — backfill self-cancel + abandonment signals
    import time

    summary["sweep_self_cancels"] = _safe_call(
        "sweep_self_cancels",
        lambda: sweep_self_cancels(db, since_ts=time.time() - 1800),
    )
    summary["sweep_abandonments"] = _safe_call(
        "sweep_abandonments",
        lambda: sweep_abandonments(db, threshold_s=86400),
    )

    # P2 v0 — auto-revert + decay + engine
    summary["auto_revert"] = _safe_call(
        "auto_revert",
        lambda: run_auto_revert_due(db=db, flags=flags, hmac_key=hmac_key),
    )
    decay_result = _safe_call(
        "decay_sweep",
        lambda: run_decay_sweep(db=db, hmac_key=hmac_key),
    )
    if isinstance(decay_result, str):
        summary["decay_sweep"] = decay_result
    else:
        summary["decay_sweep"] = decay_result.expired_count

    summary["policy_engine_tick"] = _safe_call(
        "policy_engine_tick",
        lambda: run_engine_tick(db=db, flags=flags, hmac_key=hmac_key).value,
    )

    # Phase 1 — backfill composite + judge + turn_score on unscored rows
    score_result = _safe_call("score_turns", lambda: run_score_turns(db=db))
    if isinstance(score_result, dict):
        summary["score_turns_judged"] = score_result.get("judged", 0)
        summary["score_turns_composite_only"] = score_result.get(
            "composite_only", 0,
        )
    else:
        summary["score_turns"] = score_result

    # v0.5 — Task A: digest cron (no-op outside the configured hour or if
    # already fired today)
    summary["policy_digest"] = _safe_call(
        "policy_digest",
        lambda: run_policy_digest(db=db, flags=flags),
    )

    # v0.5 — Task D: data retention prune
    summary["prune_turn_outcomes"] = _safe_call(
        "prune_turn_outcomes",
        lambda: run_prune_turn_outcomes(db=db, flags=flags),
    )

    # v1.1 plan-3 M6.4 — Dreaming v2 (default OFF, opt-in via
    # ``cfg.memory.dreaming_v2_enabled = True``).
    dream_result = _safe_call("dreaming_v2", run_dreaming_v2_tick)
    if isinstance(dream_result, dict):
        summary["dreaming_v2_promoted"] = int(dream_result.get("promoted", 0))
        summary["dreaming_v2_held"] = int(dream_result.get("held", 0))
        summary["dreaming_v2_dropped"] = int(dream_result.get("dropped", 0))
    else:
        summary["dreaming_v2"] = dream_result

    # 2026-05-10 — F4 user-model motif import. The audit found 28 graph
    # nodes / 0 edges because ``MotifImporter.import_recent`` had no
    # production caller; it only ran via ``oc user-model import``.
    # Without this tick, the F4 graph stays edge-less even when the
    # behavioral inference engine produces motifs. Idempotent: the
    # importer dedups by motif id, so repeated ticks don't re-add nodes.
    motif_result = _safe_call("motif_import", _run_motif_import_tick)
    if isinstance(motif_result, dict):
        summary["motif_import_nodes"] = int(motif_result.get("nodes_added", 0))
        summary["motif_import_edges"] = int(motif_result.get("edges_added", 0))
    else:
        summary["motif_import"] = motif_result

    logger.info("system_tick summary: %s", summary)
    return summary


def _run_motif_import_tick() -> dict[str, int]:
    """Pull recent motifs from MotifStore into the user-model graph.

    Cheap when MotifStore is empty (the typical case during the first
    days after a clean install). ``inference/motifs.sqlite`` is created
    lazily by :class:`BehavioralInferenceEngine` — until that fires,
    this tick is a fast no-op.

    Returns ``{"nodes_added": N, "edges_added": M}``.
    """
    try:
        from opencomputer.user_model.importer import MotifImporter
    except Exception:  # noqa: BLE001 — degrade gracefully
        return {"nodes_added": 0, "edges_added": 0}
    try:
        importer = MotifImporter()
        nodes_added, edges_added = importer.import_recent(limit=100)
    except Exception as exc:  # noqa: BLE001
        logger.warning("motif_import_tick: import_recent failed: %s", exc)
        return {"nodes_added": 0, "edges_added": 0}
    return {"nodes_added": int(nodes_added), "edges_added": int(edges_added)}


def _safe_call(name: str, fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — telemetry guard
        logger.warning("system_tick: %s failed: %s", name, e)
        return f"error: {e}"
