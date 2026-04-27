"""Background runner that executes queued detached tasks.

Long-lived asyncio task spawned by the gateway daemon. Polls the
``tasks`` table every ~5s for ``queued`` rows, runs each through a
fresh :class:`AgentLoop`, persists the result, then optionally
notifies the originating channel.

Concurrency:

- One concurrent task at a time (configurable via
  ``TaskRunnerConfig.concurrency``). Detached tasks can be expensive
  (long LLM runs); running 5 in parallel is a fast way to blow a
  budget by accident.
- Per-task timeout (default 1 hour). Long enough for "deep analysis"
  but bounded so a runaway loop doesn't run forever.

Notification:

- ``notify_policy`` is ``"done_only"`` by default — fire on terminal
  status only. ``"silent"`` skips the notify call entirely (the user
  asked for an async run + will check ``opencomputer task list``
  manually). ``"state_changes"`` is reserved for future use (the
  contract is in place, but progress events aren't routed to a
  channel yet).
- Notifications go through whatever channel adapter owned the original
  task's session. If the gateway daemon isn't running (CLI-only
  context), the result is recorded but not pushed; user sees it on
  their next ``opencomputer task list`` invocation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .store import Task, TaskStore

ExecutorFn = Callable[[Task], Awaitable[str]]
NotifierFn = Callable[[str, str], Awaitable[None]]

logger = logging.getLogger("opencomputer.tasks.runtime")


@dataclass
class TaskRunnerConfig:
    poll_interval_seconds: float = 5.0
    per_task_timeout_seconds: float = 3600.0
    concurrency: int = 1


class TaskRunner:
    """Long-lived asyncio loop that drains ``queued`` tasks one at a time.

    Wire from the gateway daemon::

        runner = TaskRunner(store, executor=run_one_task)
        await runner.recover_orphaned()
        runner_task = asyncio.create_task(runner.run_forever())
        ...
        runner.stop()
        await runner_task

    The ``executor`` callable runs one task and returns the final
    output text (or raises). The default executor (when ``executor`` is
    ``None``) calls :meth:`_default_executor` which constructs an
    :class:`AgentLoop` and runs the task's prompt as a one-shot
    conversation. Tests pass a stub executor to avoid spinning up the
    real loop.
    """

    def __init__(
        self,
        store: TaskStore,
        *,
        executor: ExecutorFn | None = None,
        notifier: NotifierFn | None = None,
        config: TaskRunnerConfig | None = None,
    ) -> None:
        self.store = store
        self._executor = executor or self._default_executor
        self._notifier = notifier
        self.config = config or TaskRunnerConfig()
        self._stop = asyncio.Event()
        self._inflight: dict[str, asyncio.Task] = {}

    # ─── Lifecycle ─────────────────────────────────────────────────

    async def recover_orphaned(self) -> int:
        """Mark abandoned ``running`` rows as ``orphaned``.

        Idempotent — call on every gateway start.
        """
        n = self.store.mark_orphaned_running()
        if n:
            logger.warning(
                "task runner: marked %d orphaned task(s) from previous run", n,
            )
        return n

    def stop(self) -> None:
        """Signal :meth:`run_forever` to exit cleanly at the next poll."""
        self._stop.set()

    async def run_forever(self) -> None:
        """Poll loop — drain queued tasks one at a time until stopped.

        Cancellation contract: pending tasks remain ``running`` in the
        store; the next gateway start picks them up via
        :meth:`recover_orphaned` and marks them ``orphaned`` so the
        user can resubmit (we never auto-resume — see store docstring).
        """
        logger.info(
            "task runner: starting (poll=%.1fs, timeout=%.0fs, concurrency=%d)",
            self.config.poll_interval_seconds,
            self.config.per_task_timeout_seconds,
            self.config.concurrency,
        )
        try:
            while not self._stop.is_set():
                # Reap finished in-flight tasks first so we don't double-count.
                self._reap()
                slots = self.config.concurrency - len(self._inflight)
                if slots > 0:
                    queued = self.store.list_queued(limit=slots)
                    for task in queued:
                        self._launch(task)
                # Sleep with stop-event responsiveness.
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.config.poll_interval_seconds,
                    )
                except TimeoutError:
                    continue
        finally:
            logger.info("task runner: stop signalled — letting in-flight tasks finish")
            for t in list(self._inflight.values()):
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 — best-effort shutdown
                    pass

    # ─── Per-task execution ────────────────────────────────────────

    def _launch(self, task: Task) -> None:
        """Mark running + spawn the per-task asyncio task."""
        try:
            self.store.mark_running(task.id)
        except Exception:  # noqa: BLE001 — race with cancel; skip silently
            logger.info(
                "task runner: skipping %s (mark_running failed; "
                "likely cancelled or already running)", task.id,
            )
            return

        self._inflight[task.id] = asyncio.create_task(
            self._run_one(task), name=f"detached-task-{task.id}",
        )

    async def _run_one(self, task: Task) -> None:
        try:
            output = await asyncio.wait_for(
                self._executor(task),
                timeout=self.config.per_task_timeout_seconds,
            )
        except asyncio.CancelledError:
            # Caller is shutting us down; let the runtime finalize via
            # the recover_orphaned path on next start.
            raise
        except TimeoutError:
            self._safe_fail(task.id, f"timed out after {self.config.per_task_timeout_seconds}s")
            return
        except Exception as e:  # noqa: BLE001 — capture for the user
            logger.exception("task runner: %s raised", task.id)
            self._safe_fail(task.id, f"{type(e).__name__}: {e}")
            return

        self._safe_complete(task.id, output)

        # Best-effort notification. Failure here mustn't break the runner.
        if self._notifier is not None and task.notify_policy != "silent":
            try:
                await self._notifier(task.id, output)
                self.store.mark_delivered(task.id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "task runner: notify failed for %s — user will see "
                    "result via `opencomputer task show`", task.id,
                    exc_info=True,
                )

    def _safe_complete(self, task_id: str, output: str) -> None:
        try:
            self.store.complete(task_id, output)
        except Exception:  # noqa: BLE001 — race with cancel
            logger.info(
                "task runner: complete() raced for %s — likely cancelled",
                task_id, exc_info=True,
            )

    def _safe_fail(self, task_id: str, error: str) -> None:
        try:
            self.store.fail(task_id, error)
        except Exception:  # noqa: BLE001
            logger.info(
                "task runner: fail() raced for %s", task_id, exc_info=True,
            )

    def _reap(self) -> None:
        """Drop done in-flight task handles."""
        done = [tid for tid, t in self._inflight.items() if t.done()]
        for tid in done:
            del self._inflight[tid]

    # ─── Default executor ─────────────────────────────────────────

    async def _default_executor(self, task: Task) -> str:
        """Spawn a fresh AgentLoop and run the task prompt one-shot.

        Imports lazily so tests can stub out the executor without paying
        the cost of importing the agent loop module.
        """
        from opencomputer.agent.config import default_config
        from opencomputer.agent.loop import AgentLoop
        from plugin_sdk.runtime_context import RuntimeContext

        cfg = default_config()
        loop = AgentLoop(cfg)
        runtime = RuntimeContext(plan_mode=False, yolo_mode=False)
        result = await loop.run_conversation(
            user_message=task.prompt,
            runtime=runtime,
        )
        return getattr(result.final_message, "content", None) or "(no output)"


__all__ = [
    "ExecutorFn",
    "NotifierFn",
    "TaskRunner",
    "TaskRunnerConfig",
]
