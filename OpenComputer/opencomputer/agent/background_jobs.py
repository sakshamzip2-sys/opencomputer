"""Background-job registry — Hermes-parity ``/background <prompt>`` MVP.

Lets the user kick off an isolated agent turn in a daemon thread without
blocking the current REPL or chat. Result is captured into an in-memory
registry and surfaced via ``/background list`` and ``/background show <id>``.

Design notes:

* **Isolation** — each job runs on its own thread with its own asyncio loop
  and a *fresh* ``AgentLoop`` (created via the registered factory, same shape
  as :class:`opencomputer.tools.delegate.DelegateTool`). The job's session
  has a brand-new uuid, so its history does NOT mix with the foreground
  session.
* **No streaming** — MVP captures the final assistant text only. Streaming
  the partial response back to the originating channel is a follow-up.
* **Memory** — the registry is process-local and bounded
  (``max_jobs=200``). Older completed jobs are evicted FIFO. Nothing is
  persisted across restarts.
* **Errors** — exceptions in the worker thread are captured into the
  ``error`` field and ``status="error"``; they never propagate.

Hermes parity:

* ``/background <prompt>`` matches Hermes's ``/background`` slash command
  (spawns isolated daemon thread session, no shared history).
* ``/background list`` and ``/background show <id>`` are extensions
  inspired by Hermes's "result appears as inline panel on completion" UX,
  adapted for OC's CLI + multi-channel surface where push-on-completion
  needs adapter routing (deferred to a follow-up).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

logger = logging.getLogger(__name__)

JobStatus = Literal["pending", "running", "complete", "error"]

# Loop factory — same shape as DelegateTool. The CLI / gateway entrypoint
# registers a callable that returns a fresh AgentLoop per call. The factory
# stays Any-typed so the SDK boundary doesn't pull in opencomputer.agent
# at import time (background_jobs is imported from the slash module that
# plugin_sdk-only environments may also touch).
_LoopFactory = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class BackgroundJob:
    """Snapshot of a single background-turn run.

    All fields are populated at submit time except ``completed_at`` /
    ``result`` / ``error``, which are filled in by the worker thread on
    completion.
    """

    job_id: str
    prompt: str
    status: JobStatus
    started_at: float
    completed_at: float | None = None
    result: str | None = None
    error: str | None = None
    iterations: int | None = None
    session_id: str | None = None


class BackgroundJobRegistry:
    """Process-local registry of background jobs.

    Thread-safe via a single ``threading.Lock`` — every public method
    acquires the lock before touching ``_jobs``. The lock also serialises
    state-machine transitions inside the worker thread.

    Capacity-bounded to ``max_jobs`` (default 200). Eviction is FIFO over
    *completed/error* jobs — running jobs are never evicted, so a heavy
    fan-out of submits without any pickup will eventually start raising
    ``RegistryFull``.
    """

    def __init__(self, *, max_jobs: int = 200) -> None:
        self._jobs: OrderedDict[str, BackgroundJob] = OrderedDict()
        self._lock = threading.Lock()
        self._factory: _LoopFactory | None = None
        self._max_jobs = max_jobs

    # ─── factory plumbing ────────────────────────────────────────────

    def set_factory(self, factory: _LoopFactory | None) -> None:
        """Register the AgentLoop factory used by ``submit``.

        ``None`` clears the factory (tests reset between cases). Calling
        ``submit`` without a factory raises :class:`RuntimeError`.
        """
        with self._lock:
            self._factory = factory

    @property
    def factory_registered(self) -> bool:
        with self._lock:
            return self._factory is not None

    # ─── lifecycle ──────────────────────────────────────────────────

    def submit(self, prompt: str, *, plan: bool = False) -> str:
        """Spawn a background job for ``prompt`` and return its job id.

        The worker thread is daemonised so process exit doesn't wait on
        an in-flight background turn — completed results are best-effort
        snapshots, not durable contracts.
        """
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("background prompt is empty")
        with self._lock:
            if self._factory is None:
                raise RuntimeError(
                    "background registry has no AgentLoop factory; "
                    "the CLI entrypoint forgot to call set_factory()"
                )
            self._evict_if_full_locked()
            factory = self._factory
            job_id = uuid.uuid4().hex[:12]
            job = BackgroundJob(
                job_id=job_id,
                prompt=prompt,
                status="pending",
                started_at=time.time(),
            )
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run_worker,
            name=f"oc-bg-{job_id}",
            args=(job_id, prompt, plan, factory),
            daemon=True,
        )
        thread.start()
        return job_id

    def _evict_if_full_locked(self) -> None:
        """Evict the oldest completed/error job to free space.

        Caller MUST hold ``self._lock``. Running jobs are skipped — if
        every slot is occupied by a running job we raise so the caller
        sees the back-pressure rather than silently dropping work.
        """
        if len(self._jobs) < self._max_jobs:
            return
        for jid, j in list(self._jobs.items()):
            if j.status in ("complete", "error"):
                self._jobs.pop(jid, None)
                if len(self._jobs) < self._max_jobs:
                    return
        raise RuntimeError(
            f"background registry full ({self._max_jobs} running jobs); "
            "wait for some to finish before submitting more"
        )

    def _run_worker(
        self,
        job_id: str,
        prompt: str,
        plan: bool,
        factory: _LoopFactory,
    ) -> None:
        """Worker-thread entrypoint — runs the agent turn and updates state.

        Owns its own asyncio event loop because background threads don't
        inherit the foreground loop and ``asyncio.run`` creates+tears down
        a fresh one per call. Failures are captured into the registry,
        never re-raised (the thread is daemonised; an uncaught exception
        would just print a traceback and silently disappear).
        """
        with self._lock:
            cur = self._jobs.get(job_id)
            if cur is not None:
                self._jobs[job_id] = replace(cur, status="running")

        text = ""
        err = ""
        iters: int | None = None
        sid: str | None = None
        try:
            loop_obj = factory()
            from plugin_sdk.runtime_context import RuntimeContext

            runtime = RuntimeContext(plan_mode=plan)

            async def _drive() -> Any:
                # New session id is implicit — passing session_id=None makes
                # AgentLoop generate a fresh one, which is exactly the
                # "no shared history" guarantee we want.
                return await loop_obj.run_conversation(prompt, runtime=runtime)

            result = asyncio.run(_drive())
            msg = getattr(result, "final_message", None)
            content = getattr(msg, "content", "") if msg is not None else ""
            text = content if isinstance(content, str) else ""
            iters = getattr(result, "iterations", None)
            sid = getattr(result, "session_id", None)
        except Exception as e:  # noqa: BLE001 — capture all failure modes
            err = f"{type(e).__name__}: {e}"
            logger.exception("background job %s failed", job_id)

        with self._lock:
            cur = self._jobs.get(job_id)
            if cur is None:
                # Evicted between start and finish — drop the result.
                return
            self._jobs[job_id] = replace(
                cur,
                status="error" if err else "complete",
                completed_at=time.time(),
                result=text or None,
                error=err or None,
                iterations=iters,
                session_id=sid,
            )

    # ─── readers ────────────────────────────────────────────────────

    def get(self, job_id: str) -> BackgroundJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_recent(self, limit: int = 20) -> list[BackgroundJob]:
        """Return jobs, newest first, capped at ``limit``."""
        with self._lock:
            return list(self._jobs.values())[-limit:][::-1]

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)


# ─── module-level default registry ───────────────────────────────────

_default_registry = BackgroundJobRegistry()


def get_default_registry() -> BackgroundJobRegistry:
    """Return the process-singleton registry used by the slash command."""
    return _default_registry


def reset_for_tests() -> None:
    """Clear the default registry. Tests use this between cases."""
    _default_registry._lock.acquire()
    try:
        _default_registry._jobs.clear()
        _default_registry._factory = None
    finally:
        _default_registry._lock.release()


__all__ = [
    "BackgroundJob",
    "BackgroundJobRegistry",
    "get_default_registry",
    "reset_for_tests",
]
