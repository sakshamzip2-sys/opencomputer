"""Bridge ACP permission requests to OC's consent gate.

make_approval_callback() returns a synchronous callback compatible with
OC's agent loop approval_callback contract:
    approval_callback(command: str, description: str) -> str
    Returns: "once" | "always" | "deny"

The callback runs the gate's async request_approval() on the event loop
via asyncio.run_coroutine_threadsafe(), matching Hermes's acp_adapter
pattern since the agent loop runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS: float = 60.0

# Maps OC grant_type → approval string the agent loop understands
_GRANT_TYPE_MAP: dict[str, str] = {
    "once": "once",
    "always": "always",
    "deny": "deny",
    "session": "once",  # treat session grants as once
}


def make_approval_callback(
    session_id: str,
    gate: Any,
    loop: asyncio.AbstractEventLoop,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
):
    """Return a sync approval_callback(command, description) -> str.

    Args:
        session_id: ACP session ID for logging.
        gate: ConsentGate instance (from opencomputer.agent.consent.gate).
        loop: The event loop where gate coroutines must run.
        timeout: Seconds before auto-deny.
    """
    from plugin_sdk.consent import CapabilityClaim, ConsentTier

    def approval_callback(command: str, description: str) -> str:
        """Synchronous approval bridge for the agent loop."""
        try:
            claim = CapabilityClaim(
                capability_id=f"acp.dynamic.{command[:32]}",
                tier_required=ConsentTier.PER_ACTION,
                human_description=description or command,
            )
        except Exception:
            return "deny"

        try:
            future = asyncio.run_coroutine_threadsafe(
                gate.request_approval(
                    claim=claim,
                    scope=command,
                    session_id=session_id,
                ),
                loop,
            )
            decision = future.result(timeout=timeout)
            if not decision.allowed:
                return "deny"
            grant_type = getattr(decision, "grant_type", "once") or "once"
            return _GRANT_TYPE_MAP.get(str(grant_type).lower(), "once")
        except FutureTimeout:
            logger.warning(
                "acp.permissions: approval timed out after %.0fs for session %s — denying",
                timeout,
                session_id,
            )
            return "deny"
        except Exception as exc:
            logger.warning(
                "acp.permissions: approval failed for session %s: %s — denying",
                session_id,
                exc,
            )
            return "deny"

    return approval_callback


__all__ = ["make_approval_callback"]
