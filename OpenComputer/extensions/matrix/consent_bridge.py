"""Matrix → ConsentGate bridge (Wave 6.E.4).

Wires the matrix reaction approval primitive (PR #436) into the agent's
consent gate so any tool gated by ``ConsentGate.request_approval`` can
ask the user via a Matrix room reaction instead of (or in addition to)
the existing Telegram inline-button flow.

Flow:

1. ``register_matrix_consent_handler(gate, adapter, chat_id)`` builds
   a :data:`PromptHandler` closure and installs it via
   :meth:`ConsentGate.set_prompt_handler`.

2. When a tool calls ``await gate.request_approval(...)``, the gate
   invokes the closure. The closure:
   a. Posts the prompt message to ``chat_id`` and registers it in the
      adapter's ApprovalQueue.
   b. Spawns a background task that waits for the future to resolve
      (via ✅/❌ reaction or timeout) and then calls
      ``gate.resolve_pending(...)`` with the decision.
   c. Returns ``True`` immediately so the gate enters its "wait for
      resolve_pending" path.

The closure does NOT block the gate's request_approval call directly;
all awaiting happens in the background task. This matches the existing
Telegram flow (which also returns True immediately after dispatching
the inline-button message).

Configuration (in profile config.yaml):

    matrix:
      access_token: ...
      homeserver: ...
      inbound_sync: true            # required — see PR #436
      consent_handler: true         # opt-in — wires this bridge
      consent_chat_id: "!room:server"

Without ``consent_handler: true`` the bridge is a no-op even when matrix
is otherwise configured.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from plugin_sdk import CapabilityClaim

logger = logging.getLogger("opencomputer.matrix.consent_bridge")

# Default reaction emoji — same as the underlying ApprovalQueue.
ALLOW_EMOJI = "✅"
DENY_EMOJI = "❌"
# How long the user has to react before we auto-deny. Matches the
# default in extensions.matrix.approval.request_approval.
DEFAULT_TIMEOUT_SECONDS = 300.0


def _render_prompt(claim: CapabilityClaim, scope: str | None) -> str:
    """User-visible message body. Mirrors gate.render_prompt_message
    but adds a hint about the emoji-based response mechanism."""
    if scope:
        head = f"Allow `{claim.capability_id}` on `{scope}`?"
    else:
        head = f"Allow `{claim.capability_id}`?"
    return (
        f"{head}\n\n"
        f"React with {ALLOW_EMOJI} to approve, {DENY_EMOJI} to deny."
    )


def make_matrix_prompt_handler(
    *,
    gate,
    adapter,
    chat_id: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
):
    """Build a ConsentGate-compatible prompt handler.

    The returned coroutine has the
    :data:`opencomputer.agent.consent.gate.PromptHandler` signature::

        async (session_id, claim, scope) -> bool

    It returns True (prompt dispatched) and arranges for
    ``gate.resolve_pending`` to fire with the eventual decision. False
    is returned only when the matrix adapter or its approval_queue is
    not connected, so the gate can auto-deny without waiting.
    """

    async def _handler(
        session_id: str, claim: CapabilityClaim, scope: str | None,
    ) -> bool:
        if getattr(adapter, "approval_queue", None) is None:
            logger.warning(
                "matrix consent_bridge: adapter has no approval_queue; "
                "deny session=%s capability=%s",
                session_id, claim.capability_id,
            )
            return False
        if not getattr(adapter, "_inbound_enabled", False):
            logger.warning(
                "matrix consent_bridge: inbound_sync is OFF; matrix consent "
                "prompts cannot be resolved (no /sync polling). Set "
                "matrix.inbound_sync=true. Denying session=%s capability=%s",
                session_id, claim.capability_id,
            )
            return False

        prompt_text = _render_prompt(claim, scope)

        # Lazy import to keep the matrix package self-contained.
        from extensions.matrix.approval import request_approval

        async def _watch_and_resolve() -> None:
            try:
                allowed = await request_approval(
                    adapter,
                    chat_id,
                    prompt_text,
                    timeout=timeout,
                    allow_emoji=ALLOW_EMOJI,
                    deny_emoji=DENY_EMOJI,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "matrix consent_bridge: request_approval crashed; "
                    "auto-deny session=%s capability=%s",
                    session_id, claim.capability_id,
                )
                allowed = False

            try:
                resolved = gate.resolve_pending(
                    session_id=session_id,
                    capability_id=claim.capability_id,
                    decision=bool(allowed),
                    persist=False,  # always_allow only via TUI/CLI
                )
                if not resolved:
                    logger.debug(
                        "matrix consent_bridge: gate.resolve_pending returned "
                        "False (no pending entry) session=%s capability=%s — "
                        "race vs another channel? — ignoring",
                        session_id, claim.capability_id,
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "matrix consent_bridge: resolve_pending raised; the "
                    "agent may hang on this approval until timeout"
                )

        # Fire-and-forget — gate.request_approval will await its own
        # internal Event that resolve_pending eventually flips.
        asyncio.create_task(
            _watch_and_resolve(),
            name=f"matrix-consent-{claim.capability_id}",
        )
        return True

    return _handler


def register_matrix_consent_handler(
    *,
    gate,
    adapter,
    chat_id: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Install the matrix prompt handler on a ConsentGate.

    Call from the matrix plugin's ``register()`` after both the gateway
    and the matrix adapter are running. Re-registration replaces.
    """
    if not chat_id:
        logger.warning(
            "matrix consent_bridge: chat_id is empty; bridge NOT installed"
        )
        return
    handler = make_matrix_prompt_handler(
        gate=gate, adapter=adapter, chat_id=chat_id, timeout=timeout,
    )
    gate.set_prompt_handler(handler)
    logger.info(
        "matrix consent_bridge: installed (chat_id=%s, timeout=%.0fs)",
        chat_id, timeout,
    )


def parse_consent_config(matrix_cfg: dict[str, Any] | None) -> tuple[bool, str, float]:
    """Extract the bridge-relevant knobs from a matrix config block.

    Returns ``(enabled, chat_id, timeout_seconds)``. Missing keys →
    defaults; unknown types → defaults (fail-open).
    """
    if not isinstance(matrix_cfg, dict):
        return (False, "", DEFAULT_TIMEOUT_SECONDS)
    enabled = matrix_cfg.get("consent_handler", False)
    if not isinstance(enabled, bool):
        enabled = False
    chat_id = matrix_cfg.get("consent_chat_id", "")
    if not isinstance(chat_id, str):
        chat_id = ""
    timeout = matrix_cfg.get("consent_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS
    return (bool(enabled), str(chat_id), float(timeout))


__all__ = [
    "make_matrix_prompt_handler",
    "register_matrix_consent_handler",
    "parse_consent_config",
    "ALLOW_EMOJI",
    "DENY_EMOJI",
    "DEFAULT_TIMEOUT_SECONDS",
]
