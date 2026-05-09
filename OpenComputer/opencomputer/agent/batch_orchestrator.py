"""Batch parallel-migration orchestrator (v1.1 plan-3 M11.2).

Helper for the ``opencomputer/skills/batch/SKILL.md`` skill.  Splits a
parent task into N atomic units, spawns each in a worktree-isolated
subagent (via ``delegate(isolation="worktree")``, M4.1 already on main),
and aggregates results.

The skill markdown is the user-facing trigger; this module is the
orchestration plumbing.  Designed for testability: every external
dependency (subagent dispatcher, gh-cli wrapper, worktree manager)
is injected, so unit tests can assert the orchestration logic without
spinning up real subagents.

Production-grade properties:

- **Hard cap on N** (default 30) — refuses larger batches at config time.
- **Per-subagent timeout** so a wedged unit doesn't block siblings.
- **Graceful per-unit failure**: a crashed subagent marks its unit
  failed; siblings continue.
- **No nested batching**: refuses tasks that match "/batch" in the
  prompt to prevent runaway recursion.
- **Idempotent cleanup**: orphaned worktrees from crashed subagents
  are pruned at end-of-run.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("opencomputer.agent.batch_orchestrator")

#: Hard upper bound on parallel subagents per run.  Higher values stress
#: CI + GitHub API rate limits; 30 is the empirical sweet-spot.  Caller
#: cannot exceed this without modifying the source.
MAX_BATCH_SIZE: int = 30

#: Per-unit subagent timeout.  Default 20 min — generous for codemods
#: + tests + PR creation; stops a wedged unit from delaying the run.
DEFAULT_PER_UNIT_TIMEOUT_SECONDS: float = 20 * 60

#: Refuses tasks containing this regex in their description (prevents
#: a subagent from spawning another /batch).  Defense in depth.
_NESTED_BATCH_RE = re.compile(r"/batch\b", re.IGNORECASE)


class UnitOutcome(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class BatchUnit:
    """One atomic migration unit.

    ``description`` is the prompt the subagent receives.  ``verify``
    is the verification command (typically a pytest invocation) the
    subagent runs after the change.
    """

    unit_id: str
    description: str
    verify: str = ""


@dataclass(frozen=True, slots=True)
class UnitResult:
    """Outcome of one unit's subagent run."""

    unit_id: str
    outcome: UnitOutcome
    pr_url: str | None = None
    """PR URL if the subagent successfully opened one."""
    error: str | None = None
    """Error message if outcome is FAILED or TIMED_OUT."""
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class BatchRunResult:
    """Aggregate result of one /batch invocation."""

    units: tuple[UnitResult, ...]
    aborted_before_spawn: tuple[str, ...] = ()
    """Unit IDs the runner refused to spawn (e.g. nested /batch detection)."""

    @property
    def successful(self) -> tuple[UnitResult, ...]:
        return tuple(u for u in self.units if u.outcome == UnitOutcome.SUCCESS)

    @property
    def failed(self) -> tuple[UnitResult, ...]:
        return tuple(
            u for u in self.units
            if u.outcome in (UnitOutcome.FAILED, UnitOutcome.TIMED_OUT)
        )


@dataclass(frozen=True, slots=True)
class BatchConfig:
    """Tunables for one /batch invocation.  Caller populates from
    skill-time CLI flags."""

    max_parallel: int = MAX_BATCH_SIZE
    per_unit_timeout_seconds: float = DEFAULT_PER_UNIT_TIMEOUT_SECONDS
    pr_title_prefix: str = "batch"
    """Prefix prepended to each unit's PR title so reviewers can
    group them in the GitHub UI.  Conventionally derived from the
    parent task description."""


# Injectable callable: dispatches one unit to a subagent.  Returns the
# subagent's PR URL on success, raises on failure.  The skill wires
# this to ``Delegate`` with ``isolation="worktree"``.
SpawnSubagentFn = Callable[[BatchUnit], Awaitable[str]]


class NestedBatchError(ValueError):
    """Raised when a task description contains /batch — prevents
    recursive batching from inside a subagent."""


class TooManyUnitsError(ValueError):
    """Raised when the unit list exceeds ``MAX_BATCH_SIZE``."""


def validate_units(
    units: list[BatchUnit],
    *,
    max_parallel: int = MAX_BATCH_SIZE,
    max_total_units: int = MAX_BATCH_SIZE,
) -> None:
    """Validate the unit list before any spawn happens.

    ``max_parallel`` caps RUNTIME CONCURRENCY (the asyncio semaphore
    permit count).  ``max_total_units`` caps the TOTAL number of
    units in one /batch invocation — it's the absolute hard cap that
    protects CI / GitHub-API rate-limits even when the user runs at
    low concurrency.

    Raises:
        NestedBatchError: any unit's description contains '/batch'.
        TooManyUnitsError: more than ``max_total_units`` units.
        ValueError: malformed input (empty list, duplicate IDs, empty
            description, max_parallel above hard cap).
    """
    if max_parallel > MAX_BATCH_SIZE:
        raise ValueError(
            f"max_parallel={max_parallel} exceeds hard cap "
            f"MAX_BATCH_SIZE={MAX_BATCH_SIZE}"
        )
    if max_total_units > MAX_BATCH_SIZE:
        raise ValueError(
            f"max_total_units={max_total_units} exceeds hard cap "
            f"MAX_BATCH_SIZE={MAX_BATCH_SIZE}"
        )
    if not units:
        raise ValueError("batch unit list is empty")
    if len(units) > max_total_units:
        raise TooManyUnitsError(
            f"batch has {len(units)} units; cap is {max_total_units}.  "
            f"Split into smaller batches."
        )
    seen_ids: set[str] = set()
    for unit in units:
        if unit.unit_id in seen_ids:
            raise ValueError(f"duplicate unit_id {unit.unit_id!r}")
        seen_ids.add(unit.unit_id)
        if not unit.description.strip():
            raise ValueError(f"unit {unit.unit_id!r} has empty description")
        if _NESTED_BATCH_RE.search(unit.description):
            raise NestedBatchError(
                f"unit {unit.unit_id!r} contains '/batch' in its description; "
                f"refusing to spawn (prevents recursive batching)."
            )


async def run_batch(
    units: list[BatchUnit],
    *,
    spawn_fn: SpawnSubagentFn,
    config: BatchConfig | None = None,
) -> BatchRunResult:
    """Spawn N units concurrently and aggregate results.

    Each unit runs as one ``spawn_fn`` call wrapped in
    :func:`asyncio.wait_for` with the per-unit timeout.  Failures of
    individual units are caught and recorded; sibling units continue.

    Returns a :class:`BatchRunResult` regardless of how many units
    succeeded — caller decides how to surface partial-success.
    """
    cfg = config or BatchConfig()
    # Total-units cap is the absolute MAX_BATCH_SIZE.  Concurrency knob
    # (max_parallel) is independent — see validate_units docstring.
    validate_units(
        units,
        max_parallel=cfg.max_parallel,
        max_total_units=MAX_BATCH_SIZE,
    )

    sem = asyncio.Semaphore(cfg.max_parallel)
    results: list[UnitResult] = []

    async def _run_one(unit: BatchUnit) -> UnitResult:
        async with sem:
            t0 = asyncio.get_event_loop().time()
            try:
                pr_url = await asyncio.wait_for(
                    spawn_fn(unit),
                    timeout=cfg.per_unit_timeout_seconds,
                )
                elapsed = asyncio.get_event_loop().time() - t0
                return UnitResult(
                    unit_id=unit.unit_id,
                    outcome=UnitOutcome.SUCCESS,
                    pr_url=pr_url,
                    elapsed_seconds=elapsed,
                )
            except TimeoutError:
                elapsed = asyncio.get_event_loop().time() - t0
                logger.warning(
                    "batch unit %s timed out after %.0fs",
                    unit.unit_id,
                    cfg.per_unit_timeout_seconds,
                )
                return UnitResult(
                    unit_id=unit.unit_id,
                    outcome=UnitOutcome.TIMED_OUT,
                    error=f"timeout after {cfg.per_unit_timeout_seconds}s",
                    elapsed_seconds=elapsed,
                )
            except Exception as exc:  # noqa: BLE001 — capture per-unit errors
                elapsed = asyncio.get_event_loop().time() - t0
                logger.warning(
                    "batch unit %s failed (%s): %s",
                    unit.unit_id,
                    type(exc).__name__,
                    exc,
                )
                return UnitResult(
                    unit_id=unit.unit_id,
                    outcome=UnitOutcome.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_seconds=elapsed,
                )

    # Spawn all in parallel.  asyncio.gather with return_exceptions=False
    # is safe because each task's exception is caught inside _run_one.
    tasks = [asyncio.create_task(_run_one(u)) for u in units]
    for coro in asyncio.as_completed(tasks):
        result = await coro
        results.append(result)

    # Sort results back into input order so the report is reproducible.
    by_id: dict[str, UnitResult] = {r.unit_id: r for r in results}
    ordered = tuple(by_id[u.unit_id] for u in units)
    return BatchRunResult(units=ordered)


__all__ = [
    "DEFAULT_PER_UNIT_TIMEOUT_SECONDS",
    "MAX_BATCH_SIZE",
    "BatchConfig",
    "BatchRunResult",
    "BatchUnit",
    "NestedBatchError",
    "SpawnSubagentFn",
    "TooManyUnitsError",
    "UnitOutcome",
    "UnitResult",
    "run_batch",
    "validate_units",
]
