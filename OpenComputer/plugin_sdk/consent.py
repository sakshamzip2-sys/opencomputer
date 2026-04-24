"""Public consent primitives — shared between opencomputer core and plugins.

These types are re-exported via `plugin_sdk.__init__`. Plugins declare
`CapabilityClaim`s on their `BaseTool` subclasses; the core ConsentGate
resolves claims against stored `ConsentGrant`s and returns a `ConsentDecision`.

See the F1 architectural plan at ~/.claude/plans/i-want-you-to-twinkly-squirrel.md
for the full design rationale (four-tier model, progressive promotion,
audit log, license-boundary invariants).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Literal


class ConsentTier(IntEnum):
    """Four-tier consent model.

    Ordered: lower value = less friction, less trust required.
    """

    IMPLICIT = 0       # user told agent in chat — no external data read
    EXPLICIT = 1       # user clicked "enable" for a source, revocable
    PER_ACTION = 2     # per-action prompt showing specific data
    DELEGATED = 3      # time-windowed delegated autonomy, capability-scoped


@dataclass(frozen=True, slots=True)
class CapabilityClaim:
    """What a plugin claims its tool needs in order to run."""

    capability_id: str
    tier_required: ConsentTier
    human_description: str
    data_scope: str | None = None


@dataclass(frozen=True, slots=True)
class ConsentGrant:
    """A user-approved grant for a capability."""

    capability_id: str
    tier: ConsentTier
    scope_filter: str | None
    granted_at: float
    expires_at: float | None
    granted_by: Literal["user", "auto", "promoted"]


@dataclass(frozen=True, slots=True)
class ConsentDecision:
    """Runtime decision from ConsentGate."""

    allowed: bool
    reason: str
    tier_matched: ConsentTier | None
    audit_event_id: int | None


__all__ = [
    "ConsentTier",
    "CapabilityClaim",
    "ConsentGrant",
    "ConsentDecision",
]
