"""Detached tasks — fire-and-forget agent jobs that survive the chat session.

Saksham's workflow:

> "Forward a chart, ask Claude to do a deep 30-min analysis, get a notification
> when done. The chat session shouldn't hang waiting; it should return
> immediately with 'task #abc1234 started' and ping me when complete."

Cron is *scheduled* — fixed time pattern (8:30 AM daily). Detached tasks are
*fire-and-forget* — kicked off from a chat turn, run async, report result back
when done.

Public surface:

- :class:`TaskStore` — SQLite-backed CRUD for the v5 ``tasks`` table.
- :class:`TaskRunner` — background asyncio task that picks up queued tasks
  and runs them through a fresh :class:`AgentLoop`.
- :class:`SpawnDetachedTaskTool` — agent-callable tool that creates a
  queued task and returns a task id immediately.

Module docstring shape mirrors :mod:`opencomputer.cron` so contributors moving
between the two see the same conceptual layout.
"""

from __future__ import annotations

from .runtime import TaskRunner, TaskRunnerConfig
from .store import (
    TASK_STATUSES,
    Task,
    TaskNotFound,
    TaskStatus,
    TaskStore,
)

__all__ = [
    "TASK_STATUSES",
    "Task",
    "TaskNotFound",
    "TaskRunner",
    "TaskRunnerConfig",
    "TaskStatus",
    "TaskStore",
]
