"""Doctor contribution surface — plugins contribute health checks + repairs.

A HealthContribution is one named check. It runs against an implicit
DoctorContext (passed by the core) and returns a RepairResult. If
`doctor --fix` was invoked, the `fix` flag is True and the contribution
is expected to mutate state in place before returning.

Source: openclaw src/flows/doctor-health-contributions.ts — same check/fix
shape, same options.fix flag passed to each contribution, same decision
to keep contributions as metadata + a single `run()` callable rather than
two separate check/repair methods.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

HealthStatus = Literal["pass", "warn", "fail", "skip"]


@dataclass(frozen=True, slots=True)
class RepairResult:
    """Outcome of a single HealthContribution.run call."""

    id: str
    status: HealthStatus
    detail: str = ""
    #: True only when `fix=True` was passed AND the contribution actually
    #: mutated state to address the problem. Check-only runs always leave
    #: this False.
    repaired: bool = False


#: Async callable signature for a health contribution. Takes the fix flag,
#: returns one RepairResult. The callable is responsible for checking and
#: (optionally) repairing — there is no separate check/repair split.
HealthRunFn = Callable[[bool], Awaitable[RepairResult]]


@dataclass(frozen=True, slots=True)
class HealthContribution:
    """One plugin-contributed doctor check + optional repair."""

    id: str
    description: str
    run: HealthRunFn


__all__ = ["HealthContribution", "HealthRunFn", "HealthStatus", "RepairResult"]
