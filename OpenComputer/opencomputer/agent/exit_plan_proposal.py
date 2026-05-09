"""Process-wide slot for the most recent ExitPlanMode `next_mode` proposal.

v1.1 plan-2 M5.4 (2026-05-09). The ExitPlanMode tool (in
extensions/coding-harness) writes a proposal here when the agent
passes ``next_mode``; the agent loop reads + clears it after the
tool dispatch completes so the runtime's permission_mode flips for
subsequent turns in the same session.

Lives in core (rather than the extension) so the tool and the loop
share one slot — avoiding the module-identity trap when the same
file is loaded under different sys.path / synthetic-name routes
(once via the plugin loader, once via the core loop's import).

Single in-process slot is sufficient: an agent loop only ever has
one plan-mode session in flight at a time. Lock is defensive for
future multi-loop processes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

#: The 4 valid ``next_mode`` values, kept here so both producer
#: (the tool, in the extension) and consumer (the loop, in core)
#: pin the same set without import-cycle risk.
PROPOSED_EXIT_MODES: tuple[str, ...] = ("auto", "acceptEdits", "manual", "keep")


@dataclass(frozen=True, slots=True)
class ExitPlanProposal:
    """Most-recent plan + suggested next_mode emitted by ExitPlanMode."""

    plan: str
    next_mode: str  # one of PROPOSED_EXIT_MODES


_PROPOSAL_LOCK = threading.Lock()
_LAST_PROPOSAL: ExitPlanProposal | None = None


def get_last_proposal() -> ExitPlanProposal | None:
    """Return the most recent proposal without consuming it. Read-only."""
    with _PROPOSAL_LOCK:
        return _LAST_PROPOSAL


def pop_last_proposal() -> ExitPlanProposal | None:
    """Return + clear the most recent proposal."""
    global _LAST_PROPOSAL  # noqa: PLW0603
    with _PROPOSAL_LOCK:
        out = _LAST_PROPOSAL
        _LAST_PROPOSAL = None
        return out


def record_proposal(plan: str, next_mode: str) -> None:
    """Write a proposal into the slot. Caller validates ``next_mode``
    against :data:`PROPOSED_EXIT_MODES`."""
    global _LAST_PROPOSAL  # noqa: PLW0603
    with _PROPOSAL_LOCK:
        _LAST_PROPOSAL = ExitPlanProposal(plan=plan, next_mode=next_mode)


__all__ = [
    "PROPOSED_EXIT_MODES",
    "ExitPlanProposal",
    "get_last_proposal",
    "pop_last_proposal",
    "record_proposal",
]
