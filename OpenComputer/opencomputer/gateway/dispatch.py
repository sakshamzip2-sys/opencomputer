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
import time
import uuid
from typing import TYPE_CHECKING, Any

from opencomputer.agent.loop import AgentLoop
from plugin_sdk.core import MessageEvent, ProcessingOutcome
from plugin_sdk.runtime_context import (
    DEFAULT_RUNTIME_CONTEXT,
    RequestContext,
    RuntimeContext,
)

if TYPE_CHECKING:
    from opencomputer.gateway.agent_router import AgentRouter
    from opencomputer.gateway.binding_resolver import BindingResolver
    from opencomputer.gateway.channel_directory import ChannelDirectory
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
    """Fire-and-forget telemetry write. Never propagates exceptions."""
    try:
        from opencomputer.agent.turn_outcome_recorder import TurnOutcomeRecorder
        TurnOutcomeRecorder(db).record(sig)
    except Exception as e:  # noqa: BLE001 — telemetry must never block
        logger.warning("outcome recording failed: %s", e)


def _format_user_facing_error(exc: Exception) -> str:
    """Render an exception from the agent loop as a one-liner the user
    can read on a chat surface.

    The full traceback is logged via ``logger.exception`` at the call
    site — this only shapes what the *user* sees on Telegram / Discord
    / etc. Keying off ``status_code`` works for Anthropic, OpenAI, and
    httpx exceptions uniformly; class-name fallback handles network-
    layer errors that never produced an HTTP response.

    Pure function (no Dispatch state) so unit tests + downstream
    error-presentation code can call it directly.
    """
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)

    # Network-layer — connection refused, DNS failure, TCP timeout. No HTTP
    # status was ever produced. Class-name match because httpx + the SDKs
    # use these names without a shared base class we can isinstance-check.
    if name in {
        "APIConnectionError", "APITimeoutError", "ConnectError",
        "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    }:
        return ("Can't reach the model server right now (network issue). "
                "Try again in a moment.")

    if status == 429 or name == "RateLimitError":
        return ("Rate-limited by the model provider. "
                "Try again in a few seconds.")

    if status in (401, 403) or name in {
        "AuthenticationError", "PermissionDeniedError",
    }:
        return ("Authentication failed — your API key may be invalid or "
                "your provider proxy is misconfigured.")

    if isinstance(status, int) and 500 <= status < 600:
        return (f"The model service returned an error ({status}). "
                "This is usually transient — try again in a moment.")

    # Unknown / unmapped — keep the class name so logs can be grepped,
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
    ) -> None:
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
        # Per-(profile_id, session_id) lock map — multi-profile correct.
        # Same chat across two profiles no longer interleaves.
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
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
        self._burst_window_seconds: float = float(
            cfg.get("photo_burst_window", 0.8)
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

    def register_adapter(self, platform: str, adapter) -> None:
        self._adapters_by_platform[platform] = adapter
        # Round 2a P-5 — if the adapter exposes the approval-button
        # surface, route its callbacks through us so we can translate
        # opaque tokens back into ``ConsentGate.resolve_pending`` calls.
        if hasattr(adapter, "set_approval_callback"):
            adapter.set_approval_callback(self._handle_approval_click)

    def _session_id_for(self, event: MessageEvent) -> str:
        """Stable session id: hash(platform + chat_id[, thread_hint]).

        ``thread_hint`` (Item 21) comes from ``event.metadata["thread_hint"]``
        if set, letting cron / non-conversational paths route output to
        a separate session within the same chat. Default behaviour
        (no hint) keeps existing chats on a single session forever.
        """
        thread_hint: str | None = None
        if event.metadata:
            raw = event.metadata.get("thread_hint")
            if isinstance(raw, str) and raw.strip():
                thread_hint = raw.strip()
        return session_id_for(event.platform.value, event.chat_id, thread_hint)

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

        session_id = self._session_id_for(event)
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
        """
        # Phase 3 Task 3.3: per-event profile resolution via the
        # BindingResolver. Falls back to "default" when no resolver
        # was wired — preserves the legacy behaviour for callers /
        # tests that construct Dispatch without a bindings file.
        profile_id = (
            self._resolver.resolve(event) if self._resolver is not None else "default"
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
        message_id: str | None = None
        if event.metadata:
            raw_id = event.metadata.get("message_id")
            if isinstance(raw_id, str | int) and raw_id != "":
                message_id = str(raw_id)
        if adapter is not None:
            asyncio.create_task(
                self._safe_lifecycle_hook(
                    adapter.on_processing_start(event.chat_id, message_id)
                )
            )
        # Per-(profile_id, session_id) lock keys make multi-profile
        # correct: same chat_id across two profiles no longer
        # interleaves through the same lock.
        lock_key = (profile_id, session_id)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        outcome: ProcessingOutcome = ProcessingOutcome.SUCCESS
        async with lock:
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
            # Phase 2 audit G1: bind the per-task ``current_profile_home``
            # ContextVar around ``run_conversation`` so ``_home()`` and
            # any PluginAPI lazy paths resolve to the right profile for
            # the duration of this dispatch.
            from plugin_sdk.profile_context import set_profile
            # P0-4: capture wall-clock around run_conversation so we
            # can record a turn_outcomes row at the end.
            turn_start_ts = time.time()
            try:
                with set_profile(profile_home):
                    if self._plugin_api is not None:
                        with self._plugin_api.in_request(request_ctx):
                            result = await loop.run_conversation(
                                user_message=user_message,
                                session_id=session_id,
                                images=images,
                                runtime=runtime,
                            )
                    else:
                        result = await loop.run_conversation(
                            user_message=user_message,
                            session_id=session_id,
                            images=images,
                            runtime=runtime,
                        )
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
                return result.final_message.content or None
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
                heartbeat.cancel()
                try:
                    await heartbeat
                except (asyncio.CancelledError, Exception):
                    pass
                # Hermes channel-port (PR 2 Task 2.2): fire
                # on_processing_complete after the turn settles, with
                # the outcome captured above. Fire-and-forget so a
                # failing reaction send doesn't mask the actual reply.
                if adapter is not None:
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
        return RuntimeContext(custom=custom)

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
        prompt_text = gate.render_prompt(claim, scope)
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
        if verb == "once":
            decision, persist = True, False
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
        )
        if not resolved:
            logger.info(
                "approval click verb=%s session=%s capability=%s "
                "had no pending request — stale callback",
                verb, session_id, capability_id,
            )


__all__ = ["Dispatch", "session_id_for", "_format_user_facing_error"]
