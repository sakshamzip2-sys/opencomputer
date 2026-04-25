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
from typing import Any

logger = logging.getLogger(__name__)


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
        """Bridge: call AgentLoop.run_conversation, capture final message.

        Signature confirmed from opencomputer/agent/loop.py:
            async def run_conversation(
                self,
                user_message: str,
                session_id: str | None = None,
                ...
            ) -> ConversationResult

        ConversationResult.final_message is a Message with a .content str attribute.
        """
        loop_inst = self._loop_instance
        result = await loop_inst.run_conversation(
            user_message=content,
            session_id=self.session_id,
        )
        # ConversationResult.final_message.content is a str
        final_message = result.final_message
        final_content: str = getattr(final_message, "content", "") or ""
        self._messages.append({"role": "assistant", "content": final_content})
        return final_content

    async def cancel(self) -> bool:
        """Signal cancellation. Returns True if a prompt was in flight."""
        was_running = not self._cancel_event.is_set()
        self._cancel_event.set()
        return was_running

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
