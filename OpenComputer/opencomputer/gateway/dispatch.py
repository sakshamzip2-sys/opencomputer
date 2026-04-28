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
import hashlib
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from opencomputer.agent.loop import AgentLoop
from plugin_sdk.core import MessageEvent, ProcessingOutcome
from plugin_sdk.runtime_context import RequestContext

if TYPE_CHECKING:
    from opencomputer.gateway.channel_directory import ChannelDirectory
    from opencomputer.plugins.loader import PluginAPI
    from plugin_sdk.consent import CapabilityClaim

logger = logging.getLogger("opencomputer.gateway.dispatch")


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
        loop: AgentLoop,
        plugin_api: PluginAPI | None = None,
        channel_directory: ChannelDirectory | None = None,
    ) -> None:
        self.loop = loop
        # One lock per chat_id — prevents interleaved turns from the same chat
        self._locks: dict[str, asyncio.Lock] = {}
        # Adapter reference (set by Gateway) so we can send typing indicators
        self._adapters_by_platform: dict = {}
        # Task I.9: the shared PluginAPI whose ``in_request`` we wrap
        # each dispatch with. ``None`` preserves backwards compat —
        # existing CLI test paths constructing Dispatch without a
        # plugin_api keep working.
        self._plugin_api: PluginAPI | None = plugin_api
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

        Also starts a periodic typing-indicator heartbeat on the source
        channel so the user sees "..." while the agent thinks.

        Task I.9 — when a ``plugin_api`` is bound, each dispatch wraps
        the ``run_conversation`` call in ``plugin_api.in_request(ctx)``
        so plugins can query their per-request scope. Empty-text
        early-return skips the wrap entirely (no work → no scope).

        Task II.3 — before touching the agent loop, we record this
        event into the channel directory so future send-message tools
        can resolve friendly names instead of raw chat ids. Failures
        are swallowed at WARNING level — the directory is best-effort
        metadata and must never take dispatch down.
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
        if not event.text.strip():
            return None
        session_id = self._session_id_for(event)
        # Round 2a P-5 — record the (adapter, chat_id) binding so a
        # consent prompt later in this turn can find the right surface
        # to ask the user on. Best-effort: missing adapter = legacy
        # CLI/wire path, no harm done.
        adapter = self._adapters_by_platform.get(
            event.platform.value if event.platform else ""
        )
        if adapter is not None:
            self._session_channels[session_id] = (adapter, event.chat_id)
        # Hermes channel-port (PR 2 Task 2.2): fire on_processing_start
        # BEFORE acquiring the per-chat lock so a fast-clicking user
        # sees the 👀 reaction even if the previous turn is still
        # holding the lock. The hook is fire-and-forget — failures
        # never affect the reply path.
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
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        outcome: ProcessingOutcome = ProcessingOutcome.SUCCESS
        async with lock:
            # Start a typing heartbeat (Telegram's typing state expires after
            # ~5s, so we re-send every 4s until the turn completes).
            heartbeat = asyncio.create_task(
                self._typing_heartbeat(event.platform.value, event.chat_id)
            )
            # Task I.9: build a per-request ctx. The channel is the
            # MessageEvent platform; user_id is the chat_id (the
            # channel-specific user-visible identifier the dispatcher
            # already keys on); session_id is the deterministic hash
            # computed above. ``time.monotonic()`` is the canonical
            # request-start clock (used for request-timing metrics).
            request_ctx = RequestContext(
                request_id=str(uuid.uuid4()),
                channel=event.platform.value if event.platform else None,
                user_id=event.chat_id,
                session_id=session_id,
                started_at=time.monotonic(),
            )
            try:
                if self._plugin_api is not None:
                    with self._plugin_api.in_request(request_ctx):
                        result = await self.loop.run_conversation(
                            user_message=event.text,
                            session_id=session_id,
                        )
                else:
                    result = await self.loop.run_conversation(
                        user_message=event.text,
                        session_id=session_id,
                    )
                return result.final_message.content or None
            except Exception as e:  # noqa: BLE001
                # Always log full traceback for debugging; user only
                # sees the one-liner from _format_user_facing_error so
                # SDK internals / prompt fragments don't leak to chat.
                logger.exception("dispatch error for %s: %s", event.platform, e)
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

        gate = getattr(self.loop, "_consent_gate", None)
        if gate is None:
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
        gate = getattr(self.loop, "_consent_gate", None)
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
