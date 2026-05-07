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


#: Valid ConsentTier names accepted by ``make_approval_callback``.
#: Mirrors plugin_sdk.consent.ConsentTier exactly — kept as a frozenset
#: of strings so the caller doesn't need to import ConsentTier just to
#: validate the parameter.
_VALID_TIERS: frozenset[str] = frozenset({
    "IMPLICIT", "EXPLICIT", "PER_ACTION", "DELEGATED",
})


def make_approval_callback(
    session_id: str,
    gate: Any,
    loop: asyncio.AbstractEventLoop,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    *,
    default_tier: str = "PER_ACTION",
):
    """Return a sync approval_callback(command, description) -> str.

    Args:
        session_id: ACP session ID for logging.
        gate: ConsentGate instance (from opencomputer.agent.consent.gate).
        loop: The event loop where gate coroutines must run.
        timeout: Seconds before auto-deny.
        default_tier: PR-A Feature 3 — the ``ConsentTier`` name used
            when building the ``CapabilityClaim``. Acceptable values
            mirror ``plugin_sdk.consent.ConsentTier``:
            ``IMPLICIT`` / ``EXPLICIT`` / ``PER_ACTION`` / ``DELEGATED``.
            Validated at construction; a bad value raises ``ValueError``
            so the caller can surface it to the IDE.

    Raises:
        ValueError: ``default_tier`` is not a valid ConsentTier name.
    """
    from plugin_sdk.consent import CapabilityClaim, ConsentTier

    if default_tier not in _VALID_TIERS:
        raise ValueError(
            f"default_tier must be one of {sorted(_VALID_TIERS)}, "
            f"got {default_tier!r}"
        )

    # Resolve once at construction so the inner callback doesn't repeat
    # the lookup on every invocation.
    _tier = getattr(ConsentTier, default_tier)

    def approval_callback(command: str, description: str) -> str:
        """Synchronous approval bridge for the agent loop."""
        try:
            claim = CapabilityClaim(
                capability_id=f"acp.dynamic.{command[:32]}",
                tier_required=_tier,
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
