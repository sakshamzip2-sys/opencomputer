"""F2 bus subscriber for auto-skill-evolution (T5).

Wires :class:`SessionEndEvent` arrivals on the
:class:`opencomputer.ingestion.bus.TypedEventBus` to the three-stage
pipeline shipped in T2-T4:

1. :func:`extensions.skill_evolution.pattern_detector.is_candidate_session`
   — synchronous heuristic gate. Cheap. Run on every event that passes the
   enabled-state check.
2. :func:`extensions.skill_evolution.pattern_detector.judge_candidate_async`
   — async LLM judge. Cost-guarded.
3. :func:`extensions.skill_evolution.skill_extractor.extract_skill_from_session`
   — async 3-call SKILL.md generator. Also cost-guarded. Runs only when
   the judge clears the configured confidence threshold AND marks the
   session as ``is_novel``.

Failure isolation
-----------------
The bus contract requires subscribers to never raise into the publish
path; per-event work is offloaded to a fire-and-forget task and every
stage of the pipeline is wrapped in try/except. A SessionDB hiccup, a
provider exception, or a candidate-store write error must not break
subsequent events.

State / control
---------------
Activation is gated by an on-disk JSON file:

::

    <profile_home>/skills/evolution_state.json   {"enabled": <bool>}

A missing file is treated as ``enabled=false`` so the feature ships
opt-in. T6 supplies the CLI for flipping it.

Heartbeat
---------
Every event arrival while enabled writes
``<profile_home>/skills/evolution_heartbeat`` (timestamp). Lets the
operator confirm the subscriber is wired without enabling the LLM
pipeline first.

Privacy / logging
-----------------
We log session_id and the binary outcome of each pipeline stage. We
never log session content (transcripts, user messages, tool calls).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencomputer.hooks.runner import fire_and_forget
from plugin_sdk.ingestion import SessionEndEvent

from .candidate_store import add_candidate
from .pattern_detector import is_candidate_session, judge_candidate_async
from .session_metrics import compute_session_metrics
from .skill_extractor import extract_skill_from_session

_log = logging.getLogger("opencomputer.skill_evolution.subscriber")

_STATE_FILENAME = "evolution_state.json"
_HEARTBEAT_FILENAME = "evolution_heartbeat"


def _is_enabled(profile_home: Path) -> bool:
    """Read state file; treat missing/unreadable as disabled."""
    state_path = profile_home / "skills" / _STATE_FILENAME
    try:
        raw = state_path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return False
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _log.warning(
            "skill-evolution: malformed state.json at %s — treating as disabled",
            state_path,
        )
        return False
    return bool(data.get("enabled", False))


def _write_heartbeat(profile_home: Path) -> None:
    """Best-effort heartbeat write. OSErrors are logged at DEBUG only."""
    hb = profile_home / "skills" / _HEARTBEAT_FILENAME
    try:
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(str(time.time()))
    except OSError:
        _log.debug("skill-evolution: heartbeat write failed", exc_info=True)


class EvolutionSubscriber:
    """Subscribes to ``session_end`` on the F2 bus; runs the auto-evo pipeline.

    Construction is side-effect-free — call :meth:`start` to attach to the
    bus, and :meth:`stop` to detach. The handler intentionally returns a
    coroutine the bus's ``apublish`` can await; the heavy lifting runs in a
    fire-and-forget task so it never blocks fanout. ``publish`` (sync)
    callers will see the coroutine closed without execution — the stated
    integration point is ``apublish``.
    """

    def __init__(
        self,
        *,
        bus: Any,  # opencomputer.ingestion.bus.TypedEventBus (duck-typed)
        profile_home_factory: Callable[[], Path],
        session_db_factory: Callable[[], Any],
        provider: Any,
        cost_guard: Any,
        sensitive_filter: Callable[[Any], bool] | None = None,
        confidence_threshold: int = 70,
    ) -> None:
        self._bus = bus
        self._profile_home_factory = profile_home_factory
        self._session_db_factory = session_db_factory
        self._provider = provider
        self._cost_guard = cost_guard
        self._sensitive_filter = sensitive_filter
        self._confidence_threshold = int(confidence_threshold)

        self._subscription: Any = None

    # ─── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to ``session_end`` events on the bus.

        Idempotent — calling :meth:`start` twice without an intervening
        :meth:`stop` returns early without registering a duplicate handler.
        """
        if self._subscription is not None:
            return
        self._subscription = self._bus.subscribe("session_end", self._handle_event)

    def stop(self) -> None:
        """Unsubscribe from the bus. Idempotent.

        In-flight pipeline tasks are intentionally left to drain via
        :func:`opencomputer.hooks.runner.drain_pending` — cancelling them
        here would risk leaving half-written candidate dirs.
        """
        sub = self._subscription
        self._subscription = None
        if sub is None:
            return
        try:
            sub.unsubscribe()
        except Exception:  # noqa: BLE001 — never raise from stop
            _log.warning(
                "skill-evolution: subscription.unsubscribe raised (continuing)",
                exc_info=True,
            )

    # ─── handlers ──────────────────────────────────────────────────────

    async def _handle_event(self, event: SessionEndEvent) -> None:
        """Bus-facing handler — fast, never raises into the bus.

        Reads the state file, writes the heartbeat, and (when enabled)
        spawns the heavy pipeline as a fire-and-forget task. A disabled
        feature never spawns a task at all.
        """
        try:
            profile_home = self._profile_home_factory()
        except Exception:  # noqa: BLE001 — bad factory must not poison the bus
            _log.warning(
                "skill-evolution: profile_home_factory raised", exc_info=True
            )
            return

        if not _is_enabled(profile_home):
            return

        _write_heartbeat(profile_home)

        # Fire-and-forget the rest of the pipeline. We bind the loop-local
        # event so the task carries it without referencing this method's
        # frame after we return.
        fire_and_forget(self._run_pipeline(event))

    async def _run_pipeline(self, event: SessionEndEvent) -> None:
        """Run Stage 1 → Stage 2 → Extractor → store. Never raises.

        Each stage logs its outcome at INFO; failures log at WARNING.
        Logs include session_id only — never transcript content.
        """
        session_id = getattr(event, "session_id", None) or ""
        try:
            profile_home = self._profile_home_factory()
            session_db = self._session_db_factory()
            existing_skills_dir = profile_home / "skills"

            # ── Stage 1: heuristic ───────────────────────────────────
            # Compute SessionMetrics from real SessionDB.get_messages()
            # output. The detector reads pre-derived fields off this
            # dataclass — no DB hits inside the detector itself.
            metrics = compute_session_metrics(session_db, session_id)
            if metrics is None:
                _log.info(
                    "skill-evolution: session=%s rejected at stage 1 (no messages)",
                    session_id,
                )
                return

            score = is_candidate_session(
                event,
                metrics=metrics,
                existing_skills_dir=existing_skills_dir,
                sensitive_filter=self._sensitive_filter,
            )
            if not getattr(score, "is_candidate", False):
                _log.info(
                    "skill-evolution: session=%s rejected at stage 1 (%s)",
                    session_id,
                    getattr(score, "rejection_reason", ""),
                )
                return

            # ── Stage 2: LLM judge ───────────────────────────────────
            judge = await judge_candidate_async(
                score,
                transcript_summary=getattr(score, "summary_hint", "") or "",
                existing_skill_names=[],
                provider=self._provider,
                cost_guard=self._cost_guard,
            )
            if judge is None:
                _log.info(
                    "skill-evolution: session=%s — judge unavailable / parse failed",
                    session_id,
                )
                return

            confidence = int(getattr(judge, "confidence", 0) or 0)
            is_novel = bool(getattr(judge, "is_novel", False))
            if confidence < self._confidence_threshold or not is_novel:
                _log.info(
                    "skill-evolution: session=%s skipped — confidence=%d "
                    "novel=%s threshold=%d",
                    session_id,
                    confidence,
                    is_novel,
                    self._confidence_threshold,
                )
                return

            # ── Extraction ───────────────────────────────────────────
            target_session_id = getattr(score, "session_id", "") or session_id
            proposal = await extract_skill_from_session(
                target_session_id,
                session_db=session_db,
                judge_result=judge,
                provider=self._provider,
                cost_guard=self._cost_guard,
                sensitive_filter=self._sensitive_filter,
            )
            if proposal is None:
                _log.info(
                    "skill-evolution: session=%s — extractor returned None",
                    session_id,
                )
                return

            # ── Stage 4: stage the candidate ─────────────────────────
            try:
                staged_path = add_candidate(profile_home, proposal)
            except Exception:  # noqa: BLE001 — store failure must not crash
                _log.warning(
                    "skill-evolution: session=%s add_candidate failed",
                    session_id,
                    exc_info=True,
                )
                return

            _log.info(
                "skill-evolution: session=%s staged candidate at %s "
                "(confidence=%d)",
                session_id,
                staged_path,
                confidence,
            )
        except Exception:  # noqa: BLE001 — fire-and-forget must never raise
            _log.warning(
                "skill-evolution: session=%s pipeline raised: boom-suppressed",
                session_id,
                exc_info=True,
            )


__all__ = ["EvolutionSubscriber"]
