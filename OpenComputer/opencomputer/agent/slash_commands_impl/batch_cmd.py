"""``/batch`` — fan out N units in parallel via DelegateTool isolation=worktree.

This is the operator-facing surface for the M11.2 production wiring at
``opencomputer/agent/batch_runner.py``.  Two invocation forms:

* ``/batch [{"unit_id":"a","description":"..."}, ...]`` — JSON list.
  Operator (or the agent itself, when decomposing a task) supplies a
  ready-made unit list.  Each unit spawns a DelegateTool call with
  ``isolation="worktree"``; PR URLs are aggregated.

* ``/batch <plain text>`` — refused with usage hint.  Decomposition
  belongs in the model (per ``opencomputer/skills/batch/SKILL.md``);
  the slash command is the *spawn* layer, not the *plan* layer.

Refused if no DelegateTool factory has been set (i.e. running outside
a real agent loop).  Refused if any unit's description contains
``/batch`` (defence in depth on top of the orchestrator's own check).
"""

from __future__ import annotations

import asyncio
import json

from opencomputer.agent.batch_orchestrator import (
    BatchConfig,
    BatchUnit,
    NestedBatchError,
    TooManyUnitsError,
    UnitOutcome,
)
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_USAGE = (
    "Usage: /batch <json-list-of-units>\n"
    "\n"
    "Each unit is an object with:\n"
    '  "unit_id":      unique id (e.g. "ext-rename-foo")\n'
    '  "description":  the prompt sent to the subagent (its own task)\n'
    '  "verify":       optional verification command (pytest invocation)\n'
    "\n"
    "Example:\n"
    '  /batch [{"unit_id":"a","description":"rename foo->bar in extensions/x"},'
    '{"unit_id":"b","description":"rename foo->bar in extensions/y"}]\n'
    "\n"
    "For chat-mode batching (decompose then spawn), see "
    "opencomputer/skills/batch/SKILL.md."
)


class BatchCommand(SlashCommand):
    name = "batch"
    description = "Spawn N parallel subagents in worktree-isolated sandboxes"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        body = (args or "").strip()
        if not body:
            return SlashCommandResult(output=_USAGE, handled=True)

        try:
            raw = json.loads(body)
        except json.JSONDecodeError as exc:
            return SlashCommandResult(
                output=f"/batch: argument must be JSON list of units. {exc}\n\n{_USAGE}",
                handled=True,
            )

        if not isinstance(raw, list) or not raw:
            return SlashCommandResult(
                output="/batch: argument must be a non-empty JSON list.",
                handled=True,
            )

        try:
            units = [_parse_unit(u, idx=i) for i, u in enumerate(raw)]
        except ValueError as exc:
            return SlashCommandResult(
                output=f"/batch: {exc}", handled=True
            )

        # Lazy imports to avoid circular at module load (slash_commands.py
        # eagerly registers all built-ins; batch_runner pulls in
        # DelegateTool which pulls in cli imports).
        try:
            from opencomputer.agent.batch_runner import run_batch_via_delegate
            from opencomputer.tools.delegate import DelegateTool
        except ImportError as exc:
            return SlashCommandResult(
                output=f"/batch: import failed ({exc}); skill not available.",
                handled=True,
            )

        delegate_tool = DelegateTool()
        if delegate_tool._factory is None:
            return SlashCommandResult(
                output=(
                    "/batch: DelegateTool factory not initialized. "
                    "This command is only available inside a real agent "
                    "loop (run `oc` interactively, not in --no-loop mode)."
                ),
                handled=True,
            )

        cfg = BatchConfig(
            max_parallel=min(int(runtime.custom.get("batch_max", 30)), 30),
            pr_title_prefix=str(runtime.custom.get("batch_prefix", "batch")),
        )

        try:
            result = await run_batch_via_delegate(
                units, delegate_tool=delegate_tool, config=cfg
            )
        except (NestedBatchError, TooManyUnitsError, ValueError) as exc:
            return SlashCommandResult(
                output=f"/batch: validation failed: {exc}",
                handled=True,
            )

        return SlashCommandResult(
            output=_format_result(result), handled=True
        )


def _parse_unit(raw: object, *, idx: int) -> BatchUnit:
    if not isinstance(raw, dict):
        raise ValueError(
            f"unit #{idx} is not an object: {type(raw).__name__}"
        )
    unit_id = raw.get("unit_id")
    description = raw.get("description")
    verify = raw.get("verify", "")
    if not isinstance(unit_id, str) or not unit_id:
        raise ValueError(f"unit #{idx} missing required string 'unit_id'")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(
            f"unit {unit_id!r} missing required non-empty string 'description'"
        )
    if verify is not None and not isinstance(verify, str):
        raise ValueError(f"unit {unit_id!r} 'verify' must be a string")
    return BatchUnit(
        unit_id=unit_id,
        description=description,
        verify=verify or "",
    )


def _format_result(result: object) -> str:
    """Pretty-print the BatchRunResult for the operator."""
    success_lines: list[str] = []
    fail_lines: list[str] = []
    timeout_lines: list[str] = []
    for u in result.units:
        if u.outcome == UnitOutcome.SUCCESS:
            success_lines.append(
                f"  ✓ {u.unit_id} → {u.pr_url} ({u.elapsed_seconds:.1f}s)"
            )
        elif u.outcome == UnitOutcome.TIMED_OUT:
            timeout_lines.append(
                f"  ⏱ {u.unit_id}: timed out after {u.elapsed_seconds:.0f}s"
            )
        else:
            fail_lines.append(
                f"  ✗ {u.unit_id}: {u.error or '(unknown failure)'}"
            )

    parts: list[str] = []
    parts.append(
        f"/batch finished: {len(success_lines)} success, "
        f"{len(fail_lines)} failed, {len(timeout_lines)} timed out, "
        f"{len(result.aborted_before_spawn)} aborted-before-spawn."
    )
    if success_lines:
        parts.append("\nOpened PRs:")
        parts.extend(success_lines)
    if fail_lines:
        parts.append("\nFailures:")
        parts.extend(fail_lines)
    if timeout_lines:
        parts.append("\nTimeouts:")
        parts.extend(timeout_lines)
    if result.aborted_before_spawn:
        parts.append("\nAborted (validation):")
        parts.extend(f"  · {x}" for x in result.aborted_before_spawn)
    return "\n".join(parts)


__all__ = ["BatchCommand"]


# Keep asyncio reachable for downstream consumers that monkey-patch
# this module to inject custom event-loop policies.
_ = asyncio
