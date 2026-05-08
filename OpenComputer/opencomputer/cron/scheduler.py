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

Hermes parity (2026-05-08):
- :func:`_parse_wake_agent_marker` — last-line ``{"wakeAgent": false}`` JSON
  marker on agent-path output suppresses delivery (silent tick).
- :func:`_run_script_only` — ``--no-agent`` / ``--script`` script-only mode
  bypasses the LLM entirely.
- ``cron.wrap_response`` config — opt-in delivery wrap.
- ``cron.script_timeout_seconds`` config — default timeout for script jobs.
"""

from __future__ import annotations

import asyncio
import json
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
DEFAULT_JOB_TIMEOUT_S = 2400
"""40 minutes per job (2026-05-05: doubled 1200 → 2400 in the cap-doubling sweep; previously doubled 600 → 1200 on 2026-05-04); can be overridden via env var ``OPENCOMPUTER_CRON_TIMEOUT``."""

DEFAULT_MAX_PARALLEL = 6
"""2x Hermes default after 2026-05-05 cap-doubling sweep (was 3)."""

DEFAULT_SCRIPT_TIMEOUT_S = 120
"""Default ``--no-agent`` script timeout. Overridden by ``cron.script_timeout_seconds``
config or per-job ``script_timeout_seconds`` field. Hermes parity."""


def _parse_wake_agent_marker(text: str) -> bool:
    """Hermes parity: parse last non-empty stdout line as JSON.

    If it's a dict with key ``wakeAgent`` set to ``False``, the scheduler
    suppresses delivery for this tick (treat as silent).

    Returns ``True`` (default — proceed with delivery) for:
    - empty/whitespace-only output
    - last line not valid JSON
    - last line valid JSON but not a dict
    - dict has no ``wakeAgent`` key
    - dict has ``wakeAgent: True``

    Returns ``False`` only when the last non-empty line is a JSON dict with
    ``wakeAgent: false``.
    """
    if not text:
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True
    last = lines[-1].strip()
    try:
        parsed = json.loads(last)
    except (json.JSONDecodeError, ValueError):
        return True
    if not isinstance(parsed, dict):
        return True
    return bool(parsed.get("wakeAgent", True))


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

    Hermes parity (2026-05-08): ``enabled_toolsets`` on the job dict
    becomes ``loop.allowed_tools``. ``None`` = inherit full tool set;
    ``[]`` = no tools (pure-reasoning cron); list of names = only those
    tools dispatchable. Closes the silent gap where the field was stored
    on the job but never applied at run time.

    Two latent bugs fixed alongside:
      1. ``Config.with_loop_overrides`` doesn't exist (Config is a frozen
         dataclass) — uses ``dataclasses.replace`` instead.
      2. ``AgentLoop(config=cfg)`` is missing the required ``provider``
         arg — resolves via ``_resolve_provider`` like the rest of the
         codebase. When the registry isn't loaded yet (early test
         contexts), the provider lookup is best-effort and the AgentLoop
         construction is skipped — caller gets a stub object instead.
    """
    import dataclasses

    from opencomputer.agent.config_store import load_config
    from opencomputer.agent.loop import AgentLoop

    cfg = load_config()

    # Cron sessions are short-lived; cap iterations tighter than interactive default.
    capped_iters = min(cfg.loop.max_iterations, 30)
    if capped_iters != cfg.loop.max_iterations:
        new_loop = dataclasses.replace(cfg.loop, max_iterations=capped_iters)
        cfg = dataclasses.replace(cfg, loop=new_loop)

    # Resolve provider from the active config. Best-effort: in test contexts
    # without a loaded registry/plugins, fall back to a stub object that
    # carries the toolset allowlist for tests, but won't actually run an
    # agent loop. Production cron always has plugins loaded by this point.
    try:
        from opencomputer.cli import _resolve_provider
        provider = _resolve_provider(cfg.model.provider)
        loop = AgentLoop(provider=provider, config=cfg)
    except Exception:  # noqa: BLE001 — registry/plugin resolution may fail in tests
        # Stub: a minimal namespace exposing only ``allowed_tools`` and
        # ``config``, plus a non-functional run_conversation that raises.
        # Real cron flow won't hit this path because plugins are loaded
        # at gateway/CLI bootstrap before the cron tick fires.
        from types import SimpleNamespace

        async def _no_provider(*_args, **_kwargs):
            raise RuntimeError("cron _build_agent_loop: no provider resolved")

        loop = SimpleNamespace(
            allowed_tools=None,
            config=cfg,
            run_conversation=_no_provider,
        )

    # Hermes parity: enabled_toolsets actually applied at run time.
    toolsets = job.get("enabled_toolsets")
    if toolsets is not None:
        loop.allowed_tools = frozenset(toolsets)

    return loop


def _build_context_from_block(job: dict[str, Any]) -> str:
    """Wave 6.A — build the upstream-context block for ``context_from``.

    For each upstream job ID listed in ``job['context_from']``, look up
    that job's ``last_response`` from jobs.json and emit a tagged block.
    Missing or empty upstream responses are skipped. Empty result means
    no upstream context (job runs as if context_from was unset).
    """
    refs = job.get("context_from") or []
    if not refs:
        return ""
    from opencomputer.cron.jobs import load_jobs

    blocks: list[str] = []
    by_id = {j["id"]: j for j in load_jobs()}
    for ref in refs:
        upstream = by_id.get(ref)
        if upstream is None:
            continue
        last = (upstream.get("last_response") or "").strip()
        if not last:
            continue
        blocks.append(
            f"[CONTEXT FROM upstream cron job '{upstream.get('name', ref)}' "
            f"(id={ref}, last run {upstream.get('last_run_at', 'never')}):\n"
            f"{last}\n]"
        )
    return ("\n\n".join(blocks) + "\n\n") if blocks else ""


def _build_run_prompt(job: dict[str, Any]) -> str:
    """Construct the user prompt the agent should answer for this run.

    For ``--prompt`` jobs: returns the prompt verbatim with a cron-context header.
    For ``--skill`` jobs: returns "use the X skill" so the agent self-invokes.
    Wave 6.A: ``context_from`` block (if any) is prepended after the cron
    hint and before the user prompt.
    """
    cron_hint = (
        "[SYSTEM: You are running as a scheduled cron job. "
        "DELIVERY: Your final response will be automatically delivered to the "
        "configured channel — do NOT call send_message yourself. "
        "SILENT: If there is genuinely nothing new to report, respond with "
        'exactly "[SILENT]" (nothing else) to suppress delivery.]\n\n'
    )
    upstream = _build_context_from_block(job)
    if job.get("skill"):
        return f"{cron_hint}{upstream}Use the `{job['skill']}` skill and report your findings."
    return cron_hint + upstream + (job.get("prompt") or "")


async def _run_one_job(job: dict[str, Any]) -> tuple[bool, str, str, str | None]:
    """Run a single cron job. Returns ``(success, full_doc, final_response, error)``.

    Hermes parity (2026-05-08): branches on ``no_agent`` to ``_run_script_only``
    when the job is script-only (no LLM invocation). The agent path also
    honors a final ``{"wakeAgent": false}`` JSON marker as a silent tick.

    Defence-in-depth: re-scan the prompt for threats before invoking the
    agent. A poisoned prompt that survived create-time scanning (e.g.
    via direct file edit) is blocked here too.
    """
    # Hermes parity: --no-agent / --script branch (skip LLM entirely).
    if job.get("no_agent"):
        return await _run_script_only(job)

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

    # Wave 6.A — per-job workdir. Apply via os.chdir for the agent run,
    # restore after. Best-effort: missing/invalid workdir falls through
    # to the parent-process cwd with a warning.
    import os as _os

    saved_cwd = _os.getcwd()
    workdir = job.get("workdir")
    if workdir:
        try:
            _os.chdir(workdir)
        except OSError as exc:
            logger.warning(
                "cron job %s workdir=%r unusable (%s); using process cwd",
                job_id,
                workdir,
                exc,
            )

    try:
        loop = await _build_agent_loop(job)
        runtime = RuntimeContext(
            plan_mode=bool(job.get("plan_mode", True)),
            yolo_mode=False,
            agent_context="cron",
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
        try:
            _os.chdir(saved_cwd)
        except OSError:
            pass
        return False, _failed_doc(job, error), "", error
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("Cron job '%s' (id=%s) failed: %s", job_name, job_id, error)
        try:
            _os.chdir(saved_cwd)
        except OSError:
            pass
        return False, _failed_doc(job, error), "", error
    finally:
        # Restore process cwd unconditionally — a job's workdir change
        # must NOT bleed into subsequent ticks of the scheduler.
        try:
            _os.chdir(saved_cwd)
        except OSError:
            pass

    final = (result.final_message.content if result and result.final_message else "") or ""
    if final.strip() == "(No response generated)":
        final = ""

    # Hermes parity: wakeAgent: false marker on last stdout line suppresses
    # delivery (silent tick). The marker is removed from the saved doc too.
    if not _parse_wake_agent_marker(final):
        return True, "", SILENT_MARKER, None

    doc = _success_doc(job, full_prompt, final or "(empty response)")
    return True, doc, final, None


# ---------------------------------------------------------------------------
# Script-only jobs (Hermes parity, 2026-05-08)
# ---------------------------------------------------------------------------


async def _run_script_only(
    job: dict[str, Any],
) -> tuple[bool, str, str, str | None]:
    """Hermes parity: ``--no-agent`` / ``--script`` script-only execution.

    Runs a shell script under ``<profile_home>/scripts/<name>`` with a
    timeout (``script_timeout_seconds`` per-job override, else
    ``cron.script_timeout_seconds`` config, else
    :data:`DEFAULT_SCRIPT_TIMEOUT_S`). No LLM invocation.

    Returns ``(success, full_doc, response_text, error)`` matching the
    agent-path return shape so ``_process_job`` doesn't need to branch.

    Behavior:
        Empty stdout (zero exit) → silent tick (``response_text =
        SILENT_MARKER``). Caller suppresses delivery.
        Non-empty stdout (zero exit) → response_text is stdout (rstripped).
        Non-zero exit → ``success=False``, ``error`` includes exit code +
        first 500 chars of stdout.
        Timeout → ``success=False``, error includes timeout value.
        Script not found → ``success=False``, error names the path.
    """
    from opencomputer.agent.config import _home

    job_name = job.get("name", "?")
    script_name = (job.get("script") or "").strip()
    if not script_name:
        error = "no_agent=True but no script supplied"
        return False, _failed_doc(job, error), "", error

    scripts_dir = _home() / "scripts"
    script_path = scripts_dir / script_name
    if not script_path.exists():
        error = f"script {script_name!r} not found at {script_path}"
        return False, _failed_doc(job, error), "", error

    timeout = job.get("script_timeout_seconds")
    if timeout is None:
        try:
            from opencomputer.agent.config_store import load_config
            cfg = load_config()
            timeout = getattr(cfg.cron, "script_timeout_seconds", DEFAULT_SCRIPT_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            timeout = DEFAULT_SCRIPT_TIMEOUT_S

    cwd = job.get("workdir") or None

    try:
        proc = await asyncio.create_subprocess_exec(
            str(script_path),
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        error = f"failed to launch script {script_name!r}: {exc}"
        return False, _failed_doc(job, error), "", error

    try:
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(), timeout=float(timeout)
        )
    except TimeoutError:
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except TimeoutError:
                proc.kill()
        except ProcessLookupError:
            pass
        error = f"script {script_name!r} exceeded {timeout}s timeout"
        logger.warning("Cron job '%s' (id=%s): %s", job_name, job["id"], error)
        return False, _failed_doc(job, error), "", error

    output_text = stdout_bytes.decode("utf-8", errors="replace").rstrip()
    if proc.returncode != 0:
        # Truncate the first 500 chars of output into the error to keep
        # delivery messages readable.
        snippet = output_text[:500]
        error = f"script {script_name!r} exited {proc.returncode}: {snippet}"
        logger.warning("Cron job '%s' (id=%s): %s", job_name, job["id"], error)
        return False, _failed_doc(job, error), output_text, error

    if not output_text.strip():
        # Empty stdout = silent tick (Hermes pattern — common for watchdogs).
        return True, _success_doc(job, f"[script: {script_name}]", "(silent tick)"), SILENT_MARKER, None

    return True, _success_doc(job, f"[script: {script_name}]", output_text), output_text, None


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

    Hermes parity (2026-05-08): any channel registered with the plugin
    registry is a valid notify target — the spec lists 17+ platforms
    (telegram, discord, slack, whatsapp, signal, matrix, mattermost,
    email, sms, homeassistant, dingtalk, feishu, wecom, weixin, qqbot,
    teams, irc, webhook, etc.). Lookup goes through the module-level
    ``registry.channels`` dict (the canonical singleton — there is no
    ``PluginRegistry.instance()`` classmethod; the prior code had a
    latent bug that only manifested if an unknown target was provided).

    Special targets:
        ``"local"`` / ``""`` / ``None`` → no-op (saved locally only).
        ``"origin"`` → use the originating chat captured at create time
            (``origin_platform`` + ``origin_chat_id``); falls through to
            local-save when origin context is absent.

    Returns ``None`` on success / no-op; returns an error string on
    failure. Failures are logged but never raise — the job's output
    file is already saved.
    """
    target = (job.get("notify") or "").strip().lower()
    if not target or target == "local":
        return None

    # Hermes parity: notify="origin" → resolve to platform:chat_id captured
    # at create time. Falls through to local-save (None) silently when the
    # origin context is missing — matches Hermes behavior of "default to
    # local for non-messaging-spawned jobs."
    if target == "origin":
        plat = (job.get("origin_platform") or "").strip().lower()
        chat = (job.get("origin_chat_id") or "").strip()
        if not plat or not chat:
            logger.info(
                "Cron job %s notify=origin but origin context missing; "
                "saving locally only",
                job.get("id", "?"),
            )
            return None
        target = f"{plat}:{chat}"

    try:
        from opencomputer.plugins.registry import registry as plugin_registry
        from plugin_sdk.core import Platform

        plat_str, _, suffix = target.partition(":")

        try:
            Platform(plat_str)  # validates against the enum
        except ValueError:
            return f"unknown notify target {target!r} (not in Platform enum)"

        adapter = plugin_registry.channels.get(plat_str)
        if adapter is None:
            return f"channel plugin {plat_str!r} not enabled in this profile"

        chat_id = suffix.strip() or _resolve_default_chat_id(plat_str)
        if not chat_id:
            return f"no chat_id resolved for {target!r}; use {plat_str}:<chat_id>"

        await adapter.send(chat_id, content)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cron delivery to %s failed: %s", target, exc)
        return str(exc)


def _resolve_default_chat_id(platform: str) -> str | None:
    """Resolve a bare ``"telegram"`` / ``"discord"`` to its env-var fallback.

    Other platforms have no env shortcut — callers must use the
    ``<platform>:<chat_id>`` form.
    """
    env_map = {
        "telegram": "TELEGRAM_CRON_CHAT_ID",
        "discord": "DISCORD_CRON_CHANNEL",
    }
    var = env_map.get(platform.lower())
    if not var:
        return None
    return os.environ.get(var, "").strip() or None


# Back-compat alias — older tests/scripts may have imported the old name.
_resolve_chat_id = _resolve_default_chat_id


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

        # full_doc may be empty for silent-tick paths (script-only empty
        # stdout, wakeAgent: false agent response). Skip the file save in
        # that case so we don't accumulate empty output dumps.
        if full_doc:
            out_file = save_job_output(job["id"], full_doc)
            logger.info("Cron job '%s' output saved to %s", job["name"], out_file)

        # Hermes parity: cron.wrap_response controls delivery shape.
        # Default False = raw response (existing OC behavior). True =
        # delivered text wraps in the same Markdown header that the saved
        # output file uses (job name, run time, schedule).
        wrap_response = False
        try:
            from opencomputer.agent.config_store import load_config
            cfg = load_config()
            wrap_response = bool(getattr(cfg.cron, "wrap_response", False))
        except Exception:  # noqa: BLE001
            pass

        if success:
            deliver_text = full_doc if wrap_response else final_response
        else:
            deliver_text = f"⚠️ Cron job '{job['name']}' failed:\n{error}"
        delivery_error: str | None = None

        if deliver_text:
            silent = success and SILENT_MARKER in deliver_text.strip().upper()
            if not silent:
                delivery_error = await _deliver(job, deliver_text)

        # Empty response = soft failure (only for agent-path; script-path
        # silent tick correctly carries SILENT_MARKER as final_response).
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
    "DEFAULT_SCRIPT_TIMEOUT_S",
    "DEFAULT_TICK_INTERVAL_S",
    "SILENT_MARKER",
    "run_scheduler_loop",
    "tick",
]
