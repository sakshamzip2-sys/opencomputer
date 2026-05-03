"""Cron job scheduler — asyncio-native tick loop.

Provides:

- :func:`tick` — single-shot: find due jobs, run them, save output, mark results.
  Returns the number of jobs executed. Skips silently when another tick holds
  the file lock.
- :func:`run_scheduler_loop` — long-running coroutine that ticks every 60s.
  Lives inside the gateway daemon (or `opencomputer cron daemon`).

The scheduler reuses a file-based lock at ``<cron_dir>/.tick.lock`` so the
gateway's in-process ticker, a standalone ``opencomputer cron daemon``, and a
manual ``opencomputer cron tick`` never overlap. Recurring jobs have their
``next_run_at`` advanced under the lock BEFORE execution, providing
at-most-once semantics on crash.

Adapted from `sources/hermes-agent-2026.4.23/cron/scheduler.py` but slimmer:
- Hermes runs jobs in a `ThreadPoolExecutor`; we run them as asyncio tasks
  (gathered with `asyncio.gather`) since OC's agent loop is async.
- We delegate the actual agent run to :class:`opencomputer.agent.loop.AgentLoop`
  rather than a Hermes-specific `AIAgent` class.
- Delivery is OC-native: telegram/discord/webhook channels resolved through
  the bundled adapter plugins.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows fallback
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None

from opencomputer.cron.jobs import (
    advance_next_run,
    cron_dir,
    get_due_jobs,
    mark_job_run,
    save_job_output,
)
from opencomputer.cron.threats import scan_cron_prompt

logger = logging.getLogger(__name__)

SILENT_MARKER = "[SILENT]"
"""Cron jobs can suppress delivery by responding exactly ``[SILENT]``."""

DEFAULT_TICK_INTERVAL_S = 60
DEFAULT_JOB_TIMEOUT_S = 600
"""10 minutes per job; can be overridden via env var ``OPENCOMPUTER_CRON_TIMEOUT``."""

DEFAULT_MAX_PARALLEL = 3
"""Match Hermes's default for concurrent cron job execution."""


def _now() -> datetime:
    return datetime.now(UTC)


def _job_timeout_seconds() -> float:
    raw = os.getenv("OPENCOMPUTER_CRON_TIMEOUT", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            logger.warning("Invalid OPENCOMPUTER_CRON_TIMEOUT=%r; using default", raw)
    return float(DEFAULT_JOB_TIMEOUT_S)


def _max_parallel() -> int:
    raw = os.getenv("OPENCOMPUTER_CRON_MAX_PARALLEL", "").strip()
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            logger.warning("Invalid OPENCOMPUTER_CRON_MAX_PARALLEL=%r; using default", raw)
    return DEFAULT_MAX_PARALLEL


# ---------------------------------------------------------------------------
# Cross-process file lock around tick()
# ---------------------------------------------------------------------------


class _TickLockHeld(Exception):  # noqa: N818 — internal sentinel, not a public Error
    """Raised internally when another process is already ticking."""


def _acquire_tick_lock() -> Any:
    """Acquire an exclusive non-blocking flock on ``<cron_dir>/.tick.lock``.

    Returns the open file descriptor; caller is responsible for releasing
    via :func:`_release_tick_lock`. Raises :class:`_TickLockHeld` if another
    process holds it.
    """
    lock_path = cron_dir() / ".tick.lock"
    fd = open(lock_path, "w")  # noqa: SIM115 — fd must outlive this fn (caller releases via _release_tick_lock)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:  # pragma: no cover
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        fd.close()
        raise _TickLockHeld() from exc
    return fd


def _release_tick_lock(fd: Any) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover
            try:
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        fd.close()


# ---------------------------------------------------------------------------
# Single job execution
# ---------------------------------------------------------------------------


async def _build_agent_loop(job: dict[str, Any]) -> Any:
    """Construct a fresh :class:`AgentLoop` configured for a cron run.

    Cron jobs run in their own session, in plan mode by default, with a
    capped iteration budget. The loop inherits the active provider plugin
    from config — there's no per-job provider override.
    """
    from opencomputer.agent.config_store import load_config
    from opencomputer.agent.loop import AgentLoop

    cfg = load_config()

    # Cron sessions are short-lived; cap iterations tighter than interactive default.
    cfg = cfg.with_loop_overrides(max_iterations=min(cfg.loop.max_iterations, 30))

    return AgentLoop(
        config=cfg,
        # plan_mode + yolo_mode are surfaced via RuntimeContext at run-time
        # rather than baked into the loop itself.
    )


def _build_run_prompt(job: dict[str, Any]) -> str:
    """Construct the user prompt the agent should answer for this run.

    For ``--prompt`` jobs: returns the prompt verbatim with a cron-context header.
    For ``--skill`` jobs: returns "use the X skill" so the agent self-invokes.
    """
    cron_hint = (
        "[SYSTEM: You are running as a scheduled cron job. "
        "DELIVERY: Your final response will be automatically delivered to the "
        "configured channel — do NOT call send_message yourself. "
        "SILENT: If there is genuinely nothing new to report, respond with "
        "exactly \"[SILENT]\" (nothing else) to suppress delivery.]\n\n"
    )
    if job.get("skill"):
        return f"{cron_hint}Use the `{job['skill']}` skill and report your findings."
    return cron_hint + (job.get("prompt") or "")


async def _run_one_job(job: dict[str, Any]) -> tuple[bool, str, str, str | None]:
    """Run a single cron job. Returns ``(success, full_doc, final_response, error)``.

    Defence-in-depth: re-scan the prompt for threats before invoking the
    agent. A poisoned prompt that survived create-time scanning (e.g.
    via direct file edit) is blocked here too.
    """
    from plugin_sdk.runtime_context import RuntimeContext

    job_id = job["id"]
    job_name = job["name"]

    # Re-scan prompt before run
    prompt_text = job.get("prompt") or ""
    if prompt_text:
        threat = scan_cron_prompt(prompt_text)
        if threat:
            error = f"prompt scan failed at run-time: {threat}"
            logger.error("Cron job %s blocked: %s", job_id, error)
            return False, _failed_doc(job, error), "", error

    full_prompt = _build_run_prompt(job)

    try:
        loop = await _build_agent_loop(job)
        runtime = RuntimeContext(
            plan_mode=bool(job.get("plan_mode", True)),
            yolo_mode=False,
            custom={"cron_job_id": job_id, "cron_session": True},
        )
        timeout = _job_timeout_seconds()
        result = await asyncio.wait_for(
            loop.run_conversation(user_message=full_prompt, runtime=runtime),
            timeout=timeout,
        )
    except TimeoutError:
        error = f"cron job '{job_name}' exceeded {_job_timeout_seconds():.0f}s timeout"
        logger.error(error)
        return False, _failed_doc(job, error), "", error
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("Cron job '%s' (id=%s) failed: %s", job_name, job_id, error)
        return False, _failed_doc(job, error), "", error

    final = (result.final_message.content if result and result.final_message else "") or ""
    if final.strip() == "(No response generated)":
        final = ""

    doc = _success_doc(job, full_prompt, final or "(empty response)")
    return True, doc, final, None


def _success_doc(job: dict[str, Any], prompt: str, response: str) -> str:
    return (
        f"# Cron Job: {job['name']}\n\n"
        f"**Job ID:** {job['id']}\n"
        f"**Run Time:** {_now().strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        f"**Schedule:** {job.get('schedule_display', 'N/A')}\n\n"
        f"## Prompt\n\n{prompt}\n\n"
        f"## Response\n\n{response}\n"
    )


def _failed_doc(job: dict[str, Any], error: str) -> str:
    return (
        f"# Cron Job: {job['name']} (FAILED)\n\n"
        f"**Job ID:** {job['id']}\n"
        f"**Run Time:** {_now().strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        f"**Schedule:** {job.get('schedule_display', 'N/A')}\n\n"
        f"## Error\n\n```\n{error}\n```\n"
    )


# ---------------------------------------------------------------------------
# Delivery (channel routing)
# ---------------------------------------------------------------------------


async def _deliver(job: dict[str, Any], content: str) -> str | None:
    """Best-effort delivery of cron output to the configured channel.

    Returns ``None`` on success or no-op (``notify=None``); returns an error
    string on failure. Failures are logged but never raise — the job result
    is already saved to the output file.
    """
    target = (job.get("notify") or "").strip().lower()
    if not target or target == "local":
        return None

    try:
        from opencomputer.plugins.registry import PluginRegistry
        from plugin_sdk.core import Platform

        registry = PluginRegistry.instance()
        platform_map = {"telegram": Platform.TELEGRAM, "discord": Platform.DISCORD}
        platform = platform_map.get(target.split(":", 1)[0])
        if platform is None:
            return f"unknown notify target {target!r}"

        adapter = registry.get_channel_adapter(platform)
        if adapter is None:
            return f"channel plugin {target!r} not enabled in this profile"

        chat_id = _resolve_chat_id(target)
        if not chat_id:
            return f"no chat_id resolved for {target!r}"

        await adapter.send(chat_id, content)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cron delivery to %s failed: %s", target, exc)
        return str(exc)


def _resolve_chat_id(target: str) -> str | None:
    """Resolve a notify target string to a concrete chat_id.

    ``"telegram"`` → reads ``TELEGRAM_CRON_CHAT_ID`` env var.
    ``"telegram:12345"`` → returns ``"12345"``.
    """
    if ":" in target:
        return target.split(":", 1)[1].strip()
    env_map = {
        "telegram": "TELEGRAM_CRON_CHAT_ID",
        "discord": "DISCORD_CRON_CHANNEL",
    }
    var = env_map.get(target.lower())
    if not var:
        return None
    return os.environ.get(var, "").strip() or None


# ---------------------------------------------------------------------------
# tick() — public single-shot entry point
# ---------------------------------------------------------------------------


async def tick(*, verbose: bool = True) -> int:
    """Find and run all due jobs concurrently. Returns the number executed.

    No-ops (returns 0) if another process holds the tick lock.
    """
    try:
        lock_fd = _acquire_tick_lock()
    except _TickLockHeld:
        logger.debug("Cron tick skipped — another instance holds the lock")
        return 0

    # Phase 0 + Phase 2 v0 system jobs always fire on every tick. They
    # are individually idempotent + gated by data-availability checks
    # so over-running is harmless. A failure inside any one job is
    # logged but doesn't abort the user-cron flow below.
    try:
        from opencomputer.cron.system_jobs import run_system_tick
        run_system_tick()
    except Exception:  # noqa: BLE001
        logger.exception("system_tick failed; continuing with user cron")

    try:
        due = get_due_jobs()
        if not due:
            if verbose:
                logger.info("Cron tick: no jobs due")
            return 0

        if verbose:
            logger.info("Cron tick: %d job(s) due", len(due))

        # Pre-advance recurring jobs BEFORE running so a crash mid-run doesn't
        # cause re-fire on next restart.
        for job in due:
            advance_next_run(job["id"])

        # Run jobs concurrently with bounded parallelism.
        sem = asyncio.Semaphore(_max_parallel())

        async def _run(job: dict[str, Any]) -> bool:
            async with sem:
                return await _process_job(job)

        results = await asyncio.gather(*(_run(j) for j in due), return_exceptions=False)
        return sum(1 for r in results if r)
    finally:
        _release_tick_lock(lock_fd)


async def _process_job(job: dict[str, Any]) -> bool:
    """Run one job end-to-end: execute, save output, deliver, mark result."""
    try:
        success, full_doc, final_response, error = await _run_one_job(job)

        out_file = save_job_output(job["id"], full_doc)
        logger.info("Cron job '%s' output saved to %s", job["name"], out_file)

        deliver_text = final_response if success else f"⚠️ Cron job '{job['name']}' failed:\n{error}"
        delivery_error: str | None = None

        if deliver_text:
            silent = success and SILENT_MARKER in deliver_text.strip().upper()
            if not silent:
                delivery_error = await _deliver(job, deliver_text)

        # Empty response = soft failure
        if success and not final_response:
            success = False
            error = "agent ran but produced no response"

        mark_job_run(job["id"], success, error, delivery_error=delivery_error)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("Cron job %s processing failed", job.get("id", "?"))
        try:
            mark_job_run(job["id"], False, str(exc))
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# run_scheduler_loop() — long-running daemon entry point
# ---------------------------------------------------------------------------


async def run_scheduler_loop(*, interval_s: int = DEFAULT_TICK_INTERVAL_S) -> None:
    """Tick every ``interval_s`` seconds until cancelled.

    Designed to run as an asyncio task inside the gateway daemon, or as the
    sole task in ``opencomputer cron daemon``. Cancellable via task.cancel().
    """
    logger.info("Cron scheduler loop started (interval=%ds)", interval_s)
    try:
        while True:
            try:
                count = await tick(verbose=False)
                if count:
                    logger.info("Cron scheduler ran %d job(s)", count)
            except Exception:
                logger.exception("Cron scheduler tick errored — continuing loop")
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("Cron scheduler loop cancelled")
        raise


__all__ = [
    "DEFAULT_JOB_TIMEOUT_S",
    "DEFAULT_MAX_PARALLEL",
    "DEFAULT_TICK_INTERVAL_S",
    "SILENT_MARKER",
    "run_scheduler_loop",
    "tick",
]
