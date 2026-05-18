"""
Gateway dispatch — route inbound MessageEvents to the agent loop.

This is the glue between channel adapters (Telegram, Discord, etc.)
and the AgentLoop. Each adapter calls `Dispatch.handle_message(event)`;
we map chat_id → session_id and invoke the loop.

Task I.9 — per-request plugin scope. When constructed with a
``plugin_api``, each ``handle_message`` wraps ``run_conversation`` in
``plugin_api.in_request(ctx)`` so plugins can query their
``request_context`` (auth gating, rate limiting, activation-context
queries). Mirrors OpenClaw's server-plugins request binding at
``sources/openclaw/src/gateway/server-plugins.ts:47-64, 107-144``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import os
import time
import uuid
from datetime import UTC
from typing import TYPE_CHECKING, Any

from opencomputer.agent.loop import AgentLoop
from plugin_sdk.core import MessageEvent, ProcessingOutcome
from plugin_sdk.runtime_context import (
    DEFAULT_RUNTIME_CONTEXT,
    RequestContext,
    RuntimeContext,
)

if TYPE_CHECKING:
    from opencomputer.channels.allowlist import AllowlistGate
    from opencomputer.gateway.agent_router import AgentRouter
    from opencomputer.gateway.binding_resolver import BindingResolver
    from opencomputer.gateway.channel_directory import ChannelDirectory
    from opencomputer.gateway.reset_policy import ResetPolicyChecker
    from opencomputer.plugins.loader import PluginAPI
    from plugin_sdk.consent import CapabilityClaim

logger = logging.getLogger("opencomputer.gateway.dispatch")


# ─── P0-4: outcome-aware learning telemetry hook ─────────────────────
#
# Phase 0 records implicit per-turn signals into ``turn_outcomes`` after
# every completed turn. Fire-and-forget: never blocks the user reply
# path; swallows DB errors so a telemetry failure can't break dispatch.
#
# A module-level set keeps strong references to in-flight tasks so the
# event loop's GC doesn't collect them mid-write (standard asyncio
# fire-and-forget pattern).

_pending_outcome_writes: set[asyncio.Task] = set()


def _compute_turn_index(db, session_id: str) -> int:
    """Return the next turn_index for this session.

    Cheap query against the new ``idx_turn_outcomes_session`` index.
    Idempotent — re-running just gives the same +1, which the recorder
    handles cleanly (it doesn't enforce uniqueness on (session, turn);
    the rare race produces two rows the engine dedups by created_at).
    """
    with db._connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(turn_index), -1) + 1 FROM turn_outcomes "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return int(row[0])


def _read_compactions_count(db, session_id: str) -> int:
    """Return ``sessions.compactions_count`` for ``session_id`` (0 on miss).

    Used by parity mechanism #10 (``compaction_long_session``) — the
    dispatcher snapshots this before/after ``run_conversation`` and a
    rise means CompactionEngine ran this turn. Best-effort: any failure
    (missing row, no DB, SQL error) yields 0 so a delta of 0 reads as
    "no compaction" rather than wedging telemetry.
    """
    try:
        row = db.get_session(session_id)
    except Exception:  # noqa: BLE001 — telemetry read must never raise
        return 0
    if not row:
        return 0
    try:
        return int(row.get("compactions_count") or 0)
    except (TypeError, ValueError):
        return 0


def _build_end_of_turn_signals(
    db,
    *,
    session_id: str,
    turn_index: int,
    start_ts: float,
    end_ts: float,
):
    """Compose a TurnSignals at end-of-turn from data already captured.

    Deliberately leaves ``affirmation_present``, ``correction_present``,
    and ``reply_latency_s`` at their defaults (False / False / None).
    Those columns are populated by P0-4b's start-of-next-turn back-fill
    once we know the next user message — at end-of-turn we don't have it
    yet.
    """
    from opencomputer.agent.turn_outcome_recorder import TurnSignals

    counts = db.query_tool_usage_in_window(
        session_id=session_id, start_ts=start_ts, end_ts=end_ts,
    )
    vibes = db.query_recent_vibes(
        session_id=session_id, before_ts=end_ts, limit=2,
    )
    vibe_after = vibes[0] if len(vibes) >= 1 else None
    vibe_before = vibes[1] if len(vibes) >= 2 else None

    return TurnSignals(
        session_id=session_id,
        turn_index=turn_index,
        tool_call_count=counts["call_count"],
        tool_success_count=counts["success_count"],
        tool_error_count=counts["error_count"],
        tool_blocked_count=counts["blocked_count"],
        vibe_before=vibe_before,
        vibe_after=vibe_after,
        duration_s=max(0.0, end_ts - start_ts),
    )


async def _record_turn_outcome_async(db, sig) -> None:
    """Fire-and-forget telemetry write. Never propagates exceptions.

    P0-6: after the DB write, publishes a ``TurnCompletedEvent`` on the
    typed event bus so any subscriber (Honcho extension, analytics
    dashboards, custom reactors) can observe per-turn outcomes without
    dispatch importing or knowing about them. Decouples Phase 0
    capture from any downstream provider — preserves the SDK boundary
    (plugins never import from opencomputer/*; they subscribe to the
    bus and let the bus deliver).
    """
    try:
        from opencomputer.agent.turn_outcome_recorder import TurnOutcomeRecorder
        TurnOutcomeRecorder(db).record(sig)
    except Exception as e:  # noqa: BLE001 — telemetry must never block
        logger.warning("outcome recording failed: %s", e)
        return

    # Publish the event AFTER the DB write succeeded — subscribers
    # see only durable outcomes.
    try:
        from opencomputer.ingestion.bus import get_default_bus
        from plugin_sdk.ingestion import TurnCompletedEvent

        evt = TurnCompletedEvent(
            session_id=sig.session_id,
            source="gateway.dispatch",
            turn_index=sig.turn_index,
            signals={
                "tool_call_count": sig.tool_call_count,
                "tool_success_count": sig.tool_success_count,
                "tool_error_count": sig.tool_error_count,
                "tool_blocked_count": sig.tool_blocked_count,
                "self_cancel_count": sig.self_cancel_count,
                "retry_count": sig.retry_count,
                "vibe_before": sig.vibe_before,
                "vibe_after": sig.vibe_after,
                "reply_latency_s": sig.reply_latency_s,
                "affirmation_present": sig.affirmation_present,
                "correction_present": sig.correction_present,
                "conversation_abandoned": sig.conversation_abandoned,
                "duration_s": sig.duration_s,
            },
        )
        bus = get_default_bus()
        if bus is not None:
            await bus.apublish(evt)
    except Exception as e:  # noqa: BLE001 — bus failures must never block
        logger.warning("turn_completed event publish failed: %s", e)


async def _backfill_prior_turn_async(
    db,
    *,
    session_id: str,
    user_text: str,
    now_ts: float,
) -> None:
    """P0-4b: at start-of-next-turn, fill in the prior turn_outcomes
    row's affirmation/correction/latency from the new user message.

    Targets the most recent row in this session whose
    ``reply_latency_s`` is still NULL (i.e., not yet back-filled). If
    no such row exists (first turn of session, or end-of-turn writer
    hasn't committed yet), this is a clean no-op.

    Fire-and-forget; swallows all exceptions so a telemetry failure
    can never block the user reply.
    """
    try:
        from opencomputer.agent.affirmation_lexicon import (
            detect_affirmation,
            detect_correction,
        )

        with db._connect() as conn:
            row = conn.execute(
                "SELECT id, created_at FROM turn_outcomes "
                "WHERE session_id = ? AND reply_latency_s IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if not row:
                return  # first message of session, or race with end-of-turn write

            latency = max(0.0, now_ts - float(row["created_at"]))
            affirm = int(detect_affirmation(user_text or ""))
            correct = int(detect_correction(user_text or ""))

            conn.execute(
                "UPDATE turn_outcomes "
                "SET affirmation_present = ?, correction_present = ?, "
                "    reply_latency_s = ? "
                "WHERE id = ?",
                (affirm, correct, latency, row["id"]),
            )
    except Exception as e:  # noqa: BLE001 — telemetry guard
        logger.warning("turn_outcomes backfill failed: %s", e)


def _format_user_facing_error(exc: Exception) -> str:
    """Render an exception from the agent loop as a one-liner the user
    can read on a chat surface.

    The full traceback is logged via ``logger.exception`` at the call
    site — this only shapes what the *user* sees on Telegram / Discord
    / etc. Categorisation flows through
    :func:`opencomputer.agent.error_classifier.classify` so all retry/
    rotation/render paths agree on what counts as which kind of error.

    Pure function (no Dispatch state) so unit tests + downstream
    error-presentation code can call it directly.
    """
    from opencomputer.agent.error_classifier import ErrorCategory, classify

    name = type(exc).__name__
    category = classify(exc)
    status = getattr(exc, "status_code", None)

    if category is ErrorCategory.NETWORK or category is ErrorCategory.TIMEOUT:
        return ("Can't reach the model server right now (network issue). "
                "Try again in a moment.")

    if category is ErrorCategory.RATE_LIMITED:
        return ("Rate-limited by the model provider. "
                "Try again in a few seconds.")

    if category is ErrorCategory.AUTH:
        return ("Authentication failed — your API key may be invalid or "
                "your provider proxy is misconfigured.")

    if category is ErrorCategory.QUOTA:
        return ("Plan/quota exceeded — top up the provider account "
                "or switch to a different key.")

    if category is ErrorCategory.SERVER:
        if isinstance(status, int):
            return (f"The model service returned an error ({status}). "
                    "This is usually transient — try again in a moment.")
        return ("The model service returned an error. "
                "This is usually transient — try again in a moment.")

    if category is ErrorCategory.BAD_REQUEST:
        return ("The request was rejected as invalid — this is usually a "
                "bug in the agent or an unsupported model feature. "
                "Check the gateway logs.")

    # UNKNOWN / unmapped — keep the class name so logs can be grepped,
    # but don't dump the raw exception args (those often contain the
    # offending prompt or an SDK-internal kwarg dump).
    return (f"Sorry, something went wrong ({name}). "
            "Check the gateway logs for details.")


def session_id_for(
    platform: str, chat_id: str, thread_hint: str | None = None,
) -> str:
    """Derive the stable per-chat session id used by :class:`Dispatch`.

    Public helper extracted from :meth:`Dispatch._session_id_for` so
    channel adapters can compute the same id without a live ``Dispatch``
    handle — needed by P-2 ``/steer`` interception in the Telegram
    adapter (the adapter's per-chat decisions key on the same id the
    dispatcher would have generated for the same inbound event).

    Stable across processes: ``sha256(platform:chat_id[:thread_hint])``
    truncated to 32 hex chars. Two adapters seeing the same
    ``(platform, chat_id, thread_hint)`` triple always produce identical
    session ids — so a nudge submitted via wire / CLI lands in the same
    per-session bucket the agent loop will later consume.

    Item 21 — ``thread_hint``: when present, derives a SEPARATE session
    from the same chat. Use cases:

    - Cron output to Telegram tags itself with ``thread_hint="cron:<job>"``
      so the morning briefing doesn't pollute the ad-hoc Q&A thread.
    - Future ``messages_send`` callers can pass a hint when explicitly
      starting a new topic from a non-conversational source.

    None / empty hint reproduces the legacy behavior (same chat → same
    session forever) so existing tests + callers see no change.
    """
    key = (
        f"{platform}:{chat_id}:{thread_hint}" if thread_hint
        else f"{platform}:{chat_id}"
    )
    h = hashlib.sha256(key.encode())
    return h.hexdigest()[:32]


class Dispatch:
    """Map channel messages to agent-loop runs, keeping per-chat sessions separate."""

    def __init__(
        self,
        loop: AgentLoop | None = None,
        plugin_api: PluginAPI | None = None,
        channel_directory: ChannelDirectory | None = None,
        config: dict[str, Any] | None = None,
        *,
        router: AgentRouter | None = None,
        resolver: BindingResolver | None = None,
        allowlist_gate: AllowlistGate | None = None,
        reset_policy: ResetPolicyChecker | None = None,
        last_seen_path: Any = None,  # Path to <profile>/gateway/last_seen.json
    ) -> None:
        # Phase 2 collect-mode leader registry. Only ONE arrival per
        # session in collect mode runs the agent; subsequent arrivals
        # buffer + return early. The leader's drain wait coalesces them.
        # Map: session_id → asyncio.Lock acting as the leader claim.
        self._collect_leaders: dict[str, bool] = {}
        # /background completion-notifier plumbing — main-thread asyncio
        # loop is captured by ``bind_main_loop`` at gateway start so the
        # background-worker thread's notifier can schedule adapter.send
        # via ``run_coroutine_threadsafe``. ``None`` until bound.
        self._main_loop: asyncio.AbstractEventLoop | None = None

        # 2026-05-08 — Hermes Doc-2 ``session:start`` tracking. Hermes'
        # spec: "Fires on brand-new sessions only (not continuations)."
        # We track session ids we've already routed through dispatch in
        # this process so subsequent messages for the same id fire
        # ``session:end`` (per-turn) only, not another ``session:start``.
        # Memory is per-process; a gateway restart resets it (and
        # legitimately re-fires session:start on resumed sessions —
        # acceptable semantics).
        self._known_sessions: set[str] = set()

        # A7 (gateway-vs-CLI parity) — one-line session banner. Shown
        # once per session (process-lifetime latch) prepended to the
        # first reply, so the user can see which profile / model / cwd
        # answered. Suppressed by display.gateway_banner.enabled=false.
        self._banner_shown: set[str] = set()

        # Phase 2 multi-routing: accept either ``loop=`` (legacy single
        # loop) or ``router=`` (per-profile cache). Exactly one of the
        # two must be set.
        if router is not None and loop is not None:
            raise ValueError("Dispatch: pass either loop or router, not both")
        if router is None and loop is None:
            raise ValueError("Dispatch: pass either loop or router")

        if router is None:
            # Legacy single-loop path — wrap into a one-entry router
            # seeded with the loop as ``"default"`` so the rest of the
            # code path is uniform (no per-call branching). The lambda
            # is a no-op fallback that should never fire because the
            # ``"default"`` slot is pre-populated below; if a future
            # caller asks for a non-default profile through this
            # legacy router it'll get the same loop, which preserves
            # current behaviour.
            from opencomputer.agent.config import _home as _resolve_home
            from opencomputer.gateway.agent_router import AgentRouter

            assert loop is not None  # for mypy after the guard above
            router = AgentRouter(
                loop_factory=lambda _pid, _home: loop,
                # Minor fix: return the real OPENCOMPUTER_HOME default
                # rather than Path() (CWD), which was wrong for set_profile
                # calls inside run_conversation.
                profile_home_resolver=lambda _pid: _resolve_home(),
            )
            router._loops["default"] = loop  # pre-populate
        self._router = router
        # Phase 3 Task 3.3: per-event profile resolver. ``None`` falls
        # back to the legacy ``"default"`` routing behaviour, preserving
        # backwards compat for tests / callers that don't load a
        # ``bindings.yaml`` file.
        self._resolver: BindingResolver | None = resolver
        # ``self.loop`` preserves the legacy attribute access path.
        # When the caller passed ``router=`` directly we expose
        # whatever the seeded "default" loop is (if any) so existing
        # code that reads ``dispatch.loop`` still works for the common
        # single-profile case. Multi-profile callers reading this
        # attribute should migrate to ``router.get_or_load(profile_id)``.
        self.loop = loop if loop is not None else router._loops.get("default")
        # Phase 2 (S1 from 2026-05-06 brief) — inbound queue manager.
        # Replaces the historical per-(profile,session) asyncio.Lock dict.
        # Default mode = "followup" (preserves legacy serialize-and-wait
        # behaviour); slash command can flip per-session to "interrupt".
        from opencomputer.gateway.queue_manager import (
            QueueManager,
            set_active_manager,
        )

        self._queue_manager: QueueManager = QueueManager()
        # Register so /queue-mode slash command can reach this manager.
        # Last-Dispatch-wins is fine: tests construct multiple Dispatches
        # and the most recent one is the relevant one for slash dispatch.
        set_active_manager(self._queue_manager)
        # Backwards compat: tests + callers that read ``dispatch._locks``
        # see an empty dict; new code paths use ``self._queue_manager``.
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        # Kanban-Goals v2 (2026-05-08) — set of session_ids currently
        # inside a ``run_conversation`` call. Inspected by the
        # /goal-set mid-run race-guard (``_goal_midrun_check``) so we
        # can refuse a new goal when the agent is in flight rather than
        # racing the current continuation prompt.
        self._active_runs: set[str] = set()
        # M3 #6/#8 — sessions that have already shown the one-line
        # routing badge. The badge ("↪ routed: …") surfaces the
        # otherwise-invisible binding/routing/profile-rebind decision;
        # shown once per session so it doesn't clutter every reply.
        self._routing_badge_shown: set[str] = set()
        # M3 #5 — per-session count of consent approval prompts sent.
        # Bumped by _send_approval_prompt; the dispatcher snapshots it
        # before/after run_conversation so mechanism #5
        # (no_interactive_consent) fires only on turns that actually
        # paid the async-consent round-trip, not structurally on every
        # turn (the gateway DOES have working interactive consent).
        self._consent_prompt_counts: dict[str, int] = {}
        # Adapter reference (set by Gateway) so we can send typing indicators
        self._adapters_by_platform: dict = {}
        # Task I.9: the shared PluginAPI whose ``in_request`` we wrap
        # each dispatch with. ``None`` preserves backwards compat —
        # existing CLI test paths constructing Dispatch without a
        # plugin_api keep working.
        self._plugin_api: PluginAPI | None = plugin_api
        # Hermes channel-port (PR 2 Task 2.6 + amendment §A.1): photo-
        # burst merging. When multiple pure-attachment events arrive
        # within the burst window for the same session, we collapse
        # them into ONE agent run with merged attachments. A text
        # event arriving mid-burst CANCELS the pending dispatch and
        # absorbs the photo's attachments into the text event (the
        # text is the user's "go" signal).
        cfg = config or {}
        # Wave 5 T4 — keep the raw display-config dict so the runtime
        # footer + busy_ack helpers can be re-resolved per-platform.
        self._display_cfg: dict[str, Any] = cfg
        self._burst_window_seconds: float = float(
            cfg.get("photo_burst_window", 0.8)
        )
        # 2026-05-08 — gate the Hermes-style 👀/✅ lifecycle reactions
        # behind a config flag, default OFF. Default behavior in
        # ``BaseChannelAdapter`` posts a 👀 reaction on every inbound
        # message (and ✅ on completion); Telegram clients render that
        # inline next to the user's message and it reads like the bot
        # is replying with an emoji. Saksham's standing emoji-free
        # preference (memory: ``user_oc_owns_all_channels.md``) means
        # this MUST default off for him; users who explicitly want the
        # indicator can opt in via ``gateway.lifecycle_reactions: true``.
        self._lifecycle_reactions: bool = bool(
            cfg.get("lifecycle_reactions", False)
        )
        self._burst_pending: dict[str, MessageEvent] = {}
        self._burst_tasks: dict[str, asyncio.Task[None]] = {}
        # Joiners (subsequent pure-attachment events) await the same
        # future so every caller sees the same final assistant text.
        self._burst_futures: dict[str, asyncio.Future[str | None]] = {}
        # Task II.3: channel directory cache. Records every inbound
        # MessageEvent's (platform, chat_id, display_name) so future
        # send-message tools can resolve friendly names instead of raw
        # numeric ids. ``None`` is fine — record() becomes a no-op.
        self._channel_directory: ChannelDirectory | None = channel_directory
        # Round 2a P-5 — session ↔ (adapter, chat_id) binding map.
        # Populated on every inbound ``handle_message`` so a later
        # consent prompt can find the right channel surface to ask the
        # user on. Capped implicitly: when a session goes idle and a
        # new one starts, the entry is overwritten on the next inbound
        # message. We never grow without bound because session ids are
        # deterministic per (platform, chat_id).
        self._session_channels: dict[str, tuple[Any, str]] = {}
        # Critical fix (Task 2.5 review): session ↔ profile_id mapping so
        # _send_approval_prompt and _handle_approval_click can resolve the
        # per-profile gate via the router rather than self.loop (which is
        # None when Dispatch is constructed with router= instead of loop=).
        self._session_profiles: dict[str, str] = {}
        # Token registry — opaque request tokens minted in the prompt
        # handler so we don't leak session_id / capability_id onto the
        # Telegram callback wire. Maps token → (session_id, cap_id).
        self._approval_tokens: dict[str, tuple[str, str]] = {}
        # Wire ourselves up as the gate's channel-side prompt handler if
        # the loop has a gate attached. Idempotent: re-setting later is
        # safe, and tests can construct Dispatch without a gate.
        gate = getattr(loop, "_consent_gate", None)
        if gate is not None and hasattr(gate, "set_prompt_handler"):
            gate.set_prompt_handler(self._send_approval_prompt)

        # ── PR-1 (Task 1.6): allowlist + reset policy + last-seen ──────
        # All optional — Dispatch constructed without them preserves the
        # historic permissive flow (existing tests untouched). Gateway
        # wires these in production to gate unknown users + reset stale
        # sessions.
        self._allowlist_gate: AllowlistGate | None = allowlist_gate
        self._reset_policy: ResetPolicyChecker | None = reset_policy
        self._last_seen_path = last_seen_path
        # ``(platform, chat_id) → unix_seconds`` and ``(platform, chat_id) →
        # reset_token`` — both tracked here, persisted to last_seen.json.
        self._chat_last_seen: dict[tuple[str, str], float] = {}
        self._chat_reset_tokens: dict[tuple[str, str], str] = {}
        self._last_seen_dirty_count = 0
        self._last_seen_persist_every = 10
        self._load_last_seen()

        # A2 (gateway-vs-CLI parity) — per-chat plan-mode store. Persisted
        # alongside last_seen.json under <profile>/gateway/. The /plan
        # slash command toggles it; __do_dispatch_inner injects plan_mode
        # onto the runtime each turn. Registered process-wide so /plan
        # (which only sees a RuntimeContext) reaches this same instance.
        from pathlib import Path as _Path

        from opencomputer.gateway.runtime_state import (
            GatewayRuntimeState,
            set_active_runtime_state,
        )

        _rs_path = None
        if last_seen_path is not None:
            try:
                _rs_path = _Path(last_seen_path).parent / "runtime_state.json"
            except Exception:  # noqa: BLE001 — fall back to in-memory only
                _rs_path = None
        self._runtime_state = GatewayRuntimeState(path=_rs_path)
        set_active_runtime_state(self._runtime_state)
        # PR-1 follow-up — drain coordination. ``_drain_active`` is set by
        # ``Gateway.serve_forever`` when ``oc gateway restart`` writes the
        # drain flag; new arrivals while drain is active return None
        # immediately without entering the agent loop. ``_inflight_count``
        # tracks how many handle_message calls are mid-flight so the
        # serve loop can wait until 0 before exiting.
        self._drain_active = False
        self._inflight_count = 0

    def register_adapter(self, platform: str, adapter) -> None:
        self._adapters_by_platform[platform] = adapter
        # Round 2a P-5 — if the adapter exposes the approval-button
        # surface, route its callbacks through us so we can translate
        # opaque tokens back into ``ConsentGate.resolve_pending`` calls.
        if hasattr(adapter, "set_approval_callback"):
            adapter.set_approval_callback(self._handle_approval_click)

    def _session_id_for(self, event: MessageEvent) -> str:
        """Stable session id: hash(platform + chat_id[, thread_hint][, reset_token]).

        ``thread_hint`` (Item 21) comes from ``event.metadata["thread_hint"]``
        if set, letting cron / non-conversational paths route output to
        a separate session within the same chat. Default behaviour
        (no hint) keeps existing chats on a single session forever.

        PR-1 (Task 1.6) — when a reset policy fires for ``(platform, chat_id)``,
        the dispatcher writes a ``reset_token`` (e.g., ``daily-2026-05-08``)
        into ``self._chat_reset_tokens``. The token is OR'd into the hash
        salt so the next session_id derivation lands in a fresh session
        without mutating the channel id or thread hint.
        """
        thread_hint: str | None = None
        if event.metadata:
            raw = event.metadata.get("thread_hint")
            if isinstance(raw, str) and raw.strip():
                thread_hint = raw.strip()
        platform_value = event.platform.value if event.platform else ""
        # Defensive: callers that bypass __init__ (e.g.,
        # Dispatch.__new__(Dispatch) in tests) won't have _chat_reset_tokens.
        # The reset path is opt-in; falling back to no token preserves the
        # legacy behavior for those callers.
        reset_tokens = getattr(self, "_chat_reset_tokens", {})
        token_key = (platform_value, event.chat_id)
        reset_token = reset_tokens.get(token_key)
        if reset_token:
            # Compose with thread_hint so explicit cron threads still
            # partition cleanly inside the reset boundary.
            thread_hint = (
                f"{thread_hint}|reset:{reset_token}"
                if thread_hint
                else f"reset:{reset_token}"
            )
        return session_id_for(platform_value, event.chat_id, thread_hint)

    # ── PR-1 (Task 1.6) — last-seen persistence + pairing reply helpers ──

    def _load_last_seen(self) -> None:
        """Restore ``_chat_last_seen`` + ``_chat_reset_tokens`` from disk."""
        if self._last_seen_path is None:
            return
        try:
            from pathlib import Path

            path = Path(self._last_seen_path)
            if not path.exists():
                return
            import json as _json

            data = _json.loads(path.read_text(encoding="utf-8"))
            for key, ts in (data.get("last_seen") or {}).items():
                if "|" in key:
                    plat, chat = key.split("|", 1)
                    self._chat_last_seen[(plat, chat)] = float(ts)
            for key, tok in (data.get("reset_tokens") or {}).items():
                if "|" in key:
                    plat, chat = key.split("|", 1)
                    self._chat_reset_tokens[(plat, chat)] = str(tok)
        except (OSError, ValueError) as exc:
            logger.warning(
                "dispatch: last_seen.json unreadable (%s) — starting fresh", exc
            )

    def _persist_last_seen(self, force: bool = False) -> None:
        """Write ``_chat_last_seen`` + ``_chat_reset_tokens`` to disk.

        Throttled to every Nth write (default 10) so the dispatch hot-path
        stays cheap. ``force=True`` ignores throttle (used on shutdown).
        """
        if self._last_seen_path is None:
            return
        self._last_seen_dirty_count += 1
        if not force and self._last_seen_dirty_count < self._last_seen_persist_every:
            return
        self._last_seen_dirty_count = 0
        try:
            import json as _json
            import tempfile
            from pathlib import Path

            path = Path(self._last_seen_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "last_seen": {f"{p}|{c}": ts for (p, c), ts in self._chat_last_seen.items()},
                "reset_tokens": {f"{p}|{c}": tok for (p, c), tok in self._chat_reset_tokens.items()},
            }
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    _json.dump(payload, f)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.warning("dispatch: last_seen persist failed: %s", exc)

    def _format_pairing_reply(self, platform: str, code: str | None) -> str | None:
        """Format the bot's reply for an unknown user.

        Returns ``None`` when the gate suppressed the code (rate-limit /
        lockout) — the dispatcher then sends nothing.
        """
        if not code:
            return None
        bot_username = os.environ.get("TELEGRAM_BOT_USERNAME") or ""
        deep = ""
        if platform == "telegram" and bot_username:
            deep = (
                f"\nOr click: https://t.me/{bot_username}?start=approve_{code}"
            )
        return (
            f"Pairing code: `{code}` (expires in 60 minutes)\n"
            f"Ask the OpenComputer admin to run:\n"
            f"`oc gateway pairing approve {platform} {code}`"
            f"{deep}"
        )

    async def handle_message(self, event: MessageEvent) -> str | None:
        """
        Handle one inbound message. Runs the agent loop and returns the
        final assistant text for the adapter to send back.

        PRESERVES the ``str | None`` return contract — 7 adapters
        (slack/mattermost/email/signal/sms/imessage/webhook) await this
        return and pass it to ``self.send(chat_id, response)``.

        Hermes channel-port (PR 2 Task 2.6 + amendment §A.1) adds
        photo-burst merging. Four cases:

        1. Pure-attachment event arriving while a burst is pending →
           merge attachments into the pending event and **join** its
           future (every joiner gets the same answer).
        2. Text event arriving while a burst is pending → CANCEL the
           pending dispatch, absorb the photo's attachments into the
           text event, dispatch immediately. Text is the user's "go".
        3. Pure-attachment event with no pending burst → start the
           timer; dispatch fires after ``_burst_window_seconds``.
        4. Plain text event → direct dispatch.

        Task I.9 — when a ``plugin_api`` is bound, each dispatch wraps
        ``run_conversation`` in ``plugin_api.in_request(ctx)``.

        Task II.3 — records the inbound channel into the directory
        cache so future send-message tools can resolve friendly names.
        """
        # Task II.3: cache the inbound channel. Best-effort; don't let a
        # write failure (full disk, permissions) break the reply path.
        if self._channel_directory is not None:
            try:
                display_name = None
                if event.metadata:
                    raw = event.metadata.get("display_name")
                    if isinstance(raw, str) and raw.strip():
                        display_name = raw
                self._channel_directory.record(
                    platform=event.platform.value if event.platform else "",
                    chat_id=event.chat_id,
                    display_name=display_name,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "channel_directory record failed for %s:%s — %s",
                    getattr(event.platform, "value", "?"),
                    event.chat_id,
                    e,
                )

        # Empty event — preserve existing skip-blank behaviour.
        text_present = bool(event.text and event.text.strip())
        attach_present = bool(event.attachments)
        if not text_present and not attach_present:
            return None

        # PR-1 follow-up — drain mode: refuse new arrivals while
        # ``oc gateway restart`` is waiting for in-flight runs to drain.
        # Existing in-flight runs continue (they're tracked in
        # ``_inflight_count``); the gateway's serve_forever waits for
        # the count to reach 0 before exiting.
        if getattr(self, "_drain_active", False):
            logger.info(
                "dispatch: drain active — skipping new arrival on %s:%s",
                event.platform.value if event.platform else "?",
                event.chat_id,
            )
            return None

        # ── PR-1 (Task 1.6) — AllowlistGate check ───────────────────────
        # Default-OFF: when ``self._allowlist_gate`` is None (unit-test
        # constructions, legacy paths) this whole block is a no-op.
        if self._allowlist_gate is not None:
            platform_value = event.platform.value if event.platform else ""
            user_id = ""
            user_name = ""
            if event.metadata:
                raw_uid = event.metadata.get("user_id") or event.metadata.get(
                    "from_user_id"
                )
                if isinstance(raw_uid, str | int):
                    user_id = str(raw_uid)
                raw_name = event.metadata.get("user_name") or event.metadata.get(
                    "from_user_name"
                )
                if isinstance(raw_name, str):
                    user_name = raw_name
            # Fall back to chat_id when no per-user id is available — for
            # 1:1 DMs the chat_id IS the user_id on most platforms.
            if not user_id:
                user_id = event.chat_id
            decision = self._allowlist_gate.check(
                platform_value, user_id, user_name=user_name
            )
            if not decision.allowed:
                reply = self._format_pairing_reply(
                    platform_value, decision.pairing_code
                )
                if reply:
                    adapter = self._adapters_by_platform.get(platform_value)
                    if adapter is not None:
                        try:
                            await adapter.send(event.chat_id, reply)
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "pairing-reply: adapter.send failed for %s:%s",
                                platform_value,
                                event.chat_id,
                                exc_info=True,
                            )
                logger.info(
                    "allowlist-gate: denied %s:%s (source=%s, code=%s)",
                    platform_value,
                    user_id,
                    decision.source,
                    "yes" if decision.pairing_code else "rate-limited",
                )
                return None

        # ── PR-1 (Task 1.6) — Reset Policy ──────────────────────────────
        # Default-OFF: skipped when ``self._reset_policy`` is None.
        if self._reset_policy is not None:
            platform_value = event.platform.value if event.platform else ""
            key = (platform_value, event.chat_id)
            last_seen = self._chat_last_seen.get(key, 0.0)
            do_reset, reason = self._reset_policy.should_reset(
                platform_value, event.chat_id, last_seen
            )
            if do_reset:
                # Compose a reset token from the reason + current UTC date
                # so the new session_id is stable within the post-reset
                # window (later messages on the same day reach the same
                # token; the next reset advances to a new token).
                from datetime import datetime

                stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
                self._chat_reset_tokens[key] = f"{reason}-{stamp}"
                logger.info(
                    "reset-policy: fresh session for %s:%s (reason=%s)",
                    platform_value,
                    event.chat_id,
                    reason,
                )
            self._chat_last_seen[key] = time.time()
            self._persist_last_seen()

        session_id = self._session_id_for(event)

        # ── Hermes-parity consent-reply text classification (Phase 3 P3.3) ──
        # When the user has a pending approval prompt and replies with a
        # bare "yes"/"no"/"approve"/"deny" (the Hermes-spec keywords),
        # route the reply to ConsentGate.resolve_pending instead of the
        # agent loop. This complements the existing button-based
        # ``_handle_approval_click`` path — text replies are necessary on
        # platforms without inline buttons (SMS, IRC, Email) and natural
        # on platforms that support buttons (the user can type or tap).
        if event.text and event.text.strip():
            try:
                consumed = await self._maybe_resolve_consent_text_reply(
                    session_id=session_id, text=event.text,
                )
            except Exception:  # noqa: BLE001 — never let a consent-side
                # bug starve the regular reply path.
                logger.debug(
                    "consent text-reply check failed for session=%s",
                    session_id, exc_info=True,
                )
                consumed = False
            if consumed:
                # The reply has been delivered to the gate. The agent
                # loop's request_approval coroutine will unblock and
                # post the actual response. We return None so the
                # adapter doesn't double-respond.
                return None

        # 2026-05-08 — Hermes Doc-2 gateway hooks: session:start.
        # Fire only on first observation of this session id in this
        # process — subsequent dispatches for the same id are
        # continuations (session:end fires per-turn instead).
        if session_id not in self._known_sessions:
            self._known_sessions.add(session_id)
            try:
                from opencomputer.gateway.event_hooks import (
                    SESSION_START as _GW_SESSION_START,
                )
                from opencomputer.gateway.event_hooks import (
                    engine as _gw_hooks_engine_ss,
                )
                asyncio.create_task(
                    _gw_hooks_engine_ss.fire(_GW_SESSION_START, {
                        "platform": event.platform.value if event.platform else None,
                        "user_id": event.chat_id,
                        "session_id": session_id,
                    }),
                    name="gw-hook-session-start",
                )
            except Exception:  # noqa: BLE001
                logger.debug("gateway session:start fire failed", exc_info=True)

        # PR-A Feature 1: if /steer just fired for this session and the
        # agent loop hasn't yet consumed the cancel state, route this
        # inbound message to SteerBuffer instead of triggering a fresh
        # turn. The next-turn between-turn consume drains the buffer and
        # merges it into the replan as <USER-INTERRUPT>. This is a narrow
        # window — typically microseconds — but during a long-running
        # cancelled tool, multiple messages can pile up.
        try:
            from opencomputer.agent.steer import (
                default_buffer as _steer_buffer,
            )
            from opencomputer.agent.steer import (
                default_registry as _steer_reg,
            )

            if (
                _steer_reg.has_cancel_listener(session_id)
                and _steer_reg.cancel_event(session_id).is_set()
            ):
                _steer_buffer.append(session_id, event.text or "")
                logger.debug(
                    "gateway: buffered inbound during cancel-pending window "
                    "for session %s",
                    session_id,
                )
                return None
        except Exception:  # noqa: BLE001 — never block dispatch on this
            pass

        # Wave 5 T13 — Hermes-port pre_gateway_dispatch hook (1ef1e4c66).
        # Fires once per inbound message before any auth check. Plugins
        # can drop, rewrite, or allow. Plugin crashes are swallowed by
        # the hook engine (returns None → proceed normally).
        try:
            from opencomputer.hooks.engine import engine as _hook_engine
            from plugin_sdk.hooks import HookContext, HookEvent

            _gw_decision = await _hook_engine.fire_blocking(
                HookContext(
                    event=HookEvent.PRE_GATEWAY_DISPATCH,
                    session_id=session_id,
                    gateway_event_text=event.text,
                    sender_id=event.chat_id,
                ),
            )
            if _gw_decision is not None:
                if _gw_decision.decision == "skip":
                    logger.info(
                        "pre_gateway_dispatch: dropping message (reason=%s)",
                        _gw_decision.reason,
                    )
                    return None
                if (
                    _gw_decision.decision == "rewrite"
                    and _gw_decision.rewritten_text is not None
                ):
                    event = dataclasses.replace(
                        event, text=_gw_decision.rewritten_text,
                    )
        except Exception as _gwe:  # noqa: BLE001
            logger.debug("pre_gateway_dispatch fire failed: %s", _gwe)

        pure_attachment = attach_present and not text_present

        # ── Case 1: pure-attachment arrival joins the in-flight burst.
        if pure_attachment and session_id in self._burst_pending:
            pending = self._burst_pending[session_id]
            merged_meta = dict(pending.metadata or {})
            new_meta = event.metadata or {}
            if "attachment_meta" in new_meta:
                merged_meta.setdefault("attachment_meta", []).extend(
                    new_meta["attachment_meta"]
                )
            self._burst_pending[session_id] = dataclasses.replace(
                pending,
                attachments=list(pending.attachments) + list(event.attachments),
                metadata=merged_meta,
            )
            future = self._burst_futures[session_id]
            return await future

        # ── Case 2: text arrival mid-burst — cancel + absorb + run inline.
        if text_present and session_id in self._burst_tasks:
            task = self._burst_tasks.pop(session_id)
            task.cancel()
            pending = self._burst_pending.pop(session_id, None)
            future = self._burst_futures.pop(session_id, None)
            if pending is not None:
                event = dataclasses.replace(
                    event,
                    attachments=list(pending.attachments)
                    + list(event.attachments),
                )
            try:
                result = await self._do_dispatch(event, session_id)
            except BaseException as exc:
                if future is not None and not future.done():
                    future.set_exception(exc)
                raise
            if future is not None and not future.done():
                future.set_result(result)
            return result

        # ── Case 3: pure-attachment with no pending — start the timer.
        if pure_attachment:
            loop_ = asyncio.get_running_loop()
            future_ = loop_.create_future()
            self._burst_pending[session_id] = event
            self._burst_futures[session_id] = future_
            self._burst_tasks[session_id] = asyncio.create_task(
                self._dispatch_after_burst_window(session_id),
                name=f"dispatch-burst-{session_id[:8]}",
            )
            return await future_

        # ── Case 4: plain text — direct dispatch.
        return await self._do_dispatch(event, session_id)

    async def _dispatch_after_burst_window(self, session_id: str) -> None:
        """Background task: wait for the burst window then dispatch.

        Cancellation (text arrival) returns cleanly. The future is
        resolved here so every joiner sees the same answer; on any
        exception the future carries the exception so joiners observe
        it rather than hang forever.
        """
        try:
            await asyncio.sleep(self._burst_window_seconds)
        except asyncio.CancelledError:
            return
        event = self._burst_pending.pop(session_id, None)
        future = self._burst_futures.pop(session_id, None)
        self._burst_tasks.pop(session_id, None)
        if event is None or future is None:
            return
        try:
            result = await self._do_dispatch(event, session_id)
            if not future.done():
                future.set_result(result)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)

    async def _do_dispatch(
        self, event: MessageEvent, session_id: str
    ) -> str | None:
        """Run the agent loop for one (possibly burst-merged) MessageEvent.

        Hermes channel-port (PR 2 Task 2.6). The body is what
        ``handle_message`` previously did inline before the burst-
        aware preamble was added. Same return contract — assistant
        text or None.

        Phase 3 multi-routing: resolves ``profile_id`` per-event via
        ``self._resolver.resolve(event)`` (or ``"default"`` if no
        resolver is wired), fetches the per-profile ``AgentLoop`` via
        ``self._router.get_or_load(profile_id)``, and wraps the
        ``run_conversation`` call in
        ``set_profile(profile_home)`` so ``_home()`` and PluginAPI
        lazy properties resolve to the right profile inside the
        request scope.

        PR-1 follow-up — inflight bookkeeping. Increments
        ``self._inflight_count`` for the duration of this call so the
        gateway's drain-aware ``serve_forever`` can wait for active
        dispatches to finish before exiting.
        """
        # Inflight tracking — guarded so legacy callers that bypass
        # __init__ (Dispatch.__new__(Dispatch) in tests) keep working.
        try:
            self._inflight_count = getattr(self, "_inflight_count", 0) + 1
        except Exception:  # noqa: BLE001
            pass
        try:
            return await self.__do_dispatch_inner(event, session_id)
        finally:
            try:
                self._inflight_count = max(0, getattr(self, "_inflight_count", 0) - 1)
            except Exception:  # noqa: BLE001
                pass

    async def __do_dispatch_inner(
        self, event: MessageEvent, session_id: str
    ) -> str | None:
        """Inner dispatch — wrapped by ``_do_dispatch`` for inflight bookkeeping."""
        # Phase 3 Task 3.3: per-event profile resolution via the
        # BindingResolver. Falls back to "default" when no resolver
        # was wired — preserves the legacy behaviour for callers /
        # tests that construct Dispatch without a bindings file.
        profile_id = (
            self._resolver.resolve(event) if self._resolver is not None else "default"
        )

        # A8 (gateway-vs-CLI parity) — apply a /handoff profile override.
        # /handoff records its target in the runtime-state store; the
        # override is persistent (mirrors the CLI persisting the active
        # profile on disk), so it wins over the binding-resolved profile
        # on this turn and every turn after, until another /handoff.
        try:
            _override = self._runtime_state.get_profile_override(session_id)
            if _override and _override != profile_id:
                logger.info(
                    "gateway /handoff: %s → %s for session %s",
                    profile_id, _override, session_id,
                )
                profile_id = _override
        except Exception:  # noqa: BLE001 — a swap glitch must not break dispatch
            logger.debug("profile-override apply failed", exc_info=True)

        # A6 (gateway-vs-CLI parity) — per-chat working directory. The
        # daemon's process cwd is its launch directory (usually the
        # profile home), not the user's project. A binding may pin
        # ``cwd:`` so file / Bash tools operate where the user expects.
        # Bound around ``run_conversation`` below via a ContextVar so it
        # propagates to the tool-dispatch tasks without an os.chdir
        # (which would race across concurrent gateway sessions).
        chat_cwd: str | None = None
        _binding_queue_mode: str | None = None
        if self._resolver is not None:
            try:
                _winning = self._resolver.resolve_binding(event)
                _raw_cwd = _winning.cwd if _winning is not None else None
                if _raw_cwd:
                    if os.path.isdir(_raw_cwd):
                        chat_cwd = _raw_cwd
                    else:
                        logger.warning(
                            "binding cwd %r is not a directory; "
                            "ignoring (file tools use the daemon cwd)",
                            _raw_cwd,
                        )
                _binding_queue_mode = (
                    _winning.queue_mode if _winning is not None else None
                )
            except Exception:  # noqa: BLE001 — routing must never break dispatch
                logger.debug("binding cwd resolution failed", exc_info=True)

        # A9 (gateway-vs-CLI parity) — seed a binding's pinned
        # ``queue_mode`` exactly once per session. ``has_session_mode``
        # guards against clobbering a later ``/queue-mode`` from the
        # user: the binding is the *default*, not an every-turn force.
        if _binding_queue_mode and not self._queue_manager.has_session_mode(
            session_id
        ):
            try:
                # load_bindings already validated the literal; the cast
                # keeps the type checker honest on the str→QueueMode hop.
                self._queue_manager.set_session_mode(
                    session_id,
                    _binding_queue_mode,  # pyright: ignore[reportArgumentType]
                )
            except ValueError:
                logger.warning(
                    "binding queue_mode %r invalid; ignoring",
                    _binding_queue_mode,
                )

        # Pass-1 G9: structured per-dispatch logging. ``binding_match``
        # is "matched" when the resolver picked a non-default profile;
        # "default" otherwise. Logged BEFORE lock acquisition so even a
        # contended turn surfaces the routing decision early.
        logger.info(
            "dispatch routing",
            extra={
                "platform": event.platform.value if event.platform else None,
                "chat_id": event.chat_id,
                "session_id": session_id,
                "profile_id": profile_id,
                "binding_match": (
                    "default"
                    if (
                        self._resolver is None
                        or profile_id == self._resolver._cfg.default_profile
                    )
                    else "matched"
                ),
            },
        )

        loop = await self._router.get_or_load(profile_id)
        profile_home = self._router._profile_home_resolver(profile_id)

        # M1 gateway-vs-CLI parity telemetry: one ParityProbe per turn.
        # The probe accumulates which of the 10 parity-affecting
        # mechanisms fire and is flushed once at turn-end (10 rows to
        # ``audit.db``). ``_initial_profile_id`` is captured BEFORE the
        # M10.3 rebind block so mechanism #6 (profile_rebind) can detect
        # a swap. All telemetry is best-effort — never breaks dispatch.
        _initial_profile_id = profile_id
        _parity_probe = None
        try:
            from opencomputer.gateway.parity_probe import ParityProbe

            _parity_probe = ParityProbe(
                session_id=session_id,
                turn_id=_compute_turn_index(loop.db, session_id),
                platform=event.platform.value if event.platform else "unknown",
            )
        except Exception:  # noqa: BLE001 — telemetry must never break dispatch
            logger.debug("parity probe init failed", exc_info=True)

        # v1.1 plan-3 M10.3 — per-rule profile rebind. After the
        # BindingResolver picks the source profile and we load its loop,
        # consult the source profile's routing.rules for THIS event. If
        # a matched rule sets `profile: <name>` (carried as
        # ``ResolvedTemplate.profile_rebind`` from M10.2), swap to the
        # named profile's loop for the remainder of dispatch.
        #
        # Composition with M10.2 (system_prompt routing): after this
        # swap, the M10.2 resolution inside ``_dispatch_tool_calls``
        # consults the REBOUND profile's routing rules. If the rebound
        # profile lists the same rule (typical setup — source profile
        # routes to specialised profiles), the agent template + system
        # prompt resolve again on the rebound profile and apply
        # identically.
        #
        # Composition with M1.4 (per-profile env): per-profile .env
        # files are loaded at CLI startup based on the active profile.
        # Mid-process rebinds via M10.3 use the rebound profile's loop
        # which was constructed against its own profile_home, so its
        # cached resources (memory, agent templates, MCP servers) cover
        # the common case. Re-loading per-profile env per-message is a
        # follow-up if real workloads need credential isolation per
        # rebound dispatch (currently the source-profile env wins).
        #
        # Defensive: any failure logs WARNING and falls through to the
        # source profile — a stale rule must NEVER break message dispatch.
        try:
            cfg_obj_for_rebind = getattr(loop, "config", None)
            routing_cfg_for_rebind = getattr(cfg_obj_for_rebind, "routing", None)
            if routing_cfg_for_rebind is not None and routing_cfg_for_rebind.rules:
                from opencomputer.agent.agent_templates import (
                    discover_agents as _discover_for_rebind,
                )
                from opencomputer.agent.routing import (
                    resolve_template_for_event as _resolve_for_rebind,
                )

                _templates_for_rebind = _discover_for_rebind()
                _resolved_for_rebind = _resolve_for_rebind(
                    routing_cfg_for_rebind, event, _templates_for_rebind
                )
                if (
                    _resolved_for_rebind is not None
                    and _resolved_for_rebind.profile_rebind
                    and _resolved_for_rebind.profile_rebind != profile_id
                ):
                    _new_pid = _resolved_for_rebind.profile_rebind
                    try:
                        _new_loop = await self._router.get_or_load(_new_pid)
                        _new_home = self._router._profile_home_resolver(_new_pid)
                        logger.info(
                            "M10.3 routing rebind: %s:%s → profile=%r "
                            "(source=%r, agent=%r)",
                            event.platform.value if event.platform else "?",
                            event.chat_id,
                            _new_pid,
                            profile_id,
                            _resolved_for_rebind.template_name,
                        )
                        loop = _new_loop
                        profile_home = _new_home
                        profile_id = _new_pid
                    except Exception as _exc:  # noqa: BLE001
                        logger.warning(
                            "M10.3 routing rebind to %r failed (%s) — "
                            "continuing on source profile %r",
                            _new_pid, _exc, profile_id,
                        )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "M10.3 routing rebind resolution failed: %s — "
                "continuing on source profile",
                e,
            )

        # M1 parity telemetry — mechanism #6 (profile_rebind) and #2
        # (tool_allowlist) are both decidable now that ``loop`` is
        # final. #6 fired iff the M10.3 block above swapped profiles;
        # #2 fired iff the gateway loop carries a non-wildcard tool
        # allowlist (the CLI never sets one).
        if _parity_probe is not None:
            try:
                _rebound = profile_id != _initial_profile_id
                _parity_probe.observe(
                    "profile_rebind",
                    _rebound,
                    {"from": _initial_profile_id, "to": profile_id}
                    if _rebound
                    else {},
                )
                _allowed = getattr(loop, "allowed_tools", None)
                _parity_probe.observe(
                    "tool_allowlist",
                    _allowed is not None,
                    {"tool_count": len(_allowed)} if _allowed is not None else {},
                )
            except Exception:  # noqa: BLE001 — telemetry never breaks dispatch
                logger.debug("parity probe observe (rebind/tools) failed", exc_info=True)

        # Round 2a P-5 — record the (adapter, chat_id) binding so a
        # consent prompt later in this turn can find the right surface
        # to ask the user on. Best-effort: missing adapter = legacy
        # CLI/wire path, no harm done.
        adapter = self._adapters_by_platform.get(
            event.platform.value if event.platform else ""
        )
        if adapter is not None:
            self._session_channels[session_id] = (adapter, event.chat_id)
        # Critical fix: record per-session profile so the consent gate
        # can be resolved via the router in _send_approval_prompt and
        # _handle_approval_click even when self.loop is None (router=
        # construction path).
        self._session_profiles[session_id] = profile_id
        # Hermes channel-port (PR 2 Task 2.2): fire on_processing_start
        # BEFORE acquiring the per-chat lock so a fast-clicking user
        # sees the 👀 reaction even if the previous turn is still
        # holding the lock. Fire-and-forget — failures never affect
        # the reply path.
        #
        # 2026-05-08: gated on ``self._lifecycle_reactions`` (default
        # off). Without the gate, Telegram clients render the reaction
        # inline next to the user's message and it reads like the bot
        # is auto-replying with 👀. See GatewayConfig.lifecycle_reactions.
        message_id: str | None = None
        if event.metadata:
            raw_id = event.metadata.get("message_id")
            if isinstance(raw_id, str | int) and raw_id != "":
                message_id = str(raw_id)
        if adapter is not None and self._lifecycle_reactions:
            asyncio.create_task(
                self._safe_lifecycle_hook(
                    adapter.on_processing_start(event.chat_id, message_id)
                )
            )
        # Wave 6.E.6 — bypass-running-guard slash commands. Some slash
        # commands (currently only ``/kanban``) are durable, cheap, and
        # should reach the user mid-turn without queuing behind a long
        # agent reply. The class attribute ``bypass_running_guard``
        # opts the command into this fast path.
        bypass_result = await self._maybe_bypass_running_guard(
            event, session_id, profile_id, loop,
        )
        if bypass_result is not None:
            return bypass_result

        # Phase 2 (S1) — queue manager replaces the legacy lock dict.
        # ``acquire`` resolves the per-session mode (default ``followup``
        # serializes; ``interrupt`` cancels any in-flight run before
        # entering the body; ``collect`` buffers + drains via the
        # leader-of-debounce-window pattern below).
        mode = self._queue_manager.get_session_mode(session_id)
        if mode == "collect" and (event.text or "").strip():
            # Always buffer this arrival's text + reset the debounce timer.
            self._queue_manager.buffer_message(session_id, event.text or "")
            await self._queue_manager.schedule_collect_drain(session_id)

            # Determine leadership: first arrival per drain window runs the
            # agent on the merged buffer; subsequent arrivals return early.
            # The leader claim lives in self._collect_leaders[session_id].
            if self._collect_leaders.get(session_id):
                # A leader is already waiting for the drain; we just
                # contributed to the buffer + reset its timer.
                return None

            self._collect_leaders[session_id] = True
            try:
                # Wait for the debounce window to close. Subsequent arrivals
                # (above) reset the timer, extending our wait.
                await self._queue_manager.wait_for_drain(session_id)
                # Drain the merged buffer and replace event.text with it.
                merged = self._queue_manager.drain_buffer(session_id)
                if merged:
                    event = MessageEvent(
                        platform=event.platform,
                        chat_id=event.chat_id,
                        user_id=event.user_id,
                        text=merged,
                        timestamp=event.timestamp,
                        attachments=list(event.attachments),
                        metadata={**event.metadata, "collect_merged": True},
                    )
                # Fall through to the existing followup-style serialized
                # acquire so the rest of the dispatch path is unchanged.
            finally:
                # Clear leadership BEFORE entering the agent run so the
                # next arrival post-drain can start a fresh debounce window
                # while this run is in flight.
                self._collect_leaders.pop(session_id, None)

        outcome: ProcessingOutcome = ProcessingOutcome.SUCCESS
        async with self._queue_manager.acquire(profile_id, session_id):
            # Start a typing heartbeat (Telegram's typing state expires after
            # ~5s, so we re-send every 4s until the turn completes).
            heartbeat = asyncio.create_task(
                self._typing_heartbeat(event.platform.value, event.chat_id)
            )
            # Task I.9: build a per-request ctx.
            request_ctx = RequestContext(
                request_id=str(uuid.uuid4()),
                channel=event.platform.value if event.platform else None,
                user_id=event.chat_id,
                session_id=session_id,
                started_at=time.monotonic(),
            )
            # Hermes channel-port (PR 2 Task 2.6): pass attachments
            # through to the agent loop as ``images=`` when present.
            # ``user_message`` defaults to "" for pure-attachment
            # events; the loop's prompt builder treats an empty message
            # with non-empty images as a vision-only turn.
            user_message = event.text or ""
            images = list(event.attachments) if event.attachments else None
            # P0-4b: back-fill the prior turn's affirmation/correction/
            # latency from this incoming user message. Fire-and-forget;
            # if there's no prior turn (first message of session) it's
            # a clean no-op. Wrapped in try/except so a telemetry
            # failure can never block dispatch.
            try:
                _backfill_task = asyncio.create_task(
                    _backfill_prior_turn_async(
                        loop.db,
                        session_id=session_id,
                        user_text=user_message,
                        now_ts=time.time(),
                    )
                )
                _pending_outcome_writes.add(_backfill_task)
                _backfill_task.add_done_callback(_pending_outcome_writes.discard)
            except Exception as e:  # noqa: BLE001
                logger.warning("backfill scheduling failed: %s", e)
            # Hermes channel-port (PR 5): per-channel ephemeral system
            # prompt + auto-loaded skills. When the inbound MessageEvent
            # carries a ``channel_id`` (Telegram DM Topics surface this
            # via ``message_thread_id`` lookup) we ask the originating
            # adapter for a resolved prompt + skill list and thread the
            # result through ``RuntimeContext.custom``. The agent loop's
            # per-turn ``system`` lane (the same lane that appends
            # ``prefetched`` memory) reads these and appends them to the
            # composed system prompt — staying out of the FROZEN base
            # so prefix-cache hits on turn 2+ remain valid.
            runtime = self._build_channel_runtime(event, adapter, loop)
            # M3 #7 fix — display.persona_override. When the operator
            # has pinned a persona for gateway sessions, thread it onto
            # the runtime so the loop's existing override path
            # (_build_persona_overlay) applies it instead of letting the
            # platform-driven classifier pick a casual register.
            #   value "none"/"off" → suppress the persona overlay entirely
            #   any other value     → pin that persona id
            # CRITICAL: _build_channel_runtime returns the module-shared
            # DEFAULT_RUNTIME_CONTEXT when there is no channel_id —
            # mutating its .custom would leak across every session (the
            # #10-class bug). So we build a FRESH RuntimeContext here.
            _persona_override = str(
                (self._display_cfg.get("display") or {}).get(
                    "persona_override", ""
                )
                or ""
            ).strip()
            if _persona_override:
                _rt_custom = dict(getattr(runtime, "custom", {}) or {})
                if _persona_override.lower() in ("none", "off", "disabled"):
                    _rt_custom["persona_disabled"] = True
                else:
                    _rt_custom["persona_id_override"] = _persona_override
                runtime = RuntimeContext(custom=_rt_custom)
            # A2 (gateway-vs-CLI parity) — per-chat plan mode. /plan
            # persists the toggle in <profile>/gateway/runtime_state.json;
            # inject plan_mode onto the runtime so the loop's plan-mode
            # path applies, parity with `oc --plan` on the CLI.
            # dataclasses.replace returns a NEW RuntimeContext — it never
            # mutates the module-shared DEFAULT_RUNTIME_CONTEXT.
            try:
                if self._runtime_state.get_plan_mode(session_id):
                    import dataclasses as _dc

                    runtime = _dc.replace(runtime, plan_mode=True)
            except Exception:  # noqa: BLE001 — never break dispatch
                logger.debug("plan-mode injection failed", exc_info=True)
            # M1 parity telemetry — mechanisms decidable from the
            # per-turn runtime: #4 (channel_prompt_overlay) fired iff
            # _build_channel_runtime stuffed a channel-scoped prompt or
            # skill bodies into custom; #7 (persona_casual_register) is
            # structural — a gateway turn carries the chat agent_context;
            # #5 (no_interactive_consent) is structural — the gateway
            # cannot prompt for tool approval synchronously.
            if _parity_probe is not None:
                try:
                    _custom = getattr(runtime, "custom", {}) or {}
                    _overlay = bool(
                        _custom.get("channel_prompt")
                        or _custom.get("channel_skill_bodies")
                    )
                    _parity_probe.observe(
                        "channel_prompt_overlay",
                        _overlay,
                        {
                            "has_prompt": bool(_custom.get("channel_prompt")),
                            "has_skills": bool(_custom.get("channel_skill_bodies")),
                        }
                        if _overlay
                        else {},
                    )
                    # #7 — fires when the turn runs in the platform-
                    # driven casual register. A display.persona_override
                    # (pin or disable) closes the gap → fired=False.
                    _agent_ctx = getattr(runtime, "agent_context", "chat") or "chat"
                    _persona_pinned = bool(
                        _custom.get("persona_id_override")
                        or _custom.get("persona_disabled")
                    )
                    _parity_probe.observe(
                        "persona_casual_register",
                        _agent_ctx == "chat" and not _persona_pinned,
                        {
                            "agent_context": _agent_ctx,
                            "persona_override": _persona_pinned,
                        },
                    )
                    # #5 (no_interactive_consent) is decided AFTER the
                    # turn — see the post-run consent-delta observe.
                except Exception:  # noqa: BLE001 — telemetry never breaks dispatch
                    logger.debug("parity probe observe (runtime) failed", exc_info=True)
            # Phase 2 audit G1: bind the per-task ``current_profile_home``
            # ContextVar around ``run_conversation`` so ``_home()`` and
            # any PluginAPI lazy paths resolve to the right profile for
            # the duration of this dispatch.
            from plugin_sdk.profile_context import set_profile
            # P0-4: capture wall-clock around run_conversation so we
            # can record a turn_outcomes row at the end.
            turn_start_ts = time.time()
            # 2026-05-08 — Hermes Doc-2 gateway hooks: agent:start.
            # Fire-and-forget (gathered concurrently; never blocks the
            # turn). Failure isolated.
            try:
                from opencomputer.gateway.event_hooks import (
                    AGENT_START as _GW_AGENT_START,
                )
                from opencomputer.gateway.event_hooks import (
                    engine as _gw_hooks_engine_a1,
                )
                _agent_ctx = {
                    "session_id": session_id,
                    "platform": event.platform.value if event.platform else None,
                    "user_id": event.chat_id,
                    "message": user_message,
                }
                asyncio.create_task(
                    _gw_hooks_engine_a1.fire(_GW_AGENT_START, _agent_ctx),
                    name="gw-hook-agent-start",
                )
            except Exception:  # noqa: BLE001 — hook firing must never block
                logger.debug("gateway agent:start fire failed", exc_info=True)
            try:
                self._active_runs.add(session_id)
                # Kanban-Goals v2 (2026-05-08) — install a per-session
                # goal-banner callback so the Ralph loop's ``↻/✓/⏸``
                # transitions reach this chat as a separate message.
                # Per-session keying matters because one AgentLoop can
                # serve multiple concurrent sessions on the same
                # profile; a global callback would fan banners to the
                # wrong chat.
                if adapter is not None and hasattr(loop, "set_goal_banner_callback"):
                    self._install_goal_banner_callback(
                        loop=loop,
                        session_id=session_id,
                        adapter=adapter,
                        chat_id=event.chat_id,
                    )
                # v1.1 plan-3 M10.2 — per-channel routing dispatcher
                # integration. ``resolve_template_for_event`` returns the
                # matched template (system prompt + maybe profile rebind)
                # or None if no rule matches / the named template isn't
                # registered. We pass the system prompt through the same
                # ``system_prompt_override`` plumbing that ``DelegateTool``
                # already uses for ``agent: ...``. Tool allowlist
                # enforcement is a follow-up (AgentLoop's
                # ``allowed_tools`` is constructor-bound; per-message
                # filtering needs more plumbing).
                #
                # Wrapped defensively: any routing failure logs at WARNING
                # and falls through to the default behavior — a stale
                # routing rule must NEVER break message dispatch.
                routing_system_override: str | None = None
                routing_template_name: str | None = None
                routing_system_merge: bool = False
                try:
                    cfg_obj = getattr(loop, "config", None)
                    routing_cfg = getattr(cfg_obj, "routing", None)
                    if routing_cfg is not None and routing_cfg.rules:
                        from opencomputer.agent.agent_templates import (
                            discover_agents as _discover_agents,
                        )
                        from opencomputer.agent.routing import (
                            resolve_template_for_event as _rs_template,
                        )

                        templates = _discover_agents()
                        resolved = _rs_template(routing_cfg, event, templates)
                        if resolved is not None:
                            routing_system_override = resolved.system_prompt
                            routing_template_name = resolved.template_name
                            routing_system_merge = resolved.merge_with_builder
                            logger.info(
                                "M10.2 routing: %s:%s → agent=%r (merge=%s)",
                                event.platform.value if event.platform else "?",
                                event.chat_id,
                                resolved.template_name,
                                resolved.merge_with_builder,
                            )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "M10.2 routing resolution failed (falling through "
                        "to default dispatch): %s",
                        e,
                    )

                # Routing summary for this turn — drives both parity
                # mechanism #1/#8 telemetry below AND the M3 #6/#8
                # chat-visible routing badge appended in the success
                # path. Computed unconditionally (not gated on the
                # probe) so the badge works even if telemetry init
                # failed.
                _override_active = bool(
                    routing_system_override and routing_system_override.strip()
                )
                _binding_matched = (
                    self._resolver is not None
                    and profile_id != self._resolver._cfg.default_profile
                )
                _rebound = profile_id != _initial_profile_id
                _routed = _override_active or _binding_matched or _rebound

                # M1 parity telemetry — #1 (prompt_override) fired iff a
                # routing rule supplied a non-empty system prompt;
                # #8 (routing_decision_invisible) fired iff ANY routing
                # decision changed behaviour AND no badge surfaced it.
                if _parity_probe is not None:
                    try:
                        _parity_probe.observe(
                            "prompt_override",
                            _override_active,
                            {
                                "template": routing_template_name,
                                "override_len": len(routing_system_override or ""),
                            }
                            if _override_active
                            else {},
                        )
                        # #8 "invisible" — fired when routing happened
                        # but the badge was NOT shown on this turn
                        # (badge shows once per session; see success
                        # path). A turn that surfaces the badge reads
                        # fired=False — the gap is closed for it.
                        _badge_will_show = (
                            _routed
                            and session_id not in self._routing_badge_shown
                        )
                        _parity_probe.observe(
                            "routing_decision_invisible",
                            _routed and not _badge_will_show,
                            {
                                "binding_matched": _binding_matched,
                                "template": routing_template_name,
                                "rebound": _rebound,
                                "badge_shown": _badge_will_show,
                            }
                            if _routed
                            else {},
                        )
                    except Exception:  # noqa: BLE001 — telemetry never breaks dispatch
                        logger.debug(
                            "parity probe observe (routing) failed", exc_info=True
                        )

                # M1/M3 #10 fix — snapshot the session's compaction
                # count BEFORE the turn. Mechanism #10 must be decided
                # by a before/after delta on the durable
                # ``sessions.compactions_count`` column, NOT by probing
                # ``runtime.custom`` — ``_build_channel_runtime`` returns
                # the module-shared ``DEFAULT_RUNTIME_CONTEXT`` when
                # there is no channel_id, so a ``session_compactions``
                # key written by one turn's ``_record_compaction`` leaks
                # into every later turn's runtime and would over-report.
                _pre_compactions = _read_compactions_count(loop.db, session_id)
                # M3 #5 — snapshot the consent-prompt count so we can
                # tell, post-turn, whether this turn actually paid an
                # async-consent round-trip.
                _pre_consent = self._consent_prompt_counts.get(session_id, 0)

                # A6 — bind the per-chat cwd for the duration of the
                # turn. ``working_directory(None)`` is a no-op, so the
                # default (no binding cwd) path is unchanged.
                from plugin_sdk.working_directory import working_directory

                with set_profile(profile_home), working_directory(chat_cwd):
                    if self._plugin_api is not None:
                        with self._plugin_api.in_request(request_ctx):
                            result = await loop.run_conversation(
                                user_message=user_message,
                                session_id=session_id,
                                images=images,
                                runtime=runtime,
                                system_prompt_override=routing_system_override,
                                system_prompt_merge=routing_system_merge,
                            )
                    else:
                        result = await loop.run_conversation(
                            user_message=user_message,
                            session_id=session_id,
                            images=images,
                            runtime=runtime,
                            system_prompt_override=routing_system_override,
                            system_prompt_merge=routing_system_merge,
                        )
                # 2026-05-08 — Hermes Doc-2 gateway hooks: agent:end +
                # session:end. Hermes spec: session:end fires per
                # run_conversation (i.e. per turn), agent:end same window.
                # Both fire-and-forget so a slow handler can't delay the
                # next turn.
                try:
                    from opencomputer.gateway.event_hooks import (
                        AGENT_END as _GW_AGENT_END,
                    )
                    from opencomputer.gateway.event_hooks import (
                        SESSION_END as _GW_SESSION_END,
                    )
                    from opencomputer.gateway.event_hooks import (
                        engine as _gw_hooks_engine_a2,
                    )
                    _final_text_for_hook = (
                        result.final_message.content
                        if isinstance(result.final_message.content, str)
                        else ""
                    )
                    _agent_end_ctx = {
                        "session_id": session_id,
                        "platform": event.platform.value if event.platform else None,
                        "user_id": event.chat_id,
                        "message": user_message,
                        "response": _final_text_for_hook,
                    }
                    asyncio.create_task(
                        _gw_hooks_engine_a2.fire(_GW_AGENT_END, _agent_end_ctx),
                        name="gw-hook-agent-end",
                    )
                    asyncio.create_task(
                        _gw_hooks_engine_a2.fire(
                            _GW_SESSION_END,
                            {
                                "platform": event.platform.value if event.platform else None,
                                "user_id": event.chat_id,
                                "session_key": session_id,
                            },
                        ),
                        name="gw-hook-session-end",
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("gateway agent:end / session:end fire failed", exc_info=True)
                # P0-4: schedule turn_outcomes write fire-and-forget.
                # Errors are swallowed inside _record_turn_outcome_async
                # so a telemetry failure never breaks the user reply.
                turn_end_ts = time.time()
                try:
                    sig = _build_end_of_turn_signals(
                        loop.db,
                        session_id=session_id,
                        turn_index=_compute_turn_index(loop.db, session_id),
                        start_ts=turn_start_ts,
                        end_ts=turn_end_ts,
                    )
                    task = asyncio.create_task(
                        _record_turn_outcome_async(loop.db, sig)
                    )
                    _pending_outcome_writes.add(task)
                    task.add_done_callback(_pending_outcome_writes.discard)
                except Exception as e:  # noqa: BLE001 — telemetry guard
                    logger.warning("outcome scheduling failed: %s", e)
                # Wave 5 T4 — append the runtime-metadata footer if the
                # operator has opted in via display.runtime_footer.enabled
                # (per-platform overrides honored). Default off so existing
                # deployments see no UX change. Wrapped defensively — a
                # footer-render failure must never replace the actual reply.
                _final_text = result.final_message.content or None
                # A7 — prepend the one-line session banner on the first
                # reply of a session so the user knows which profile /
                # model / cwd answered. Defensive — never replaces the
                # reply on failure.
                try:
                    _banner = self._maybe_session_banner(
                        session_id, profile_id, loop, chat_cwd,
                    )
                    if _banner:
                        _final_text = (
                            f"_{_banner}_\n\n{_final_text}"
                            if _final_text
                            else f"_{_banner}_"
                        )
                except Exception:  # noqa: BLE001
                    logger.debug("session banner prepend failed", exc_info=True)
                try:
                    from opencomputer.gateway.runtime_footer import (
                        format_runtime_footer,
                        resolve_footer_config,
                    )

                    _platform_name = (
                        event.platform.value if event.platform else None
                    )
                    _fc = resolve_footer_config(
                        self._display_cfg, platform=_platform_name,
                    )
                    # M1 parity telemetry — mechanism #9: the runtime
                    # footer is "fired" (as a gap) when it is OFF, since
                    # an off footer is what hides the model/context from
                    # the user. An empty fields list counts as off too.
                    if _parity_probe is not None:
                        try:
                            _footer_off = not (_fc.enabled and _fc.fields)
                            _parity_probe.observe(
                                "runtime_footer_off",
                                _footer_off,
                                {} if _footer_off else {"enabled": True},
                            )
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "parity probe observe (footer) failed",
                                exc_info=True,
                            )
                    if _fc.enabled and _final_text:
                        _model_name = (
                            getattr(getattr(loop, "config", None), "model", None)
                        )
                        _model_str = (
                            getattr(_model_name, "model", "") if _model_name else ""
                        )
                        # 2026-05-09 — resolve context_length via the same
                        # multi-source probe the CLI status-line uses.
                        # Resolution chain: model_context_overrides →
                        # custom_providers per-model override → probe
                        # (OpenRouter / Ollama / Anthropic / models.dev) →
                        # static DEFAULT_CONTEXT_WINDOWS table. Result is
                        # always an int (>= 64k conservative default) so
                        # context_pct now renders for every gateway turn.
                        # ``enable_probe=False`` keeps this hot path
                        # synchronous; the disk-cached 24h probe layer is
                        # separately refreshed by status-line / startup.
                        _ctx_len: int | None = None
                        try:
                            from opencomputer.agent.compaction import (
                                context_window_with_overrides,
                            )

                            _cfg = getattr(loop, "config", None)
                            if _cfg is not None and _model_str:
                                _ctx_len = context_window_with_overrides(
                                    _model_str,
                                    custom_providers=getattr(
                                        _cfg, "custom_providers", (),
                                    ),
                                    model_context_overrides=getattr(
                                        _cfg, "model_context_overrides", None,
                                    ),
                                    enable_probe=False,
                                )
                        except Exception:  # noqa: BLE001 — defensive
                            _ctx_len = None
                        _line = format_runtime_footer(
                            model=_model_str,
                            tokens_used=getattr(result, "input_tokens", 0) or 0,
                            context_length=_ctx_len,
                            # A6 — show the per-chat cwd when a binding
                            # pinned one; else the daemon's process cwd.
                            cwd=chat_cwd or os.getcwd(),
                        )
                        if _line:
                            _final_text = f"{_final_text}\n\n_{_line}_"
                except Exception as _fe:  # noqa: BLE001
                    logger.debug("runtime_footer render failed: %s", _fe)
                # M3 #6/#8 fix — routing/rebind badge. When a binding,
                # routing rule or profile-rebind changed this turn's
                # behaviour, append a one-line badge the FIRST time it
                # happens for a session, so the user knows they are not
                # talking to their plain default agent. Shown once per
                # session (in-memory latch) to avoid cluttering every
                # reply. Best-effort — a badge failure never replaces
                # the reply.
                try:
                    if (
                        _routed
                        and _final_text
                        and session_id not in self._routing_badge_shown
                    ):
                        _badge_bits: list[str] = []
                        if routing_template_name:
                            _badge_bits.append(f"agent={routing_template_name}")
                        if _rebound or _binding_matched:
                            _badge_bits.append(f"profile={profile_id}")
                        if _badge_bits:
                            _final_text = (
                                f"{_final_text}\n\n_↪ routed: "
                                f"{', '.join(_badge_bits)}_"
                            )
                        self._routing_badge_shown.add(session_id)
                except Exception:  # noqa: BLE001 — badge never breaks the reply
                    logger.debug("routing badge render failed", exc_info=True)
                # M1 parity telemetry — last two mechanisms, decided
                # from the finished turn. #3 (reply_truncation): the
                # reply exceeds the adapter's message-length cap.
                # #10 (compaction_long_session): the session's durable
                # compaction count rose during this turn.
                if _parity_probe is not None:
                    try:
                        _cap = getattr(adapter, "max_message_length", 0) or 0
                        _reply_len = len(_final_text or "")
                        _truncated = bool(_cap and _reply_len > _cap)
                        _parity_probe.observe(
                            "reply_truncation",
                            _truncated,
                            {"reply_len": _reply_len, "cap": _cap}
                            if _truncated
                            else {},
                        )
                        _post_compactions = _read_compactions_count(
                            loop.db, session_id
                        )
                        _compacted = _post_compactions > _pre_compactions
                        _parity_probe.observe(
                            "compaction_long_session",
                            _compacted,
                            {
                                "compactions_before": _pre_compactions,
                                "compactions_after": _post_compactions,
                            }
                            if _compacted
                            else {},
                        )
                        # #5 — fired iff this turn actually sent an
                        # async-consent approval prompt. The gateway HAS
                        # working interactive consent (buttons + text
                        # reply); #5 marks the turns that paid its
                        # round-trip latency, not every turn.
                        _post_consent = self._consent_prompt_counts.get(
                            session_id, 0
                        )
                        _consent_roundtrip = _post_consent > _pre_consent
                        _parity_probe.observe(
                            "no_interactive_consent",
                            _consent_roundtrip,
                            {"prompts": _post_consent - _pre_consent}
                            if _consent_roundtrip
                            else {},
                        )
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "parity probe observe (reply) failed", exc_info=True
                        )
                return _final_text
            except Exception as e:  # noqa: BLE001
                # Always log full traceback for debugging; user only
                # sees the one-liner from _format_user_facing_error so
                # SDK internals / prompt fragments don't leak to chat.
                logger.exception(
                    "dispatch error for platform=%s profile=%s: %s",
                    event.platform, profile_id, e,
                )
                outcome = ProcessingOutcome.FAILURE
                return _format_user_facing_error(e)
            finally:
                # M1 parity telemetry — flush the per-turn probe (10
                # rows) to the profile's ``audit.db``. Runs in the
                # ``finally`` so it fires on the error path too (a
                # failed turn still emits, mostly fired=0). Best-effort:
                # ``flush`` swallows SQLite errors internally; the outer
                # guard catches a missing config.home etc.
                if _parity_probe is not None:
                    try:
                        _cfg_home = getattr(
                            getattr(loop, "config", None), "home", None
                        )
                        if _cfg_home is not None:
                            _parity_probe.flush(_cfg_home / "audit.db")
                    except Exception:  # noqa: BLE001 — telemetry never breaks dispatch
                        logger.debug("parity probe flush failed", exc_info=True)
                # Kanban-Goals v2 (2026-05-08) — release the active-run
                # marker so /goal <text> dispatched in the next moment
                # is no longer refused.
                self._active_runs.discard(session_id)
                # Symmetric banner-callback teardown.
                if hasattr(loop, "clear_goal_banner_callback"):
                    try:
                        loop.clear_goal_banner_callback(session_id)
                    except Exception:  # noqa: BLE001
                        pass
                heartbeat.cancel()
                try:
                    await heartbeat
                except (asyncio.CancelledError, Exception):
                    pass
                # Hermes channel-port (PR 2 Task 2.2): fire
                # on_processing_complete after the turn settles, with
                # the outcome captured above. Fire-and-forget so a
                # failing reaction send doesn't mask the actual reply.
                #
                # 2026-05-08: gated on ``self._lifecycle_reactions``
                # (default off) — paired with the on_processing_start
                # gate above. See GatewayConfig.lifecycle_reactions.
                if adapter is not None and self._lifecycle_reactions:
                    asyncio.create_task(
                        self._safe_lifecycle_hook(
                            adapter.on_processing_complete(
                                event.chat_id, message_id, outcome
                            )
                        )
                    )

    def _build_channel_runtime(
        self,
        event: MessageEvent,
        adapter: Any,
        loop: AgentLoop | None = None,
    ) -> RuntimeContext:
        """Resolve per-channel prompt + skill bindings into a RuntimeContext.

        Hermes channel-port (PR 5). Reads ``event.metadata["channel_id"]``
        (set by the Telegram DM-Topics path); calls the adapter's
        :meth:`BaseChannelAdapter.resolve_channel_prompt` /
        :meth:`resolve_channel_skills` resolvers; if either returns
        non-empty, threads them onto a fresh ``RuntimeContext.custom``
        under the keys ``channel_prompt`` and ``channel_skill_ids``.
        Skill *bodies* (not just ids) get pre-loaded too so the loop
        can splice them into the per-turn system prompt without
        another disk hop. Failure to resolve is silent — default
        behaviour (no channel context) is preserved.

        Phase 2 multi-routing: ``loop`` is the per-profile
        ``AgentLoop`` resolved by ``_do_dispatch`` (so the right
        profile's memory is used to load skill bodies). Falls back
        to ``self.loop`` for backwards compat with older callers.
        """
        if adapter is None or not event.metadata:
            return DEFAULT_RUNTIME_CONTEXT
        channel_id = event.metadata.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id:
            return DEFAULT_RUNTIME_CONTEXT
        parent_id = event.metadata.get("parent_channel_id")
        parent = parent_id if isinstance(parent_id, str) and parent_id else None

        prompt: str | None = None
        skill_ids: list[str] = []
        try:
            prompt = adapter.resolve_channel_prompt(channel_id, parent)
        except Exception:  # noqa: BLE001 — resolution must never break dispatch
            logger.debug(
                "resolve_channel_prompt failed for channel_id=%s",
                channel_id, exc_info=True,
            )
        try:
            skill_ids = list(
                adapter.resolve_channel_skills(channel_id, parent) or []
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "resolve_channel_skills failed for channel_id=%s",
                channel_id, exc_info=True,
            )

        if not prompt and not skill_ids:
            return DEFAULT_RUNTIME_CONTEXT

        # Pre-load skill bodies so the agent loop doesn't need to do
        # disk I/O during system-prompt composition. Missing skill ids
        # are dropped silently — operators see "channel skill X not
        # found" only at config validation time.
        skill_bodies: list[tuple[str, str]] = []
        memory_source = loop if loop is not None else self.loop
        memory = getattr(memory_source, "memory", None)
        if memory is not None and skill_ids:
            for sid in skill_ids:
                try:
                    body = memory.load_skill_body(sid)
                except Exception:  # noqa: BLE001 — defensive
                    body = ""
                if body:
                    skill_bodies.append((sid, body))

        custom: dict[str, Any] = {}
        if prompt:
            custom["channel_prompt"] = prompt
        if skill_ids:
            custom["channel_skill_ids"] = skill_ids
        if skill_bodies:
            custom["channel_skill_bodies"] = skill_bodies
        custom["channel_id"] = channel_id
        # 2026-05-13 — Profile handoff auto-swap gating.
        # Gateway sessions disable auto-swap by default because channel
        # identity is part of the user's contract (a Telegram bot
        # registered as ``@stocks_bot`` should not silently switch to a
        # different profile mid-conversation). Adapters that DO want
        # auto-swap (e.g. personal Discord DM bots) set
        # ``BaseChannelAdapter.auto_swap_enabled = True`` either on the
        # subclass or per-instance config.
        custom["_is_gateway_session"] = True
        custom["_channel_auto_swap_enabled"] = bool(
            getattr(adapter, "auto_swap_enabled", False),
        )
        return RuntimeContext(custom=custom)

    def _maybe_session_banner(
        self, session_id: str, profile_id: str, loop: Any, chat_cwd: str | None,
    ) -> str:
        """Return a one-line session banner the first time a session is
        seen this process, ``""`` afterwards or when not enabled.

        A7 (gateway-vs-CLI parity). The CLI prints model / profile / cwd
        at session start; the gateway sent nothing. This banner is
        prepended (italic) to the first reply.

        Opt-in: shown only when ``display.gateway_banner.enabled = true``
        (or the shorthand ``display.gateway_banner: true``). Default off
        is a deliberate product call — a banner on every bot's first
        reply is noise for customer-facing deployments, so the operator
        enables it for the personal-assistant use case. Best-effort: a
        banner failure must never replace the reply.
        """
        try:
            display = (self._display_cfg or {}).get("display") or {}
            gb = display.get("gateway_banner")
            enabled = gb is True or (
                isinstance(gb, dict) and gb.get("enabled") is True
            )
            if not enabled:
                return ""
            if session_id in self._banner_shown:
                return ""
            self._banner_shown.add(session_id)
            model = str(
                getattr(getattr(loop, "config", None), "model", "") or ""
            )
            cwd = chat_cwd or os.getcwd()
            parts = [f"OpenComputer · profile={profile_id}"]
            if model:
                parts.append(f"model={model}")
            parts.append(f"cwd={cwd}")
            banner = " · ".join(parts)
            try:
                mem = getattr(loop, "memory", None)
                if mem is not None:
                    banner += f" · skills={len(mem.list_skills())}"
            except Exception:  # noqa: BLE001 — skill count is decoration
                pass
            return banner
        except Exception:  # noqa: BLE001 — banner must never break dispatch
            logger.debug("session banner render failed", exc_info=True)
            return ""

    async def _safe_lifecycle_hook(self, coro) -> None:
        """Fire-and-forget lifecycle hook with error swallowing.

        Hermes channel-port (PR 2 Task 2.2). Hooks are decoration —
        their failure must never affect the user's reply. We log at
        DEBUG so the failures are surfaced for adapter authors but
        invisible at INFO+ in normal operation.
        """
        try:
            await coro
        except Exception:  # noqa: BLE001
            logger.debug("lifecycle hook raised", exc_info=True)

    @staticmethod
    def _populate_session_usage(
        custom: dict[str, Any], db: Any, session_id: str,
    ) -> None:
        """Fill per-session usage counters into ``custom`` so ``/usage``
        and ``/context`` show real data on the gateway.

        A3 — these commands were written CLI-shaped: they read live
        counters off ``runtime.custom`` that only the single-session CLI
        loop maintains. ``SessionDB.session_usage_summary`` is the
        durable per-session source; reading it here lets the gateway
        bypass path supply the same numbers. Best-effort — a DB error
        leaves the keys absent and the command degrades gracefully.
        """
        try:
            summary = db.session_usage_summary(session_id)
        except Exception:  # noqa: BLE001 — usage display must never raise
            return
        if summary is None:
            return
        custom["session_tokens_in"] = summary.input_tokens
        custom["session_tokens_out"] = summary.output_tokens
        custom["session_cache_read"] = summary.cache_read_tokens
        custom["session_cache_write"] = summary.cache_write_tokens
        custom["session_compactions"] = summary.compactions_count
        if summary.cost_usd is not None:
            custom["session_cost_usd"] = summary.cost_usd

    async def _maybe_bypass_running_guard(
        self, event, session_id: str, profile_id: str, loop: Any = None,
    ) -> str | None:
        """Detect + execute a slash command inline on the gateway.

        Two class attributes opt a slash command into gateway execution:

        - ``bypass_running_guard = True`` (Wave 6.E.6, Hermes parity) —
          ``/kanban`` and friends skip the per-session lock so a board
          read/write reaches the DB even mid-flight.
        - ``gateway_safe = True`` (A3, gateway-vs-CLI parity Wave 1) —
          the command is safe to run inline on the gateway. Without
          this flag a slash command falls through to the model as plain
          text — which is why ``/status``, ``/plan`` etc. silently
          no-op'd on Telegram before A3.

        Either flag routes the command here. Both run pre-lock; spec
        requires gateway-safe commands to be quick.

        ``loop`` is the resolved per-profile :class:`AgentLoop`; it lets
        the runtime built here carry ``session_db`` / ``model`` /
        ``active_profile_id`` so read-only commands (``/history``,
        ``/agents``, ``/status``) show real data instead of placeholders.

        Returns the command's text output if dispatched, or None if the
        message is not a gateway-runnable slash command (caller proceeds
        with the normal locked path).
        """
        text = event.text or ""
        if not text.startswith("/"):
            return None
        from opencomputer.agent.slash_dispatcher import parse_slash
        parsed = parse_slash(text)
        if parsed is None:
            return None
        name, args = parsed
        from opencomputer.plugins.registry import registry as _plugin_registry
        cmd = _plugin_registry.slash_commands.get(name)
        if cmd is None:
            return None
        # Kanban-Goals v2 (2026-05-08) — refuse /goal <text> when this
        # session has a turn in flight; status / pause / resume / clear
        # remain unrestricted. Checked before the bypass_running_guard
        # gate so /goal-set is short-circuited even though /goal isn't
        # itself a bypass command. Non-set /goal subcommands fall
        # through to the normal locked dispatch path.
        if name == "goal":
            refused = await self._goal_midrun_check(
                session_id=session_id, args=list(args),
            )
            if refused is not None:
                return refused
        if not (
            getattr(cmd, "bypass_running_guard", False)
            or getattr(cmd, "gateway_safe", False)
        ):
            return None
        # Build a runtime context with channel info so the command can
        # read platform / chat_id / thread_id for things like
        # /kanban auto-subscribe.
        custom: dict[str, Any] = {
            "platform": event.platform.value if event.platform else None,
            "chat_id": event.chat_id,
            "session_id": session_id,
            "profile_id": profile_id,
            # A3 — ``active_profile_id`` is the key profile-aware commands
            # read; the gateway resolves the same value as ``profile_id``.
            "active_profile_id": profile_id,
        }
        if event.metadata:
            for k in ("thread_id", "user_id", "message_id"):
                v = event.metadata.get(k)
                if v is not None:
                    custom[k] = v
        # A3 — give read-only gateway-safe commands the loop-backed
        # context they need to show real data: ``session_db`` for
        # /history + /agents, ``model`` for /status, and the per-session
        # usage totals for /usage + /context. Best-effort — a missing
        # attribute just leaves the command in its degraded (placeholder)
        # rendering, never raises.
        if loop is not None:
            db = getattr(loop, "db", None)
            if db is not None:
                custom["session_db"] = db
                self._populate_session_usage(custom, db, session_id)
            try:
                custom["model"] = loop.config.model.model
            except Exception:  # noqa: BLE001 — model line is decoration
                pass
            # A8 — the handoff doc generator needs a provider adapter;
            # the loop caches one after its first turn. None is fine —
            # /handoff falls back to a doc-less swap.
            adapter_for_handoff = getattr(loop, "_handoff_provider_adapter", None)
            if adapter_for_handoff is not None:
                custom["_handoff_provider_adapter"] = adapter_for_handoff
        # A3 — /platforms reads the live adapter roster. The dispatcher
        # owns it (``_adapters_by_platform``); surface it so the command
        # lists real channels instead of "run oc gateway".
        roster = getattr(self, "_adapters_by_platform", None)
        if roster:
            custom["active_platforms"] = sorted(roster.keys())
        runtime = RuntimeContext(custom=custom)
        # 2026-05-08 — Hermes Doc-2 gateway hooks: command:<slug>.
        # Fire-and-forget so a slow handler never delays slash dispatch.
        # The wildcard "command:*" pattern in HOOK.yaml matches every
        # slug (handled in event_hooks.GatewayHook.matches).
        try:
            from opencomputer.gateway.event_hooks import (
                engine as _gw_hooks_engine_cmd,
            )
            asyncio.create_task(
                _gw_hooks_engine_cmd.fire(
                    f"command:{name}",
                    {
                        "platform": event.platform.value if event.platform else None,
                        "user_id": event.chat_id,
                        "session_id": session_id,
                        "command": name,
                        "args": args,
                    },
                ),
                name=f"gw-hook-command-{name}",
            )
            # 2026-05-08 — Hermes Doc-2 gateway hooks: session:reset
            # fires when a slash command rotates the session id. The
            # canonical commands are /new, /reset, /clear (they all
            # alias to the same handler — see cli_ui/slash.py:72).
            if name in {"new", "reset", "clear"}:
                from opencomputer.gateway.event_hooks import (
                    SESSION_RESET as _GW_SESSION_RESET,
                )
                asyncio.create_task(
                    _gw_hooks_engine_cmd.fire(
                        _GW_SESSION_RESET,
                        {
                            "platform": event.platform.value if event.platform else None,
                            "user_id": event.chat_id,
                            "session_key": session_id,
                            "command": name,
                        },
                    ),
                    name=f"gw-hook-session-reset-{name}",
                )
        except Exception:  # noqa: BLE001
            logger.debug("gateway command:%s fire failed", name, exc_info=True)
        try:
            result = await cmd.execute(args, runtime)
        except Exception as exc:  # noqa: BLE001
            logger.exception("bypass slash %s raised", name)
            return f"/{name}: {type(exc).__name__}: {exc}"
        return result.output if result is not None else None

    def _install_goal_banner_callback(
        self,
        *,
        loop,
        session_id: str,
        adapter,
        chat_id: str,
    ) -> None:
        """Wire ``loop._fire_goal_banner`` for ``session_id`` to send a
        chat message via ``adapter``.

        The callback runs inside the agent loop (sync, no event loop in
        scope by signature). We need to schedule an async ``adapter.send``
        — capture the running loop's event-loop here and use
        ``call_soon_threadsafe`` + ``ensure_future`` to fire-and-forget.
        Adapter errors are swallowed — banner rendering must never
        wedge the agent loop.
        """
        from opencomputer.cli_ui.goal_banner import format_banner

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        def _cb(*, session_id, kind, verdict, goal):
            try:
                text = format_banner(kind=kind, verdict=verdict, goal=goal)
            except Exception:  # noqa: BLE001
                return
            if running is None or running.is_closed():
                return
            try:
                # Schedule from whatever thread fires the banner.
                asyncio.run_coroutine_threadsafe(
                    self._safe_send_goal_banner(adapter, chat_id, text),
                    running,
                )
            except Exception:  # noqa: BLE001
                pass

        loop.set_goal_banner_callback(session_id, _cb)

    async def _safe_send_goal_banner(
        self, adapter, chat_id: str, text: str,
    ) -> None:
        """Best-effort banner-as-message send. Errors swallowed."""
        try:
            await adapter.send(chat_id, text)
        except Exception:  # noqa: BLE001
            logger.debug("goal banner send failed", exc_info=True)

    async def _goal_midrun_check(
        self,
        *,
        session_id: str,
        args: list[str],
    ) -> str | None:
        """Refuse `/goal <text>` when an agent run is already in flight.

        Spec: docs/superpowers/specs/2026-05-08-kanban-goals-v2-design.md §3
        Gap D. The set form races with the in-flight continuation prompt
        ``_maybe_continue_goal`` is about to inject — refuse and tell
        the user to /stop first. Status / pause / resume / clear only
        touch control-plane state and remain unrestricted.

        Returns the refusal string when the dispatcher should short-
        circuit, otherwise None (caller proceeds normally).
        """
        if not args:
            return None  # status form (no text)
        sub = (args[0] or "").lower()
        if sub in {"status", "pause", "resume", "clear"}:
            return None
        # SET form. Check the active-runs marker (instrumented around
        # run_conversation in :meth:`_do_dispatch`).
        if session_id in self._active_runs:
            return (
                "/goal: agent is currently running — use /stop first, "
                "then set the new goal."
            )
        return None

    async def _typing_heartbeat(self, platform: str, chat_id: str) -> None:
        """Send typing indicator every 4s until cancelled."""
        adapter = self._adapters_by_platform.get(platform)
        if adapter is None:
            return
        try:
            while True:
                try:
                    await adapter.send_typing(chat_id)
                except Exception:
                    pass  # typing is best-effort
                await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Round 2a P-5 — channel-side approval prompt
    # ------------------------------------------------------------------

    async def _send_approval_prompt(
        self,
        session_id: str,
        claim: CapabilityClaim,
        scope: str | None,
    ) -> bool:
        """Channel-side ``PromptHandler`` registered on ConsentGate.

        Looks up the (adapter, chat_id) bound to ``session_id``, mints
        an opaque correlation token, registers it so a later button
        click can be mapped back to (session_id, capability_id), and
        asks the adapter to render the approval prompt with inline
        buttons. Returns True if the prompt was sent successfully so
        the gate knows to block waiting for the click.

        Adapters without an inline-button surface (no
        ``send_approval_request`` method) cause this to return False;
        the gate then auto-denies immediately rather than burning the
        timeout.
        """
        binding = self._session_channels.get(session_id)
        if binding is None:
            return False
        adapter, chat_id = binding
        if not hasattr(adapter, "send_approval_request"):
            return False

        # Critical fix: resolve the gate via the per-profile loop rather
        # than self.loop. self.loop is None when Dispatch was constructed
        # with router= (no loop=), causing silent auto-deny.
        pid = self._session_profiles.get(session_id, "default")
        _profile_loop = self._router._loops.get(pid)
        gate = getattr(_profile_loop, "_consent_gate", None)
        if gate is None:
            logger.debug(
                "approval prompt: session=%s profile=%s has no _consent_gate; "
                "auto-deny (gate not loaded or profile not found)",
                session_id, pid,
            )
            return False

        token = uuid.uuid4().hex[:24]
        # Hermes parity: pass session_id so render_prompt can surface
        # any Tirith findings stashed by the loop's pre-consent scan.
        prompt_text = gate.render_prompt(claim, scope, session_id=session_id)
        try:
            result = await adapter.send_approval_request(
                chat_id=chat_id,
                prompt_text=prompt_text,
                request_token=token,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "approval prompt failed for session=%s capability=%s: %s",
                session_id, claim.capability_id, exc,
            )
            return False
        if result is not None and getattr(result, "success", False) is False:
            logger.warning(
                "approval prompt rejected by adapter for session=%s "
                "capability=%s: %s",
                session_id, claim.capability_id,
                getattr(result, "error", "<no detail>"),
            )
            return False
        # Only register the token after the prompt is on the wire;
        # otherwise a synchronous failure would leave a dangling entry
        # that a stale click could pick up against a future request.
        self._approval_tokens[token] = (session_id, claim.capability_id)
        # M3 #5 telemetry — record that this turn paid an async-consent
        # round-trip, so mechanism #5 fires only when consent actually
        # happened (not structurally on every turn).
        self._consent_prompt_counts[session_id] = (
            self._consent_prompt_counts.get(session_id, 0) + 1
        )
        return True

    async def _maybe_resolve_consent_text_reply(
        self, *, session_id: str, text: str,
    ) -> bool:
        """Resolve a pending consent prompt with a bare-text user reply.

        Hermes-parity for the "type yes/no in chat" flow. Returns True
        iff the reply was consumed (i.e., a pending consent request was
        resolved). False means the caller should continue to route the
        message through the agent loop normally.

        Logic:
        - Look up the per-profile loop's :class:`ConsentGate`.
        - If no pending request keyed on ``session_id`` exists, return
          False immediately. The reply is ordinary user input.
        - Classify ``text`` via :func:`opencomputer.security.approval_keywords.classify_reply`.
          Strict single-token match — anything ambiguous (e.g.
          ``"yes please"``) returns None and is treated as a normal
          message.
        - When approve/deny: resolve every pending key for this
          session — there's typically only one but the registry shape
          permits multiples. ``persist=False`` semantics mirror
          "allow once" for approve and a plain deny for deny.

        Never raises — callers see a False on any internal error.
        """
        from opencomputer.security.approval_keywords import classify_reply

        # Find the gate via the same indirection as
        # ``_handle_approval_click``: profile → router._loops → gate.
        try:
            profile = self._session_profiles.get(session_id, "default")
            loop = self._router._loops.get(profile) if self._router else None
            gate = getattr(loop, "_consent_gate", None)
        except Exception:  # noqa: BLE001
            return False
        if gate is None:
            return False

        pending = getattr(gate, "_pending_requests", None)
        if not pending:
            return False
        # Find any pending key for this session_id.
        target_keys = [k for k in pending if k[0] == session_id]
        if not target_keys:
            return False

        verdict = classify_reply(text)
        if verdict is None:
            return False

        decision = verdict == "approve"
        persist = False  # text replies always allow_once / deny — explicit
        # "always" requires the button surface, not a single keyword.
        for key in target_keys:
            session, capability_id = key
            try:
                gate.resolve_pending(
                    session_id=session,
                    capability_id=capability_id,
                    decision=decision,
                    persist=persist,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "consent text-reply resolve_pending failed for "
                    "session=%s capability=%s",
                    session, capability_id, exc_info=True,
                )
                continue
            logger.info(
                "consent text-reply: session=%s capability=%s verdict=%s",
                session, capability_id, verdict,
            )
        return True

    async def _handle_approval_click(self, verb: str, token: str) -> None:
        """Adapter-side approval-callback receiver.

        Translates the opaque ``(verb, token)`` tuple back into a
        ``ConsentGate.resolve_pending`` call. Stale clicks (token not
        in the registry) are dropped quietly.
        """
        binding = self._approval_tokens.pop(token, None)
        if binding is None:
            logger.info(
                "approval click for unknown token=%s — stale/duplicate, ignored",
                token,
            )
            return
        session_id, capability_id = binding
        # Resolve gate via the per-profile loop (same fix as
        # _send_approval_prompt: self.loop is None on router= paths).
        _pid = self._session_profiles.get(session_id, "default")
        _click_loop = self._router._loops.get(_pid)
        gate = getattr(_click_loop, "_consent_gate", None)
        if gate is None:
            return
        # Hermes parity: 4th verb 'session' grants for the rest of the
        # session only — dispatched to the in-memory cache via
        # resolve_pending(... session_scoped=True).
        session_scoped = False
        if verb == "once":
            decision, persist = True, False
        elif verb == "session":
            decision, persist, session_scoped = True, False, True
        elif verb == "always":
            decision, persist = True, True
        elif verb == "deny":
            decision, persist = False, False
        else:
            logger.warning("approval click unknown verb=%s token=%s", verb, token)
            return
        resolved = gate.resolve_pending(
            session_id=session_id,
            capability_id=capability_id,
            decision=decision,
            persist=persist,
            session_scoped=session_scoped,
        )
        if not resolved:
            logger.info(
                "approval click verb=%s session=%s capability=%s "
                "had no pending request — stale callback",
                verb, session_id, capability_id,
            )


    # ─── /background completion notifier ─────────────────────────────

    def bind_main_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the gateway's main-thread asyncio loop.

        Background-job worker threads call back synchronously into the
        completion notifier; the notifier needs the gateway's loop to
        schedule ``adapter.send`` on it via ``run_coroutine_threadsafe``.
        Called once by ``Gateway.serve_forever`` before the daemon enters
        the serve-forever wait.
        """
        self._main_loop = loop

    def background_completion_notifier(
        self, job: Any
    ) -> None:
        """Sync notifier invoked from the background-job worker thread
        when a job transitions to ``complete`` or ``error``.

        Routes the result back to the originating channel by:
        1. Looking up ``_session_channels[job.parent_session_id]`` to
           find the (adapter, chat_id) pair.
        2. Building the user-facing summary via
           :func:`_format_background_completion_text`.
        3. Scheduling ``adapter.send(chat_id, summary)`` onto the gateway's
           loop using ``run_coroutine_threadsafe`` (worker thread cannot
           directly await on the gateway's loop).

        Failure-isolated. The registry swallows any exception this raises;
        a misbehaving notifier must never tear down the worker thread.
        """
        sid = getattr(job, "parent_session_id", None)
        if not sid:
            return
        binding = self._session_channels.get(sid)
        if binding is None:
            logger.debug(
                "/background completion: session=%s has no channel binding "
                "(non-gateway path or session evicted) — skipping push",
                sid,
            )
            return
        adapter, chat_id = binding
        if not hasattr(adapter, "send"):
            return
        main_loop = getattr(self, "_main_loop", None)
        if main_loop is None:
            logger.debug(
                "/background completion: no main loop bound — skipping push"
            )
            return
        text = _format_background_completion_text(job)
        try:
            asyncio.run_coroutine_threadsafe(
                adapter.send(chat_id, text), main_loop
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "/background completion: schedule failed for session=%s",
                sid, exc_info=True,
            )


def _format_background_completion_text(job: Any) -> str:
    """Render the user-facing summary for a finished background job.

    Single-line head with the job id + status + first 60 chars of prompt,
    followed by the body (result or error). Kept compact so chat surfaces
    don't explode on long outputs.
    """
    head = (job.prompt or "").splitlines()[0]
    if len(head) > 60:
        head = head[:57] + "…"
    if job.status == "complete":
        body = job.result or "(empty response)"
        return f"✓ background {job.job_id} done — {head}\n\n{body}"
    return (
        f"✗ background {job.job_id} failed — {head}\n\n"
        f"error: {job.error or '(no detail)'}"
    )


__all__ = [
    "Dispatch",
    "_format_background_completion_text",
    "_format_user_facing_error",
    "session_id_for",
]
