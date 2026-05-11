"""EvolutionOrchestrator — the closed-loop tuner for OC's self-evolution.

Background
==========

OC ships four self-evolution surfaces:

1. Skill auto-extraction pipeline (``extensions/skill-evolution/``).
2. Dreaming v1 — episodic-memory clustering (``agent/dreaming.py``).
3. Dreaming v2 — three-gate promotion into MEMORY.md
   (``agent/dreaming_v2.py``).
4. Outcome-aware learning — ``TurnCompletedEvent`` on the typed bus.

Each surface has its own thresholds (Stage-2 confidence cutoff, score
gate, recall-count gate, diversity gate). Until 2026-05-11 those
thresholds were **static** — there was no feedback path from "what
happened" to "what to do next time".

This module closes that loop.

Responsibilities
================

* Subscribe to :class:`SkillReviewDecisionEvent` (every user accept/
  reject/edit decision) and :class:`TurnCompletedEvent` (per-turn
  outcome signals).
* Maintain a bounded rolling window of recent decisions in memory.
* On a recompute trigger (manual via ``oc evolution tune`` or
  automatic every N decisions), compute new threshold values from
  the rolling window.
* Persist tuning atomically to
  ``<profile_home>/skills/evolution_tuning.json`` so other modules
  (skill-evolution subscriber, dreaming v2 cron) can pick up the
  new values on next run.
* Expose a ``get_tuning()`` accessor with safe defaults so consumers
  never see a half-written file or a missing-file error.

Tuning math (deliberately simple, deliberately monotone)
-------------------------------------------------------

Given the last :data:`_WINDOW_SIZE` decisions:

* ``accepted`` counts as 1.0 vote-positive
* ``edited`` counts as 0.5 vote-positive (partial — user kept the
  pattern but changed it)
* ``rejected`` counts as 0.0
* ``deferred`` does not count (caller skipped)

Let ``accept_rate = sum(weights) / count_non_deferred``.

* If fewer than :data:`_MIN_DECISIONS_TO_TUNE` non-deferred decisions
  exist, do nothing — not enough signal.
* If ``accept_rate < 0.30``: raise ``confidence_threshold`` by
  :data:`_STEP`, capped at :data:`_CONFIDENCE_MAX`. (Stricter — we're
  proposing too aggressively.)
* If ``accept_rate > 0.80``: lower by :data:`_STEP`, floored at
  :data:`_CONFIDENCE_MIN`. (More permissive — we're being too strict.)
* In the dead band ``[0.30, 0.80]``: no change.

Dreaming-v2 score gate moves in lockstep but smaller (×0.01 vs ×5
integer step) because it's a 0-1 float, not 0-100 integer.

Threading / persistence safety
==============================

* Reads are racy-tolerant: ``get_tuning()`` returns
  :data:`DEFAULT_TUNING` on any read error.
* Writes are atomic: write to a sibling ``<file>.tmp`` then
  ``os.replace`` (POSIX-atomic). On platforms supporting ``fcntl``
  we additionally hold an exclusive ``flock`` for the duration of
  the write; on platforms without ``fcntl`` (Windows) we fall back to
  best-effort rename and accept last-writer-wins between concurrent
  CLI runs.

Why a class, not a free function
================================

The orchestrator owns the rolling window. Stateless free functions
would force every caller to reload the window from disk on every
event — wasteful, and a write-storm during heavy review sessions.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("opencomputer.agent.evolution_orchestrator")

# ─── tunables (frozen — published-API; don't change without bumping schema) ─

#: Decisions retained for tuning math. Older ones are evicted FIFO.
_WINDOW_SIZE = 20

#: Minimum non-deferred decisions before tuning runs. Avoids
#: over-correcting on a 2-decision sample.
_MIN_DECISIONS_TO_TUNE = 10

#: Integer step applied to ``confidence_threshold`` per tune.
_STEP = 5

#: Bounds for ``confidence_threshold``. Anything beyond these is
#: pathological (stricter than 95 → nothing ever proposed; more
#: permissive than 50 → noise floods the review queue).
_CONFIDENCE_MIN = 50
_CONFIDENCE_MAX = 95

#: Dead-band: accept-rate inside [LOW, HIGH] triggers no change.
_DEAD_BAND_LOW = 0.30
_DEAD_BAND_HIGH = 0.80

#: Score-threshold step for dreaming-v2 (0-1 float).
_DREAM_SCORE_STEP = 0.05
_DREAM_SCORE_MIN = 0.40
_DREAM_SCORE_MAX = 0.90

#: Min-recall step for dreaming-v2 (integer).
_DREAM_RECALL_STEP = 1
_DREAM_RECALL_MIN = 1
_DREAM_RECALL_MAX = 5

#: Filename of the persisted tuning state under
#: ``<profile_home>/skills/``.
_TUNING_FILENAME = "evolution_tuning.json"

#: Schema version for the persisted JSON; bumped on incompatible
#: changes so older readers can detect + fall back to defaults.
#:
#: v1 (2026-05-11): initial — confidence_threshold,
#:   dreaming_v2_score_threshold, dreaming_v2_min_recall,
#:   decisions_observed, last_recompute_ts.
#: v2 (2026-05-11): adds ``recent_decisions`` array so the rolling
#:   window survives across processes. Standalone CLI invocations
#:   (``oc skills review``) hydrate the orchestrator's in-memory
#:   window from disk on construction, so a user reviewing 3 skills
#:   today + 7 tomorrow accumulates 10 decisions and triggers a tune.
SCHEMA_VERSION = 2

#: Cap on persisted decisions. Matches :data:`_WINDOW_SIZE` so the
#: serialised window equals the in-memory window after re-hydration.
_PERSISTED_DECISIONS_CAP = 20


# ─── public dataclasses ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EvolutionTuning:
    """Snapshot of the orchestrator's tunable parameters.

    Constructed solely by the orchestrator; consumers read via
    :meth:`EvolutionOrchestrator.get_tuning` (instance) or
    :func:`load_tuning` (free function for processes that don't
    hold an orchestrator instance, e.g. the skill-evolution
    subscriber running in the gateway daemon).
    """

    confidence_threshold: int = 70
    dreaming_v2_score_threshold: float = 0.65
    dreaming_v2_min_recall: int = 2
    decisions_observed: int = 0
    last_recompute_ts: float = 0.0
    schema_version: int = SCHEMA_VERSION


#: Module-level defaults — used when no persisted state exists yet, or
#: when persisted state is malformed. Matches the original hard-coded
#: defaults from skill-evolution + dreaming-v2 so flipping the
#: orchestrator on changes nothing until a decision arrives.
DEFAULT_TUNING = EvolutionTuning()


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One decision row retained in the rolling window."""

    timestamp: float
    skill_name: str
    decision: str  # "accepted" | "rejected" | "edited" | "deferred"
    origin_session_id: str = ""
    trace_id: str = ""
    confidence_at_proposal: int = 0


# ─── persistence helpers ─────────────────────────────────────────────


def _tuning_path(profile_home: Path) -> Path:
    """Resolve the tuning file path, ensuring the parent dir exists."""
    skills_dir = profile_home / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir / _TUNING_FILENAME


def _load_raw_state(profile_home: Path) -> dict | None:
    """Read + parse the tuning JSON; return None on any failure.

    Internal helper so :func:`load_tuning` (returns the tuning dataclass)
    and :func:`load_recent_decisions` (returns the persisted window)
    can share the parse path without re-reading the file twice.

    Schema-version mismatch returns None — the caller decides whether
    to fall back to defaults (tuning) or an empty list (window).
    """
    if not profile_home or not profile_home.exists():
        return None
    path = profile_home / "skills" / _TUNING_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "evolution_tuning.json malformed at %s: %s — using defaults",
            path,
            exc,
        )
        return None
    if not isinstance(data, dict):
        return None
    persisted_version = int(data.get("schema_version", 0) or 0)
    # Forward + backward compat policy:
    #   v1 file read by v2+ code: accepted; ``recent_decisions`` will
    #     be missing → caller treats it as empty window. No data loss.
    #   v2 file read by v1 code (deprecated):
    #     handled in v1's strict load — they return defaults. Future
    #     code paths shouldn't read v1 inline anyway.
    #   future v3 read by v2 code: accept up through current
    #     SCHEMA_VERSION; unknown future fields are ignored via
    #     ``.get(name, default)`` calls.
    #   schema_version 0 / missing / negative: reject as corrupt.
    if persisted_version < 1 or persisted_version > SCHEMA_VERSION:
        logger.info(
            "evolution_tuning.json schema_version unsupported "
            "(expected 1..%d, got %r) — using defaults",
            SCHEMA_VERSION,
            data.get("schema_version"),
        )
        return None
    return data


def load_tuning(profile_home: Path) -> EvolutionTuning:
    """Read the persisted tuning state for ``profile_home``.

    Returns :data:`DEFAULT_TUNING` on any failure (missing file,
    malformed JSON, schema mismatch). Consumers should never crash on
    bad tuning state — it's an optimisation, not a correctness signal.

    This is the read entry point for the skill-evolution subscriber,
    the dreaming-v2 cron, and any future evolution-aware consumer.
    """
    data = _load_raw_state(profile_home)
    if data is None:
        return DEFAULT_TUNING
    try:
        return EvolutionTuning(
            confidence_threshold=_clamp_int(
                data.get("confidence_threshold", DEFAULT_TUNING.confidence_threshold),
                _CONFIDENCE_MIN,
                _CONFIDENCE_MAX,
            ),
            dreaming_v2_score_threshold=_clamp_float(
                float(
                    data.get(
                        "dreaming_v2_score_threshold",
                        DEFAULT_TUNING.dreaming_v2_score_threshold,
                    )
                ),
                _DREAM_SCORE_MIN,
                _DREAM_SCORE_MAX,
            ),
            dreaming_v2_min_recall=_clamp_int(
                data.get(
                    "dreaming_v2_min_recall",
                    DEFAULT_TUNING.dreaming_v2_min_recall,
                ),
                _DREAM_RECALL_MIN,
                _DREAM_RECALL_MAX,
            ),
            decisions_observed=int(
                data.get("decisions_observed", 0) or 0
            ),
            last_recompute_ts=float(
                data.get("last_recompute_ts", 0.0) or 0.0
            ),
        )
    except (TypeError, ValueError) as exc:
        logger.warning(
            "evolution_tuning.json field coercion failed: %s — using defaults",
            exc,
        )
        return DEFAULT_TUNING


def load_recent_decisions(profile_home: Path) -> list[DecisionRecord]:
    """Read the persisted decision window for ``profile_home``.

    Returns an empty list on any failure (missing file, malformed
    JSON, schema mismatch, missing field). Used by the orchestrator
    at construction time to hydrate its in-memory window so the
    rolling 20-decision count survives across CLI invocations.

    Individual entries that fail to coerce are skipped — a malformed
    row in the window doesn't poison the whole hydration.
    """
    data = _load_raw_state(profile_home)
    if data is None:
        return []
    raw_rows = data.get("recent_decisions") or []
    if not isinstance(raw_rows, list):
        return []
    out: list[DecisionRecord] = []
    for row in raw_rows[-_PERSISTED_DECISIONS_CAP:]:
        if not isinstance(row, dict):
            continue
        try:
            out.append(
                DecisionRecord(
                    timestamp=float(row.get("timestamp", 0.0) or 0.0),
                    skill_name=str(row.get("skill_name", "") or ""),
                    decision=str(row.get("decision", "deferred") or "deferred"),
                    origin_session_id=str(
                        row.get("origin_session_id", "") or ""
                    ),
                    trace_id=str(row.get("trace_id", "") or ""),
                    confidence_at_proposal=int(
                        row.get("confidence_at_proposal", 0) or 0
                    ),
                )
            )
        except (TypeError, ValueError):
            # Bad row — skip silently. The rest of the window is fine.
            continue
    return out


def _clamp_int(value: Any, lo: int, hi: int) -> int:
    """Coerce to int, clamp to [lo, hi]. Falls back to ``lo`` on bad input."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def _clamp_float(value: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomically replace ``path`` with the JSON serialisation of
    ``payload``.

    On POSIX with ``fcntl`` available, we hold an exclusive lock on a
    sibling ``.lock`` file across the write so two concurrent CLI
    runs can't interleave. On Windows we degrade to plain ``os.replace``
    (which is atomic on NTFS but doesn't protect against writer
    interleavings — last write wins).
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    lock_path = path.with_suffix(path.suffix + ".lock")

    serialised = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    try:
        import fcntl  # type: ignore[import-not-found]
    except ImportError:
        fcntl = None  # type: ignore[assignment]

    if fcntl is not None:
        # Acquire exclusive lock; create the lock file if missing.
        # Hold it across the write + rename.
        lock_file = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            tmp.write_text(serialised, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock_file.close()
    else:
        tmp.write_text(serialised, encoding="utf-8")
        os.replace(tmp, path)


# ─── orchestrator ────────────────────────────────────────────────────


class EvolutionOrchestrator:
    """Subscribe to outcome events; tune thresholds on a rolling window.

    Lifecycle is explicit: construction is side-effect-free, attach to
    a bus via :meth:`start`, detach via :meth:`stop`. The skill-evolution
    subscriber and the dreaming-v2 cron read tuned values via the
    module-level :func:`load_tuning` so they don't depend on a live
    orchestrator instance.

    The orchestrator does NOT score langfuse traces directly — that's
    the langfuse plugin's job (it subscribes to the same event
    independently). Decoupling keeps the orchestrator usable when
    langfuse is inert.
    """

    def __init__(
        self,
        *,
        bus: Any,  # opencomputer.ingestion.bus.TypedEventBus (duck-typed)
        profile_home: Path,
        langfuse_score_fn: Any = None,
    ) -> None:
        if profile_home is None:
            raise ValueError("profile_home is required")
        self._bus = bus
        self._profile_home = Path(profile_home)
        self._langfuse_score_fn = langfuse_score_fn

        # Rolling window of recent decisions. Thread-safe via _lock
        # because the gateway can fire bus handlers from multiple
        # event-loop tasks concurrently.
        self._window: deque[DecisionRecord] = deque(maxlen=_WINDOW_SIZE)
        self._lock = threading.Lock()

        # Subscription handles for clean teardown.
        self._sub_decision: Any = None
        self._sub_turn: Any = None

        # Cumulative decision counter. Hydrated from persisted state
        # at construction time so the auto-tune trigger (every Nth
        # decision) sees the correct cross-process total, not just
        # this process's increments.
        persisted = load_tuning(self._profile_home)
        self._total_decisions_observed = int(persisted.decisions_observed or 0)

        # Hydrate the rolling window from disk so a CLI user reviewing
        # 3 skills today + 7 tomorrow accumulates 10 decisions and
        # triggers a tune. Without this, every new CLI process started
        # with an empty window and auto-tune never fired in CLI mode.
        hydrated = load_recent_decisions(self._profile_home)
        for rec in hydrated:
            self._window.append(rec)

    # ─── lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to the typed bus. Idempotent."""
        if self._sub_decision is not None:
            return
        self._sub_decision = self._bus.subscribe(
            "skill_review_decision", self._on_decision
        )
        # Turn-completed subscriptions are currently observation-only —
        # accumulating per-turn signals is a future hook (e.g. for
        # dreaming-score gating). We subscribe now to avoid a wiring
        # change later, and the handler is a no-op when no consumer
        # exists.
        self._sub_turn = self._bus.subscribe(
            "turn_completed", self._on_turn_completed
        )

    def stop(self) -> None:
        """Unsubscribe. Idempotent. Safe to call from atexit."""
        for sub_name in ("_sub_decision", "_sub_turn"):
            sub = getattr(self, sub_name, None)
            setattr(self, sub_name, None)
            if sub is None:
                continue
            try:
                sub.unsubscribe()
            except Exception:  # noqa: BLE001 — never raise from stop
                logger.warning(
                    "EvolutionOrchestrator: %s.unsubscribe raised",
                    sub_name,
                    exc_info=True,
                )

    # ─── event handlers ───────────────────────────────────────────

    def _on_decision(self, evt: Any) -> None:
        """Append a decision to the rolling window; opportunistically tune."""
        try:
            record = DecisionRecord(
                timestamp=float(getattr(evt, "timestamp", time.time())),
                skill_name=str(getattr(evt, "skill_name", "") or ""),
                decision=str(getattr(evt, "decision", "deferred") or "deferred"),
                origin_session_id=str(
                    getattr(evt, "origin_session_id", "") or ""
                ),
                trace_id=str(getattr(evt, "trace_id", "") or ""),
                confidence_at_proposal=int(
                    getattr(evt, "confidence_at_proposal", 0) or 0
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EvolutionOrchestrator: malformed decision event: %s", exc
            )
            return

        with self._lock:
            self._window.append(record)
            self._total_decisions_observed += 1

        # Persist the window on every decision so a process crash /
        # SIGINT mid-session doesn't lose the in-memory state. The
        # write is fast (atomic rename of a <2KB JSON); cost ~ms.
        # Errors swallowed — file-write failure must not crash a
        # user-driven accept/reject.
        try:
            self._persist_window_only()
        except Exception:  # noqa: BLE001
            logger.debug(
                "EvolutionOrchestrator: window persist failed", exc_info=True
            )

        # Best-effort langfuse score side-channel. Failures swallowed
        # so the orchestrator's tuning is independent of langfuse
        # availability.
        self._score_trace_if_possible(record)

        # Recompute opportunistically — every N decisions, run a tune
        # pass. The math is cheap (one deque scan + one file write).
        if self._total_decisions_observed % _MIN_DECISIONS_TO_TUNE == 0:
            try:
                self.recompute_tuning()
            except Exception:  # noqa: BLE001 — opportunistic tune must never raise
                logger.warning(
                    "EvolutionOrchestrator: opportunistic tune raised",
                    exc_info=True,
                )

    def _on_turn_completed(self, evt: Any) -> None:
        """Currently observation-only — reserved for future signal mining.

        The orchestrator does not act on per-turn signals yet. The
        subscription exists so a future iteration can mine
        ``turn_completed`` signals (e.g. turn-cost, tool-diversity,
        error-recovery) without re-wiring the bus.
        """
        logger.debug(
            "EvolutionOrchestrator turn_completed: session=%s turn=%d",
            getattr(evt, "session_id", ""),
            getattr(evt, "turn_index", 0),
        )

    def _score_trace_if_possible(self, record: DecisionRecord) -> None:
        """Hand the decision to the langfuse plugin's scoring callback.

        ``self._langfuse_score_fn`` is a ``(trace_id, decision) -> None``
        callable injected at construction time. When None (langfuse not
        loaded), this is a no-op. Failures are swallowed.
        """
        fn = self._langfuse_score_fn
        if fn is None or not record.trace_id:
            return
        try:
            fn(record.trace_id, record.decision)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EvolutionOrchestrator: langfuse score fn raised: %s", exc
            )

    # ─── tuning math + persistence ────────────────────────────────

    def recompute_tuning(self) -> EvolutionTuning:
        """Compute new tuning from the current window and persist it.

        Returns the new :class:`EvolutionTuning`. Always persists, even
        when the result equals the existing values, so the
        ``last_recompute_ts`` field tracks every recompute attempt.

        Publishes :class:`EvolutionTuningChangedEvent` on the bus on
        every successful recompute so dashboards / wire clients can
        react. The event's ``changed`` flag is True when at least one
        tunable shifted; False on a no-op recompute (dead band).

        Safe to call manually (``oc evolution tune``) or
        automatically (every :data:`_MIN_DECISIONS_TO_TUNE` decisions).
        """
        with self._lock:
            window_snapshot = list(self._window)

        current = load_tuning(self._profile_home)
        new_tuning = compute_new_tuning(
            window=window_snapshot,
            current=current,
            total_decisions=self._total_decisions_observed,
        )
        self._persist(new_tuning)
        logger.info(
            "evolution tuning recomputed: "
            "confidence=%d→%d dream_score=%.2f→%.2f dream_recall=%d→%d "
            "(window=%d, total=%d)",
            current.confidence_threshold,
            new_tuning.confidence_threshold,
            current.dreaming_v2_score_threshold,
            new_tuning.dreaming_v2_score_threshold,
            current.dreaming_v2_min_recall,
            new_tuning.dreaming_v2_min_recall,
            len(window_snapshot),
            self._total_decisions_observed,
        )
        # Publish on the bus so dashboards / wire clients / future
        # observability sinks can react without polling the JSON file.
        # Failures swallowed — telemetry never blocks the tune path.
        changed = (
            new_tuning.confidence_threshold != current.confidence_threshold
            or new_tuning.dreaming_v2_score_threshold
            != current.dreaming_v2_score_threshold
            or new_tuning.dreaming_v2_min_recall
            != current.dreaming_v2_min_recall
        )
        try:
            from plugin_sdk.ingestion import EvolutionTuningChangedEvent

            self._bus.publish(
                EvolutionTuningChangedEvent(
                    source="evolution_orchestrator",
                    confidence_threshold=new_tuning.confidence_threshold,
                    dreaming_v2_score_threshold=new_tuning.dreaming_v2_score_threshold,
                    dreaming_v2_min_recall=new_tuning.dreaming_v2_min_recall,
                    decisions_observed=self._total_decisions_observed,
                    changed=changed,
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "EvolutionOrchestrator: tuning-change publish failed",
                exc_info=True,
            )
        return new_tuning

    def get_tuning(self) -> EvolutionTuning:
        """Return the currently-persisted tuning."""
        return load_tuning(self._profile_home)

    def window_snapshot(self) -> list[DecisionRecord]:
        """Return a copy of the rolling decision window for inspection."""
        with self._lock:
            return list(self._window)

    def reset(self) -> EvolutionTuning:
        """Reset tuning to module defaults and clear the rolling window.

        Used by ``oc evolution reset`` when the user wants to throw out
        accumulated state (e.g. after deliberately seeding the review
        queue with test data). Also publishes an
        :class:`EvolutionTuningChangedEvent` so dashboards refresh.
        """
        prior = load_tuning(self._profile_home)
        with self._lock:
            self._window.clear()
            self._total_decisions_observed = 0
        self._persist(DEFAULT_TUNING)
        logger.info("evolution tuning reset to defaults")
        try:
            from plugin_sdk.ingestion import EvolutionTuningChangedEvent

            self._bus.publish(
                EvolutionTuningChangedEvent(
                    source="evolution_orchestrator.reset",
                    confidence_threshold=DEFAULT_TUNING.confidence_threshold,
                    dreaming_v2_score_threshold=DEFAULT_TUNING.dreaming_v2_score_threshold,
                    dreaming_v2_min_recall=DEFAULT_TUNING.dreaming_v2_min_recall,
                    decisions_observed=0,
                    changed=(
                        prior.confidence_threshold
                        != DEFAULT_TUNING.confidence_threshold
                        or prior.dreaming_v2_score_threshold
                        != DEFAULT_TUNING.dreaming_v2_score_threshold
                        or prior.dreaming_v2_min_recall
                        != DEFAULT_TUNING.dreaming_v2_min_recall
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "EvolutionOrchestrator: reset publish failed", exc_info=True
            )
        return DEFAULT_TUNING

    def _persist(self, tuning: EvolutionTuning) -> None:
        """Write the tuning + window to disk atomically.

        Errors logged, never raised. The persisted payload conforms to
        :data:`SCHEMA_VERSION` (v2): tunables + decisions_observed +
        last_recompute_ts + recent_decisions array. Older v1 readers
        fall back to defaults on the schema mismatch — that's the
        intended migration path.
        """
        path = _tuning_path(self._profile_home)
        payload = asdict(tuning)
        # Override timestamp/observed at persist time so the recorded
        # values are accurate regardless of how ``tuning`` was built.
        payload["last_recompute_ts"] = time.time()
        payload["decisions_observed"] = self._total_decisions_observed
        # v2: include the rolling window so it survives across
        # processes. Stored as a JSON array of dicts to keep the file
        # human-readable + grep-able. Capped at the window size.
        with self._lock:
            window_snapshot = list(self._window)
        payload["recent_decisions"] = [
            asdict(rec) for rec in window_snapshot[-_PERSISTED_DECISIONS_CAP:]
        ]
        try:
            _atomic_write_json(path, payload)
        except Exception:  # noqa: BLE001 — never break callers on a write failure
            logger.warning(
                "evolution tuning persist failed for %s",
                path,
                exc_info=True,
            )

    def _persist_window_only(self) -> None:
        """Persist just the rolling window + count, preserving current
        tuning values.

        Called on every decision so the window survives crashes /
        SIGINT mid-session. Reads the current tuning from disk so the
        write doesn't accidentally clobber a tune that landed between
        decisions.
        """
        current = load_tuning(self._profile_home)
        # Build a tuning struct with the persisted values + updated
        # decision count, then call the full persister. Cheap — the
        # cost is one extra file read which we already pay on the
        # recompute path.
        snapshot = EvolutionTuning(
            confidence_threshold=current.confidence_threshold,
            dreaming_v2_score_threshold=current.dreaming_v2_score_threshold,
            dreaming_v2_min_recall=current.dreaming_v2_min_recall,
            decisions_observed=self._total_decisions_observed,
            last_recompute_ts=current.last_recompute_ts,
        )
        self._persist(snapshot)


# ─── pure tuning math (testable without bus/IO) ───────────────────────


def compute_new_tuning(
    *,
    window: Iterable[DecisionRecord],
    current: EvolutionTuning,
    total_decisions: int,
) -> EvolutionTuning:
    """Compute the next tuning given a decision window.

    Pure function — no IO. Exposed for unit tests that exercise the
    math without standing up a bus.

    Decision weights:

    * ``"accepted"`` → 1.0
    * ``"edited"`` → 0.5
    * ``"rejected"`` → 0.0
    * ``"deferred"`` → not counted

    Returns ``current`` unchanged when fewer than
    :data:`_MIN_DECISIONS_TO_TUNE` non-deferred decisions exist or the
    accept-rate sits in the dead band.
    """
    weights = {"accepted": 1.0, "edited": 0.5, "rejected": 0.0}
    counted: list[float] = []
    for rec in window:
        if rec.decision in weights:
            counted.append(weights[rec.decision])
    if len(counted) < _MIN_DECISIONS_TO_TUNE:
        # Not enough signal; return current unchanged (timestamp is
        # set by the persister, not the math).
        return EvolutionTuning(
            confidence_threshold=current.confidence_threshold,
            dreaming_v2_score_threshold=current.dreaming_v2_score_threshold,
            dreaming_v2_min_recall=current.dreaming_v2_min_recall,
            decisions_observed=total_decisions,
            last_recompute_ts=current.last_recompute_ts,
        )
    accept_rate = sum(counted) / len(counted)

    new_conf = current.confidence_threshold
    new_dream_score = current.dreaming_v2_score_threshold
    new_dream_recall = current.dreaming_v2_min_recall

    if accept_rate < _DEAD_BAND_LOW:
        # Tighten — we're over-proposing.
        new_conf = min(_CONFIDENCE_MAX, current.confidence_threshold + _STEP)
        new_dream_score = min(
            _DREAM_SCORE_MAX,
            current.dreaming_v2_score_threshold + _DREAM_SCORE_STEP,
        )
        new_dream_recall = min(
            _DREAM_RECALL_MAX,
            current.dreaming_v2_min_recall + _DREAM_RECALL_STEP,
        )
    elif accept_rate > _DEAD_BAND_HIGH:
        # Loosen — users want more proposals than we're giving.
        new_conf = max(_CONFIDENCE_MIN, current.confidence_threshold - _STEP)
        new_dream_score = max(
            _DREAM_SCORE_MIN,
            current.dreaming_v2_score_threshold - _DREAM_SCORE_STEP,
        )
        new_dream_recall = max(
            _DREAM_RECALL_MIN,
            current.dreaming_v2_min_recall - _DREAM_RECALL_STEP,
        )
    # else: dead band → no change.

    return EvolutionTuning(
        confidence_threshold=new_conf,
        dreaming_v2_score_threshold=round(new_dream_score, 3),
        dreaming_v2_min_recall=new_dream_recall,
        decisions_observed=total_decisions,
        last_recompute_ts=current.last_recompute_ts,
    )


# ─── per-process singleton (lazy, opt-in) ────────────────────────────


# Module-global singleton handle. Set by ``get_or_start_orchestrator``
# the first time a CLI surface needs to emit / observe decisions in a
# process where the gateway daemon is not running.
_singleton: EvolutionOrchestrator | None = None
_singleton_lock = threading.Lock()


def get_or_start_orchestrator(
    profile_home: Path | None = None,
) -> EvolutionOrchestrator | None:
    """Return a started orchestrator for the current process, lazily.

    Use case: CLI surfaces like ``oc skills review`` emit
    ``SkillReviewDecisionEvent`` on the default bus. In the gateway
    daemon path the orchestrator subscribed at boot time. In the
    standalone CLI path the gateway isn't running, so without this
    helper the events would have no subscriber and tuning would never
    advance.

    Returns ``None`` on any failure — callers fire-and-forget; if the
    orchestrator can't start, decisions still flow on the bus and any
    other subscribers receive them. Telemetry never blocks user
    actions.

    Thread-safe via :data:`_singleton_lock` so two concurrent CLI
    sub-commands (unusual but legal) don't double-start.

    Args:
        profile_home: Where to persist tuning state. Defaults to the
            active OC profile home resolved via
            ``opencomputer.agent.config._home``.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            return _singleton

        try:
            if profile_home is None:
                from opencomputer.agent.config import _home

                profile_home = _home()

            # Resolve optional langfuse score callback. When langfuse
            # isn't loaded (env vars unset, SDK missing) we still start
            # the orchestrator — just without trace scoring.
            score_fn: Any = None
            try:
                from extensions.langfuse.plugin import (  # type: ignore[import-not-found]
                    score_trace as _langfuse_score_trace,
                )

                score_fn = _langfuse_score_trace
            except Exception:  # noqa: BLE001 — langfuse not loaded; that's fine
                pass

            from opencomputer.ingestion.bus import default_bus

            orchestrator = EvolutionOrchestrator(
                bus=default_bus,
                profile_home=Path(profile_home),
                langfuse_score_fn=score_fn,
            )
            orchestrator.start()
            _singleton = orchestrator
            logger.info(
                "evolution orchestrator singleton started (profile=%s, langfuse=%s)",
                profile_home,
                "on" if score_fn is not None else "off",
            )
            return _singleton
        except Exception:  # noqa: BLE001 — never block the CLI on telemetry init
            logger.warning(
                "evolution orchestrator singleton start failed",
                exc_info=True,
            )
            return None


def shutdown_singleton() -> None:
    """Stop the singleton if one was started; clear the global handle.

    Useful in tests and in CLI atexit handlers. Idempotent.
    """
    global _singleton
    with _singleton_lock:
        s = _singleton
        _singleton = None
    if s is None:
        return
    try:
        s.stop()
    except Exception:  # noqa: BLE001
        logger.warning("evolution orchestrator singleton stop failed", exc_info=True)


__all__ = [
    "DEFAULT_TUNING",
    "DecisionRecord",
    "EvolutionOrchestrator",
    "EvolutionTuning",
    "SCHEMA_VERSION",
    "compute_new_tuning",
    "get_or_start_orchestrator",
    "load_recent_decisions",
    "load_tuning",
    "shutdown_singleton",
]
