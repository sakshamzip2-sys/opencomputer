"""Approval-keyword classifier for chat-based consent replies.

Hermes parity for the gateway approval flow:

> Agent sends dangerous command details to chat, waits for reply:
>   Approve: yes, y, approve, ok, go
>   Deny:    no, n, deny, cancel

OpenComputer's :class:`opencomputer.agent.consent.gate.ConsentGate`
already supports button-based replies via the adapter's
``resolve_pending`` callback (see ``extensions/telegram/adapter.py``).
This module adds a parallel TEXT-based classifier so plain-text replies
("yes" / "no" / "approve" / "deny") map cleanly onto the same gate
mechanism.

The classifier is intentionally strict: only the documented keywords
match. A reply like ``"yes please"`` is NOT classified — the user's
likely intent is to send a normal message, not approve a dangerous
command. If we matched anything containing ``"yes"`` we'd race against
benign messages.

Wiring: gateway dispatch should classify inbound text replies for
sessions with a pending consent prompt and call
``gate.resolve_pending`` when ``classify_reply`` returns "approve" or
"deny". When ``None`` is returned, treat as a normal message.
"""
from __future__ import annotations

from typing import Literal

#: Approve-side keywords (case-insensitive, exact-token match).
_APPROVE_KEYWORDS: frozenset[str] = frozenset({
    "yes", "y", "approve", "approved", "ok", "okay", "go", "allow", "permit",
})

#: Deny-side keywords.
_DENY_KEYWORDS: frozenset[str] = frozenset({
    "no", "n", "deny", "denied", "cancel", "stop", "block", "refuse",
})


ReplyClassification = Literal["approve", "deny"]


def classify_reply(text: str) -> ReplyClassification | None:
    """Classify a chat reply as ``approve`` / ``deny`` / ``None``.

    Strict match — only an exact single token (after strip + lower)
    counts. Punctuation is tolerated (``"yes!"`` works) by stripping
    common trailing punctuation chars.

    Args:
        text: raw user reply text.

    Returns:
        ``"approve"``, ``"deny"``, or ``None`` if the text isn't
        unambiguously either.
    """
    if not text:
        return None
    cleaned = text.strip().lower().rstrip(".!?,;:")
    # If multi-token, the user is sending a normal message, not a vote.
    if len(cleaned.split()) != 1:
        return None
    if cleaned in _APPROVE_KEYWORDS:
        return "approve"
    if cleaned in _DENY_KEYWORDS:
        return "deny"
    return None


__all__ = [
    "ReplyClassification",
    "classify_reply",
]
