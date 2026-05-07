"""ACPSession — per-session state for ACP-driven runs.

Wraps OpenComputer's AgentLoop and bridges streaming events back to the
ACP client via the JSON-RPC notification channel.

Tool calls flow through OC's existing PluginAPI + ConsentGate (F1).
The IDE acts as the user for consent prompts (delegated tier — IDE
notifies user via its own UI when needed).

AgentLoop.run_conversation signature (confirmed from opencomputer/agent/loop.py):

    async def run_conversation(
        self,
        user_message: str,
        session_id: str | None = None,
        system_override: str | None = None,
        runtime: RuntimeContext | None = None,
        stream_callback=None,
        system_prompt_override: str | None = None,
    ) -> ConversationResult

ConversationResult fields: final_message (Message), messages, session_id,
iterations, input_tokens, output_tokens. Message.content is str.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class QueuedMessage:
    """One pending user message buffered by ``ACPSession.queue``.

    Wave 5 T3 — port hermes-agent ``e27b0b765``. Frozen so the same
    instance can be shared between drain callers (the wire response and
    the agent loop both read it).
    """

    text: str


class ACPSession:
    """Per-ACP-session state. One per IDE-side session."""

    def __init__(
        self,
        *,
        session_id: str,
        send: Callable[[str, Any], None],
    ) -> None:
        self.session_id: str = session_id
        self._send = send  # notification sender (method, params) -> None
        self._cancel_event: asyncio.Event = asyncio.Event()
        self._loop_instance: Any = None  # lazy-imported AgentLoop
        self._messages: list[dict[str, Any]] = []  # in-memory transcript
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=256)  # buffered event log

        # Wave 5 T3 — Hermes-port /steer + /queue state.
        # ``is_running`` flips True when ``send_prompt`` is mid-flight so
        # /steer can detect "interrupt vs first-message" and /queue can
        # tell "drain after current turn" from "fire next turn".
        # ``is_interrupted`` flags that /steer was used; the agent loop
        # consumes ``pending_user_text`` via consume_pending_user_text on
        # the next turn entry. ``queued`` is a FIFO of QueuedMessage —
        # ``drain_queue`` returns + clears, called when a turn ends.
        self.is_running: bool = False
        self.is_interrupted: bool = False
        self.pending_user_text: str | None = None
        self.queued: list[QueuedMessage] = []

        # PR-A Feature 3 (2026-05-07) — per-session tool gates set by
        # ACP ``setSessionPermissions``. ``denied_tools`` is consulted
        # by the agent loop's ``_dispatch_tool_calls``; ``allowed_tools``
        # is descriptive metadata for the IDE display (not currently
        # enforced — denylist is the security gate).
        self.allowed_tools: frozenset[str] = frozenset()
        self.denied_tools: frozenset[str] = frozenset()

    def emit_event(self, method: str, params: Any) -> None:
        """Send a JSON-RPC notification to the ACP client and buffer in event_queue."""
        self._send(method, params)
        try:
            self.event_queue.put_nowait({"method": method, "params": params})
        except asyncio.QueueFull:
            pass  # bounded at 256 — drop silently when full

    async def _ensure_loop(self) -> None:
        """Lazy-construct AgentLoop on first prompt."""
        if self._loop_instance is not None:
            return
        # Lazy import to avoid circular dependencies at module load time.
        # AgentLoop requires a provider; resolve from the active config using
        # the same provider resolution path as the chat CLI command.
        from opencomputer.agent.config import Config
        from opencomputer.agent.config_store import load_config
        from opencomputer.agent.loop import AgentLoop

        cfg: Config = load_config()
        # Resolve provider via the plugin registry (mirrors cli.py _resolve_provider).
        try:
            from opencomputer.plugins.registry import registry as plugin_registry

            provider_name = cfg.model.provider
            registered = plugin_registry.providers.get(provider_name)
            if registered is not None:
                provider = registered() if isinstance(registered, type) else registered
            else:
                # Fallback: attempt a direct import of the bundled anthropic provider.
                from opencomputer.providers.anthropic import (
                    AnthropicProvider,  # type: ignore[import]
                )

                provider = AnthropicProvider()
        except Exception:
            logger.warning("acp: provider resolution failed; trying anthropic directly")
            try:
                from opencomputer.providers.anthropic import (
                    AnthropicProvider,  # type: ignore[import]
                )

                provider = AnthropicProvider()
            except Exception as exc:
                raise RuntimeError(
                    f"acp: could not resolve any provider for session {self.session_id}: {exc}"
                ) from exc

        self._loop_instance = AgentLoop(provider=provider, config=cfg)

    async def send_prompt(self, content: str) -> dict[str, Any]:
        """Run a prompt through AgentLoop. Emit streaming notifications.
        Return the final assistant message + stop_reason.
        """
        await self._ensure_loop()
        self._cancel_event.clear()

        self._messages.append({"role": "user", "content": content})
        self._send("session/promptStart", {"sessionId": self.session_id})

        try:
            # AgentLoop.run_conversation is async; honor cancel_event via wait_for.
            run_task = asyncio.create_task(self._run_conversation(content))
            cancel_task = asyncio.create_task(self._cancel_event.wait())
            done, pending = await asyncio.wait(
                [run_task, cancel_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
            if cancel_task in done:
                self._send("session/cancelled", {"sessionId": self.session_id})
                return {"sessionId": self.session_id, "cancelled": True}
            result = await run_task
        except Exception as exc:
            logger.exception("acp: prompt failed for session %s", self.session_id)
            self._send(
                "session/promptError",
                {"sessionId": self.session_id, "error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        finally:
            self._send("session/promptDone", {"sessionId": self.session_id})

        return {
            "sessionId": self.session_id,
            "content": result,
            "messageCount": len(self._messages),
        }

    async def _run_conversation(self, content: str) -> str:
        """Bridge: call AgentLoop.run_conversation, capture final message."""
        from opencomputer.acp.tools import build_tool_complete, build_tool_start

        loop_inst = self._loop_instance
        session_ref = self  # capture for closure

        def _tool_cb(phase: str, tool_name: str, tool_call_id: str, data: Any) -> None:
            if phase == "start":
                session_ref.emit_event(
                    "session/toolStart",
                    build_tool_start(tool_name, tool_call_id, data),
                )
            elif phase == "complete":
                session_ref.emit_event(
                    "session/toolComplete",
                    build_tool_complete(tool_call_id, data),
                )

        result = await loop_inst.run_conversation(
            user_message=content,
            session_id=self.session_id,
            tool_callback=_tool_cb,
        )
        final_message = result.final_message
        final_content: str = getattr(final_message, "content", "") or ""
        self._messages.append({"role": "assistant", "content": final_content})
        return final_content

    async def cancel(self) -> bool:
        """Signal cancellation. Returns True if a prompt was in flight.

        PR-A Feature 1 (2026-05-07) bridge: ACP /cancel and /steer have
        distinct *intents* (cancel = abandon the prompt entirely; steer
        = stop tools and replan with new context) but they share the
        *mechanism* — both should signal the SteerRegistry's per-session
        cancel event so the agent loop's cancel-aware tool dispatcher
        interrupts in-flight async-yielding tools.

        ACP cancel additionally clears any pending steer text so the
        next-turn consume doesn't mistake "cancel" for "replan with X".
        """
        was_running = not self._cancel_event.is_set()
        self._cancel_event.set()
        # PR-A bridge — also signal the steer cancel mechanism so
        # async-yielding tools (Bash, WebFetch, browser, MCP) interrupt
        # mid-flight. Clear pending steer text + reset the registry's
        # cancel flag after firing so the next turn starts fresh.
        try:
            from opencomputer.agent.steer import (
                default_registry as _steer_registry,
            )
            # submit() with a sentinel-empty string is a no-op (drops
            # whitespace nudges), so we set the event directly via the
            # public API.
            _steer_registry.cancel_event(self.session_id).set()
            # Clear pending nudge — ACP cancel != steer-replan.
            _steer_registry.clear(self.session_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "acp: SteerRegistry cancel-event signal failed for "
                "session %s — agent loop may not interrupt mid-tool",
                self.session_id, exc_info=True,
            )
        return was_running

    # ─── Wave 5 T3 — /steer + /queue (Hermes e27b0b765 port) ─────────

    def mark_running(self) -> None:
        """Flip ``is_running`` True at the start of a prompt run.

        Called by ``send_prompt`` (via the ACP server) so /steer + /queue
        can detect "interrupt vs first-message" semantics.
        """
        self.is_running = True

    def mark_idle(self) -> None:
        """Flip ``is_running`` False after a prompt run finishes (success or error).

        Also clears the /steer interrupt flag — the next turn's entry is
        responsible for consuming ``pending_user_text`` via
        :meth:`consume_pending_user_text`.
        """
        self.is_running = False
        self.is_interrupted = False

    def update_permissions(
        self,
        *,
        allowed: frozenset[str] | None = None,
        denied: frozenset[str] | None = None,
    ) -> None:
        """Update per-session allow/deny tool lists.

        PR-A Feature 3 (2026-05-07): IDE clients call this via the
        ``setSessionPermissions`` ACP method. Race-safe by design —
        the agent loop reads ``denied_tools`` only at dispatch entry,
        so in-flight tool calls always complete; updates take effect
        on the next dispatch.

        ``None`` for either argument means "leave unchanged"; pass an
        empty frozenset to clear.
        """
        if allowed is not None:
            self.allowed_tools = allowed
        if denied is not None:
            self.denied_tools = denied

    async def steer(self, text: str) -> None:
        """Interrupt the current turn with new user text.

        Hermes contract: /steer fires regardless of running state. When
        the agent loop sees ``is_interrupted`` set on the next iteration,
        it consumes ``pending_user_text`` and treats it as the next user
        message. On idle sessions, the steered text simply queues as
        the next user message.

        PR-A Feature 1 (2026-05-07): also signals the SteerRegistry's
        per-session cancel event so the agent loop's cancel-aware tool
        dispatcher interrupts in-flight async-yielding tools mid-call.
        Without this bridge, ACP /steer would still rely on the legacy
        ``is_interrupted`` flag (consumed only at next-turn entry) and
        would NOT interrupt mid-tool-call work — which is the whole
        point of replan-with-context.
        """
        self.is_interrupted = True
        self.pending_user_text = text
        # PR-A Feature 1 bridge — ensure the agent loop reacts mid-flight.
        try:
            from opencomputer.agent.steer import (
                default_registry as _steer_registry,
            )
            _steer_registry.submit(self.session_id, text)
        except Exception:  # noqa: BLE001
            logger.warning(
                "acp: SteerRegistry.submit failed for session %s — "
                "legacy is_interrupted flag still applies",
                self.session_id, exc_info=True,
            )

    async def queue(self, text: str) -> None:
        """Append text to drain after the current turn finishes.

        On idle sessions, the queued text is treated as the next user
        message (drained by the next prompt entry).
        """
        self.queued.append(QueuedMessage(text=text))

    def drain_queue(self) -> list[QueuedMessage]:
        """Return + clear the pending queue. Called by the loop after each turn."""
        out, self.queued = list(self.queued), []
        return out

    def consume_pending_user_text(self) -> str | None:
        """Pop the /steer message exactly once. None when no steer pending."""
        text, self.pending_user_text = self.pending_user_text, None
        return text

    async def load_from_db(self) -> bool:
        """Try to restore session from SessionDB. Return True if found."""
        try:
            from opencomputer.agent.config_store import load_config
            from opencomputer.agent.state import SessionDB

            cfg = load_config()
            db = SessionDB(cfg.session.db_path)
            messages = db.get_messages(self.session_id)
            if messages:
                self._messages = [
                    {"role": m.role, "content": m.content or ""} for m in messages
                ]
                return True
        except Exception:
            logger.exception("acp: load_from_db failed for session %s", self.session_id)
        return False
