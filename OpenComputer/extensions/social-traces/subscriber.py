"""Post-task SessionEndEvent subscriber — Phase 5: real decision tree.

Replaces the Phase 2 stub. Subscribes to ``session_end`` on the typed
event bus, and on each event arrival routes through the rule (d)
decision tree:

::

    SessionEndEvent arrives
        │
        ▼
    is_enabled? ────── false ──→ return
        │ true
        ▼
    write heartbeat
        │
        ▼
    pop_session(session_id) — read + clear bridge
        │
        ▼
    BEFORE_TASK ever fired? ─── false ──→ return
        │  (means the plugin was disabled at hook time
        │   or the session ended before any prompt was sent)
        │ true
        ▼
    trace_used set?  ─── true ──→ run novelty judge (Phase 6 stub)
        │                              │
        │                              ▼
        │                          is_novel? ─── false ──→ return (silent)
        │                              │ true
        │                              ▼
        │                          continue to distill+submit
        │
        │ false (no trace used → genuinely explored)
        ▼
    distill_session() (Phase 7 stub)
        │
        ▼
    proposal is None? ─── true ──→ return (nothing distilled)
        │ false
        ▼
    client.submit(proposal)
        │
        ▼
    accepted? ─── false ──→ enqueue to outbox (Phase 9)
        │ true
        ▼
    log + return

Failure isolation
-----------------
The bus contract requires subscribers to never raise into the publish
path. ``_handle_event`` is fire-and-forget against the heavy work; every
stage of ``_run_pipeline`` is wrapped in try/except. A SessionDB hiccup,
a provider exception, or a submit failure must not break subsequent
events.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencomputer.hooks.runner import fire_and_forget
from plugin_sdk.ingestion import SessionEndEvent
from plugin_sdk.traces import TraceNetworkClient

from . import distiller, novelty_judge, session_state, state
from .config import SocialTracesConfig
from .identity import get_or_create_agent_id

_log = logging.getLogger("opencomputer.social_traces.subscriber")


class TraceEmissionSubscriber:
    """Subscribes to ``session_end`` on the F2 bus.

    Construction is side-effect-free — call :meth:`start` to attach,
    :meth:`stop` to detach. Both are idempotent.

    The subscriber receives:

    * ``bus``: the typed event bus (duck-typed; production uses
      :class:`opencomputer.ingestion.bus.TypedEventBus`).
    * ``profile_home_factory``: lazy resolver for the active profile
      home — re-resolved per event so multi-profile dispatch sees the
      correct paths.
    * ``client_factory``: lazy resolver for a
      :class:`TraceNetworkClient`. Resolved per event so config
      changes take effect without a daemon restart. Returns the
      backend selected by ``social_traces.backend`` in config.yaml.
    * ``config_factory``: lazy resolver for the parsed
      :class:`SocialTracesConfig`.
    """

    def __init__(
        self,
        *,
        bus: Any,
        profile_home_factory: Callable[[], Path],
        client_factory: Callable[[Path, SocialTracesConfig], TraceNetworkClient],
        config_factory: Callable[[Path], SocialTracesConfig],
    ) -> None:
        self._bus = bus
        self._profile_home_factory = profile_home_factory
        self._client_factory = client_factory
        self._config_factory = config_factory
        self._subscription: Any = None

    # ─── lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to ``session_end`` events on the bus. Idempotent."""
        if self._subscription is not None:
            return
        self._subscription = self._bus.subscribe("session_end", self._handle_event)

    def stop(self) -> None:
        """Unsubscribe. Idempotent. In-flight pipeline tasks intentionally
        drain via :func:`opencomputer.hooks.runner.drain_pending` rather
        than being cancelled here — cancelling mid-distill could leave
        half-written outbox files."""
        sub = self._subscription
        self._subscription = None
        if sub is None:
            return
        try:
            sub.unsubscribe()
        except Exception:  # noqa: BLE001
            _log.warning(
                "social-traces: subscription.unsubscribe raised (continuing)",
                exc_info=True,
            )

    # ─── handlers ──────────────────────────────────────────────────

    async def _handle_event(self, event: SessionEndEvent) -> None:
        """Bus-facing handler — fast, never raises into the bus.

        Reads the enabled flag, writes heartbeat, and offloads the
        heavy pipeline as a fire-and-forget task. A disabled feature
        never spawns a task at all — keeps overhead at zero for the
        common case.
        """
        try:
            profile_home = self._profile_home_factory()
        except Exception:  # noqa: BLE001 — bad factory must not poison the bus
            _log.warning(
                "social-traces: profile_home_factory raised", exc_info=True,
            )
            return

        if not state.is_enabled(profile_home):
            # IMPORTANT: still pop the session entry so a long-lived
            # daemon that disables/re-enables the plugin doesn't leak
            # bridge state from the disabled window.
            session_state.pop_session(event.session_id)
            return

        state.write_heartbeat(profile_home)
        fire_and_forget(self._run_pipeline(event, profile_home))

    # ─── the pipeline ──────────────────────────────────────────────

    async def _run_pipeline(
        self, event: SessionEndEvent, profile_home: Path
    ) -> None:
        """Run the full Phase 5 decision tree. Never raises.

        Each stage is wrapped — a failure logs at WARNING and
        short-circuits. The bridge entry is ALWAYS popped at the end
        so a daemon doesn't accumulate state for sessions whose
        pipeline crashed mid-flight.
        """
        session_id = event.session_id
        try:
            # ── Read + clear the bridge ─────────────────────────────
            entry = session_state.pop_session(session_id)
            if entry is None:
                # BEFORE_TASK never fired for this session. Most likely
                # the plugin was disabled at hook time, or the session
                # ended before any user prompt. Either way: not our
                # turn to emit.
                _log.debug(
                    "social-traces: session=%s untracked — skipping emit",
                    session_id,
                )
                return

            cfg = self._config_factory(profile_home)

            # ── Decision tree ───────────────────────────────────────
            if entry.trace_used is not None and entry.hit_count > 0:
                # Path A: a trace was injected mid-task. Run novelty
                # judge to decide whether the agent did something
                # beyond what the trace showed.
                if not cfg.novelty_judge.enabled:
                    # Rule (d) collapsed to rule (a): when a trace was
                    # used and the judge is off, we never emit.
                    _log.info(
                        "social-traces: session=%s used trace=%s — judge "
                        "disabled, silent emit",
                        session_id, entry.trace_used,
                    )
                    return

                verdict = await self._judge_novelty(
                    session_id=session_id,
                    trace_id=entry.trace_used,
                )
                if not verdict.is_novel:
                    _log.info(
                        "social-traces: session=%s used trace=%s — "
                        "judge says not novel, silent",
                        session_id, entry.trace_used,
                    )
                    return
                _log.info(
                    "social-traces: session=%s used trace=%s — judge "
                    "says novel (%s), continuing to distill",
                    session_id, entry.trace_used, verdict.reason,
                )
            else:
                # Path B: no trace used (BEFORE_TASK fired but inbox
                # had no match). Genuinely explored — emit
                # unconditionally per rule (d) binary path.
                _log.debug(
                    "social-traces: session=%s explored from scratch — "
                    "candidate emission",
                    session_id,
                )

            # ── Distill ─────────────────────────────────────────────
            try:
                submitter_hash = get_or_create_agent_id(profile_home)
            except Exception:  # noqa: BLE001
                _log.warning(
                    "social-traces: failed to resolve submitter_hash "
                    "for session=%s — skipping",
                    session_id,
                    exc_info=True,
                )
                return

            try:
                proposal = await distiller.distill_session(
                    session_id=session_id,
                    profile_home=profile_home,
                    submitter_hash=submitter_hash,
                )
            except Exception:  # noqa: BLE001
                _log.warning(
                    "social-traces: distiller raised for session=%s — skipping",
                    session_id,
                    exc_info=True,
                )
                return

            if proposal is None:
                # Phase 5 stub returns None unconditionally — that's
                # the expected path until Phase 7 lands. Logged at
                # DEBUG so production logs aren't noisy.
                _log.debug(
                    "social-traces: session=%s distiller returned None — "
                    "nothing to submit",
                    session_id,
                )
                return

            # ── Submit ──────────────────────────────────────────────
            try:
                client = self._client_factory(profile_home, cfg)
            except Exception:  # noqa: BLE001
                _log.warning(
                    "social-traces: client construction failed for "
                    "session=%s — skipping",
                    session_id,
                    exc_info=True,
                )
                return

            try:
                receipt = await client.submit(proposal)
            except Exception:  # noqa: BLE001
                _log.warning(
                    "social-traces: client.submit raised for session=%s "
                    "— Phase 9 will queue this in outbox",
                    session_id,
                    exc_info=True,
                )
                return

            if not receipt.accepted:
                _log.info(
                    "social-traces: session=%s submission rejected: %s "
                    "(Phase 9 will queue for retry)",
                    session_id, receipt.reason,
                )
                return

            _log.info(
                "social-traces: session=%s submitted (queue_id=%s)",
                session_id, receipt.queue_id,
            )
        except Exception:  # noqa: BLE001 — fire-and-forget must never raise
            _log.warning(
                "social-traces: session=%s pipeline raised: boom-suppressed",
                session_id,
                exc_info=True,
            )

    async def _judge_novelty(
        self, *, session_id: str, trace_id: str
    ) -> novelty_judge.NoveltyVerdict:
        """Wrap :func:`novelty_judge.judge_session_novelty` with a
        try/except envelope so a judge crash falls open to
        ``is_novel=False`` (the conservative default — silent emit
        rather than spam-emit on bad signal).

        Phase 6 will pass the actual session transcript + used trace
        body. For Phase 5 the stub returns False regardless of inputs,
        so the call signature is intentionally minimal here.
        """
        try:
            return await novelty_judge.judge_session_novelty(
                user_message="",
                transcript="",
                used_trace_intent="",
                used_trace_insight="",
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "social-traces: novelty_judge raised for session=%s — "
                "treating as not-novel (silent)",
                session_id,
                exc_info=True,
            )
            return novelty_judge.NoveltyVerdict(
                is_novel=False, reason="judge-raised",
            )


__all__ = ["TraceEmissionSubscriber"]
