"""Production wiring for /batch (v1.1 plan-3 M11.2 follow-up).

The :mod:`opencomputer.agent.batch_orchestrator` module ships a pure
orchestration engine (``run_batch``) that takes a ``spawn_fn`` callable
and fans out N units in parallel.  This module supplies the production
``spawn_fn`` that wires ``run_batch`` to the real ``DelegateTool`` with
``isolation="worktree"`` so each unit lands in its own sandboxed git
worktree and opens its own PR.

The skill markdown (``opencomputer/skills/batch/SKILL.md``) instructs
the agent to call ``Delegate(...)`` directly per unit when in chat
mode — this is the normal path.  Programmatic callers (scripts,
internal automation, batch-from-CLI) call
:func:`run_batch_via_delegate` instead so they don't need the model
in the loop.

The PR-URL extraction is the only non-trivial bit: subagents are
instructed (via the skill) to end with their PR URL on the last line.
We grep for the first ``https://github.com/.+/pull/\\d+`` match in
the subagent's final response.  Failure to find one raises
``MissingPRUrlError`` so the orchestrator records the unit as FAILED
(not SUCCESS-with-empty-URL, which would be a silent failure).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable

from opencomputer.agent.batch_orchestrator import (
    BatchConfig,
    BatchRunResult,
    BatchUnit,
    SpawnSubagentFn,
    run_batch,
)
from plugin_sdk.core import ToolCall, ToolResult

logger = logging.getLogger("opencomputer.agent.batch_runner")

# Match the first GitHub PR URL in the subagent's final response.
# Captures up to "/pull/<digits>" so trailing punctuation / parens
# don't pollute the URL.
_PR_URL_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+/pull/\d+"
)

#: Default tool allowlist for batch subagents.  Read/edit/grep cover
#: 99% of mechanical migrations; explicit Bash for ``gh pr create`` +
#: pytest invocations.  Delegate / SkillTool / Web tools are excluded
#: to prevent fork-bombs and reduce blast radius.
DEFAULT_BATCH_ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Edit",
    "MultiEdit",
    "Write",
    "Grep",
    "Glob",
    "Bash",
    "TodoWrite",
)


class MissingPRUrlError(RuntimeError):
    """Raised when a subagent's response doesn't contain a parseable PR URL.

    Tracked separately from generic spawn failures so observability can
    distinguish "subagent never opened a PR" from "subagent crashed".
    """


def _extract_pr_url(text: str) -> str:
    """Extract the first GitHub PR URL from a subagent's final response.

    Raises:
        MissingPRUrlError: when no PR URL is found.
    """
    if not text:
        raise MissingPRUrlError("subagent response was empty")
    m = _PR_URL_RE.search(text)
    if not m:
        raise MissingPRUrlError(
            "no GitHub PR URL in subagent response. The subagent must "
            "open a PR via `gh pr create` and include its URL in the "
            "final response. First 200 chars of response: " + text[:200]
        )
    return m.group(0)


def _format_unit_prompt(
    unit: BatchUnit,
    *,
    pr_title_prefix: str,
) -> str:
    """Render a single unit's prompt for the subagent.

    The prompt:

    1. States the unit-specific work.
    2. Lists verification expectations (from ``BatchUnit.verify``).
    3. Tells the subagent to commit + open a PR with the prescribed
       title prefix.
    4. Demands the PR URL on the final line.

    Worktree isolation handles cwd; the subagent doesn't need to
    create a worktree itself.
    """
    title = f"{pr_title_prefix}: {unit.unit_id}"
    verification_block = (
        f"\n\n**Verification:** {unit.verify}"
        if getattr(unit, "verify", "")
        else ""
    )
    return (
        f"# Batch unit: {unit.unit_id}\n\n"
        f"{unit.description.strip()}"
        f"{verification_block}\n\n"
        f"---\n\n"
        f"## Workflow\n\n"
        f"1. Implement the unit's change.\n"
        f"2. Run any verification listed above.\n"
        f"3. `git add` + `git commit -m \"{title}\"`.\n"
        f"4. `gh pr create --title \"{title}\" --body <unit-summary>`.\n"
        f"5. **End your final response with the PR URL on the last "
        f"line** — the orchestrator scrapes it via regex.\n\n"
        f"You are running in an isolated worktree; no other unit will "
        f"touch your files.  Do not call /batch yourself.\n"
    )


def make_delegate_spawn_fn(
    delegate_tool: object,
    *,
    allowed_tools: tuple[str, ...] = DEFAULT_BATCH_ALLOWED_TOOLS,
    pr_title_prefix: str = "batch",
    max_turns: int = 20,
) -> SpawnSubagentFn:
    """Build a production ``spawn_fn`` that calls ``DelegateTool.execute``.

    Args:
        delegate_tool: an instance of ``opencomputer.tools.delegate
          .DelegateTool`` (or any object with a compatible ``execute``
          method that accepts a ``ToolCall`` and returns a ``ToolResult``).
        allowed_tools: tools the subagent may invoke.  Defaults to a
          read/edit/grep/Bash subset chosen to cover mechanical
          migrations without enabling delegate-recursion or web access.
        pr_title_prefix: prepended to each unit's PR title so reviewers
          can group them.
        max_turns: hard cap on the subagent's loop length per unit.

    Returns:
        A :data:`SpawnSubagentFn` that takes a :class:`BatchUnit` and
        returns the PR URL on success.  Raises
        :class:`MissingPRUrlError` when the subagent finished but
        didn't surface a PR URL; the orchestrator catches this in its
        per-unit try/except and records the unit as FAILED.
    """

    async def spawn_fn(unit: BatchUnit) -> str:
        prompt = _format_unit_prompt(unit, pr_title_prefix=pr_title_prefix)
        # ``BatchUnit`` doesn't carry a path list today; if a future
        # field surfaces, propagate it here so the DelegateTool path
        # coordinator serialises overlapping units.
        paths = list(getattr(unit, "file_paths", ()) or ())
        call = ToolCall(
            id=f"batch-{unit.unit_id}-{uuid.uuid4().hex[:8]}",
            name="Delegate",
            arguments={
                "task": prompt,
                "isolation": "worktree",
                "allowed_tools": list(allowed_tools),
                "max_turns": max_turns,
                "role": "leaf",  # never let a batch unit spawn its own batches
                "paths": paths,
            },
        )
        try:
            result: ToolResult = await delegate_tool.execute(call)
        except Exception as exc:  # noqa: BLE001 — surface to orchestrator
            logger.warning(
                "batch unit %s: delegate.execute raised %s: %s",
                unit.unit_id,
                type(exc).__name__,
                exc,
            )
            raise

        if result.is_error:
            raise RuntimeError(
                f"DelegateTool returned error for unit {unit.unit_id!r}: "
                f"{result.content[:300]}"
            )

        return _extract_pr_url(result.content or "")

    return spawn_fn


async def run_batch_via_delegate(
    units: list[BatchUnit],
    *,
    delegate_tool: object,
    config: BatchConfig | None = None,
    allowed_tools: tuple[str, ...] = DEFAULT_BATCH_ALLOWED_TOOLS,
    max_turns: int = 20,
) -> BatchRunResult:
    """Convenience wrapper: build the spawn_fn, run the orchestrator.

    The agent's ``/batch`` skill drives this path when the operator
    invokes it programmatically (scripts, CLI helpers); chat-mode
    invocations follow the SKILL.md instructions instead, calling
    ``Delegate(...)`` per unit directly through the model.
    """
    cfg = config or BatchConfig()
    spawn_fn = make_delegate_spawn_fn(
        delegate_tool,
        allowed_tools=allowed_tools,
        pr_title_prefix=cfg.pr_title_prefix,
        max_turns=max_turns,
    )
    return await run_batch(units, spawn_fn=spawn_fn, config=cfg)


# Re-exports kept for documentation; spawn_fn type lives upstream.
SpawnSubagentFn = SpawnSubagentFn  # noqa: F811


__all__ = [
    "DEFAULT_BATCH_ALLOWED_TOOLS",
    "MissingPRUrlError",
    "make_delegate_spawn_fn",
    "run_batch_via_delegate",
]


_ = Callable, Awaitable  # ruff: keep imports for downstream type hints
