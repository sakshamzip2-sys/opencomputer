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

import asyncio
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


# ─── Stage-1 heuristic gate ──────────────────────────────────────────


#: Minimum loop iterations for a session to be worth distilling.
#: ``turn_count == 1`` means "user asked, agent answered without
#: tools" — no procedure to share, skip.
_MIN_TURN_COUNT = 2

#: Minimum wall-clock duration (seconds). Filters out cancellations,
#: tool guard aborts, and other near-instant exits.
_MIN_DURATION_S = 3.0

#: Maximum number of distill pipelines that can be in-flight at once
#: per subscriber. Caps the cost-spike when many sessions end in rapid
#: succession (e.g. gateway burst). Each pipeline can fire up to four
#: Haiku calls (1 novelty + 3 distill), so 2 in flight = 8 max
#: concurrent provider calls — comfortable under typical Anthropic
#: rate limits while still letting a real workload progress.
_MAX_CONCURRENT_PIPELINES = 2


def is_session_worth_distilling(event: SessionEndEvent) -> bool:
    """Cheap heuristic gate — runs before any LLM cost.

    Returns ``False`` for trivial sessions: no tool turns, instant
    exits, cancelled mid-prompt, etc. The distiller's own filters
    (empty user message, sentinel-only output, schema validation)
    catch the next layer; this gate just avoids paying ~3 Haiku calls
    on a session that's obviously not worth sharing.

    A session that hit errors (``had_errors=True``) IS worth
    distilling — failure-mode traces are valuable per the HANDOVER
    "edge case" rule. Don't filter on outcome here.

    Tunable: thresholds live in module-level constants and are
    intentionally NOT in :class:`SocialTracesConfig` for now —
    they're heuristics, not policy. If real-world usage shows the
    cap is wrong we can promote them.
    """
    if event.turn_count < _MIN_TURN_COUNT:
        return False
    return event.duration_seconds >= _MIN_DURATION_S


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
        provider: Any | None = None,
        cost_guard: Any | None = None,
        sensitive_filter: Callable[[str], bool] | None = None,
        harness_version: str = "",
    ) -> None:
        self._bus = bus
        self._profile_home_factory = profile_home_factory
        self._client_factory = client_factory
        self._config_factory = config_factory
        # Phase 6: provider + cost_guard for the novelty judge + Phase 7
        # distiller. When either is None the judge degrades to
        # ``is_novel=False`` and the distiller returns ``None`` —
        # gateway production wiring (Phase 9) supplies real values
        # by resolving from OC's configured provider + the per-profile
        # default cost guard.
        self._provider = provider
        self._cost_guard = cost_guard
        # Phase 7: caller-supplied filter that flags whole bodies as
        # too sensitive to ship. Composes with the always-on PII /
        # secret regex sweeps inside :mod:`redactor`.
        self._sensitive_filter = sensitive_filter
        self._harness_version = harness_version
        self._subscription: Any = None
        # Backpressure: bounds in-flight pipelines so a session burst
        # doesn't fan out into an unbounded swarm of LLM calls. The
        # semaphore is created lazily on first acquire because
        # asyncio.Semaphore() in __init__ would bind to whatever loop
        # happens to be running at construction time (often "no loop"
        # in tests), and the bus may dispatch on a different one.
        self._pipeline_sem: asyncio.Semaphore | None = None

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

    def _get_pipeline_semaphore(self) -> asyncio.Semaphore:
        """Lazily construct the in-flight semaphore on the running loop.

        Created on first use (not in ``__init__``) so the semaphore
        binds to the loop that actually dispatches events, not the
        loop that happened to be running at subscriber construction
        time.
        """
        if self._pipeline_sem is None:
            self._pipeline_sem = asyncio.Semaphore(_MAX_CONCURRENT_PIPELINES)
        return self._pipeline_sem

    async def _run_pipeline(
        self, event: SessionEndEvent, profile_home: Path
    ) -> None:
        """Run the full Phase 5 decision tree. Never raises.

        Each stage is wrapped — a failure logs at WARNING and
        short-circuits. The bridge entry is ALWAYS popped at the end
        so a daemon doesn't accumulate state for sessions whose
        pipeline crashed mid-flight.

        Bounded by :attr:`_pipeline_sem` so a burst of session_end
        events doesn't fan out into unbounded concurrent LLM calls.
        Excess pipelines wait at the semaphore — they still execute,
        just serialized.
        """
        session_id = event.session_id
        sem = self._get_pipeline_semaphore()
        async with sem:
            await self._run_pipeline_body(event, profile_home, session_id)

    async def _run_pipeline_body(
        self, event: SessionEndEvent, profile_home: Path, session_id: str
    ) -> None:
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

            # ── Stage-1 heuristic gate (Phase 9 production wiring) ─
            # Skip trivial sessions before paying any LLM cost. The
            # distiller's own filters catch the next layer; this gate
            # just avoids 3 Haiku calls on sessions that are
            # obviously not worth sharing (cancellations, one-shot
            # chat with no tools, instant exits).
            if not is_session_worth_distilling(event):
                _log.debug(
                    "social-traces: session=%s — too trivial "
                    "(turns=%d duration=%.1fs), skipping emit",
                    session_id, event.turn_count, event.duration_seconds,
                )
                return

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
                    entry=entry,
                    profile_home=profile_home,
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
            # Phase 6 / Stage 2 — when an OpenHub-issued submitter_hash
            # is configured (env or config.yaml), use IT as the card's
            # ``meta.submitter_hash``. The OH submit endpoint enforces
            # ``X-Submitter-Hash`` (from HMAC creds) == body's
            # submitter_hash so it can attribute submissions correctly.
            # Without this override, the local ``get_or_create_agent_id``
            # generates a different per-profile UUID and the server
            # rejects every signed submission with "submitter_hash in
            # payload does not match X-Submitter-Hash header".
            try:
                submitter_hash = cfg.submitter_hash or get_or_create_agent_id(
                    profile_home
                )
            except Exception:  # noqa: BLE001
                _log.warning(
                    "social-traces: failed to resolve submitter_hash "
                    "for session=%s — skipping",
                    session_id,
                    exc_info=True,
                )
                return

            try:
                # SessionEndEvent.had_errors drives outcome — by the
                # time messages land in SessionDB the per-tool
                # ``is_error`` flag is gone (it's on ToolResult, not
                # Message), so the event itself is the source of truth.
                outcome = "failed" if event.had_errors else "success"
                proposal = await distiller.distill_session(
                    session_id=session_id,
                    profile_home=profile_home,
                    submitter_hash=submitter_hash,
                    provider=self._provider,
                    cost_guard=self._cost_guard,
                    redact_paths_layer=cfg.privacy.redact_paths,
                    redact_hostnames_layer=cfg.privacy.redact_hostnames,
                    sensitive_filter=self._sensitive_filter,
                    harness_version=self._harness_version,
                    outcome=outcome,
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
        self,
        *,
        session_id: str,
        entry: Any,  # session_state._SessionEntry
        profile_home: Path,
    ) -> novelty_judge.NoveltyVerdict:
        """Wrap :func:`novelty_judge.judge_session_novelty` with a
        try/except envelope so a judge crash falls open to
        ``is_novel=False`` (the conservative default — silent emit
        rather than spam-emit on bad signal).

        Phase 6 reads:

        * ``entry.trace_card`` — the TraceCard the prefetch hook
          injected, stored in the bridge so we don't re-query the
          network.
        * The session's user message + transcript from
          :class:`opencomputer.agent.state.SessionDB` (the same DB
          the agent loop persists into).

        If the SessionDB read fails we still call the judge with empty
        strings — the judge will return ``is_novel=False`` (it can't
        tell anything from no context), which is the conservative
        default.
        """
        used_intent = ""
        used_insight = ""
        if entry is not None and entry.trace_card is not None:
            card = entry.trace_card
            used_intent = getattr(card, "intent", "") or ""
            used_insight = getattr(card, "distilled_insight", "") or ""

        user_message, transcript = self._read_session_for_judge(
            session_id=session_id, profile_home=profile_home,
        )

        try:
            return await novelty_judge.judge_session_novelty(
                user_message=user_message,
                transcript=transcript,
                used_trace_intent=used_intent,
                used_trace_insight=used_insight,
                provider=self._provider,
                cost_guard=self._cost_guard,
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

    def _read_session_for_judge(
        self, *, session_id: str, profile_home: Path
    ) -> tuple[str, str]:
        """Read the session's user message + transcript for the judge.

        Returns ``("", "")`` on any failure — the judge handles empty
        inputs by returning ``is_novel=False``, which is correct.

        We use SessionDB directly rather than expecting the caller to
        thread the messages in. Avoids leaking opencomputer.* import
        surface to the public API of this method.
        """
        try:
            from opencomputer.agent.state import SessionDB

            db = SessionDB(profile_home / "sessions.db")
            messages = db.get_messages(session_id)
        except Exception:  # noqa: BLE001
            _log.debug(
                "social-traces: session %s — couldn't read SessionDB; "
                "judge will run with empty transcript",
                session_id,
                exc_info=True,
            )
            return "", ""

        # First user message = the task prompt. Everything else gets
        # joined into the transcript with role labels.
        user_message = ""
        transcript_lines: list[str] = []
        for msg in messages:
            role = getattr(msg, "role", "") or ""
            content = getattr(msg, "content", "") or ""
            if role == "user" and not user_message:
                user_message = content
                continue
            # Skip system-reminder injections from our own pre-task
            # hook — they're not the agent's work.
            if role == "user" and "<system-reminder>" in content:
                continue
            transcript_lines.append(f"[{role}] {content}")
        transcript = "\n".join(transcript_lines)
        return user_message, transcript


__all__ = ["TraceEmissionSubscriber"]
