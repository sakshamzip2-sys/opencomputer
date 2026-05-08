"""Triage → spec expansion via auxiliary LLM (Hermes Doc-2 parity, 2026-05-08).

A triage task is a one-line idea ("research AI funding"). The dashboard
and CLI offer a ``specify`` action that expands the title + (optional)
short body into a structured multi-paragraph spec via the auxiliary
model, then promotes the task ``triage → todo``.

The auxiliary call routes through :mod:`opencomputer.agent.aux_llm` so
the same provider + auth that runs chat handles the call — no new
config slot needed for the model itself. The system prompt is fixed
here; future configuration may override via ``auxiliary.triage_specifier``
if a user wants to swap models, but the load-bearing default is
"piggyback on whatever the user already has working".

Design constraints:

* **Idempotent on completed tasks.** ``specify_task`` on a non-triage
  task raises :class:`SpecifyError` rather than silently overwriting
  the body — this catches accidental "specify the wrong id" mistakes.
* **Fail-safe on LLM errors.** A specifier-model failure leaves the
  task in ``triage`` (does NOT promote, does NOT modify body). The
  caller sees the underlying exception bubble so dashboards can show a
  banner.
* **Deterministic max length.** The expanded body is capped (default
  4000 chars) so a runaway model doesn't bloat the DB.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from opencomputer.kanban import db as kb

logger = logging.getLogger(__name__)


SPECIFY_SYSTEM_PROMPT = (
    "You are a senior triage editor. The user filed a one-line task "
    "idea on a kanban board. Expand it into a concrete, actionable "
    "specification a worker can execute without follow-up questions.\n\n"
    "Output a single Markdown body with these sections in order:\n\n"
    "## Goal\n"
    "<one paragraph stating the desired end-state in concrete terms>\n\n"
    "## Approach\n"
    "<2-4 bullet points of the load-bearing steps>\n\n"
    "## Definition of Done\n"
    "<bulleted list of testable acceptance criteria>\n\n"
    "## Out of scope\n"
    "<bulleted list — be honest about what this task does NOT cover>\n\n"
    "Rules:\n"
    "- Do NOT ask the user clarifying questions; use your best judgment.\n"
    "- Do NOT include task metadata (assignee, priority, deadlines).\n"
    "- Do NOT prefix with 'Here is...' — output ONLY the markdown body.\n"
    "- Be concrete and specific; vague specs cause downstream failures."
)

SPECIFY_USER_TEMPLATE = (
    "Title: {title}\n"
    "Existing body (may be empty):\n{body}\n\n"
    "Expand into the structured spec described above."
)


# Cap on expanded body so a runaway model doesn't bloat the DB. 4000
# chars ≈ 800 tokens — comfortably more than any reasonable spec.
MAX_BODY_CHARS = 4000


class SpecifyError(RuntimeError):
    """Raised when a task can't be specified for a *deterministic* reason
    (wrong status, missing task, empty LLM output). LLM transport
    failures bubble as their original exception so the caller sees the
    real cause."""


@dataclass(slots=True, frozen=True)
class SpecifyResult:
    """What ``specify_task`` returns to a caller (CLI, dashboard endpoint)."""

    task_id: str
    old_status: str
    new_status: str
    expanded_body: str
    truncated: bool


async def _call_specifier_model(prompt: str) -> str:
    """Invoke the auxiliary model for spec expansion.

    Wrapped in its own function so unit tests can monkeypatch this and
    avoid hitting a real provider. Mirrors the pattern in
    :mod:`opencomputer.agent.goal`.
    """
    from opencomputer.agent.aux_llm import complete_text

    return await complete_text(
        messages=[{"role": "user", "content": prompt}],
        system=SPECIFY_SYSTEM_PROMPT,
        max_tokens=1500,
        temperature=0.2,
    )


async def specify_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    promote_to: str = "todo",
) -> SpecifyResult:
    """Expand a triage task and promote it.

    Parameters
    ----------
    conn:
        Open SQLite connection to the active board's kanban DB.
    task_id:
        Target task. Must be in status ``triage``; raises
        :class:`SpecifyError` otherwise so misuse on already-specified
        tasks is loud.
    promote_to:
        New status after expansion. Defaults to ``todo``; some flows
        prefer ``ready`` (skip the dispatcher's promotion step) — caller's
        choice.
    """
    task = kb.get_task(conn, task_id)
    if task is None:
        raise SpecifyError(f"task {task_id!r} not found")
    if task.status != "triage":
        raise SpecifyError(
            f"task {task_id!r} is in status {task.status!r}, not triage — "
            "specify only operates on triage tasks. Edit non-triage "
            "task bodies manually."
        )

    prompt = SPECIFY_USER_TEMPLATE.format(
        title=task.title,
        body=task.body or "(empty)",
    )
    expanded = (await _call_specifier_model(prompt)).strip()
    if not expanded:
        raise SpecifyError(
            "LLM returned empty body — task left in triage. Re-run or "
            "manually expand."
        )
    truncated = False
    if len(expanded) > MAX_BODY_CHARS:
        expanded = expanded[:MAX_BODY_CHARS] + "\n\n[truncated]"
        truncated = True

    ok = kb.apply_specify(
        conn,
        task_id=task_id,
        expanded_body=expanded,
        new_status=promote_to,
    )
    if not ok:
        # The earlier ``get_task`` succeeded so the task existed; if
        # ``apply_specify`` returns False, the row was deleted between
        # the two reads (rare race).
        raise SpecifyError(
            f"task {task_id!r} disappeared mid-specify — was it archived?"
        )
    return SpecifyResult(
        task_id=task_id,
        old_status="triage",
        new_status=promote_to,
        expanded_body=expanded,
        truncated=truncated,
    )


__all__ = ["SpecifyError", "SpecifyResult", "specify_task"]
