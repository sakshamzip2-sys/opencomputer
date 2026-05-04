"""Cron job storage and CRUD.

Jobs are stored in ``<profile_home>/cron/jobs.json`` (JSON file, atomically
written). Per-execution output is saved to
``<profile_home>/cron/output/{job_id}/{timestamp}.md``.

Ported and adapted from `sources/hermes-agent-2026.4.23/cron/jobs.py`
(Hermes Agent project, MIT licensed). The OC port is profile-aware (uses
:func:`opencomputer.agent.config._home`) and integrates with F1 capability
claims via :class:`opencomputer.tools.cron_tool.CronTool`.

Key differences from upstream:
- Profile-isolated storage (``<profile_home>/cron/`` not ``~/.hermes/cron/``).
- All timestamps are ISO 8601 strings with timezone for portability across
  profiles + Docker (no tz drift between mac local and Linux container).
- Threading-safe via in-process :class:`threading.Lock`; the
  :mod:`opencomputer.cron.scheduler` adds a file lock for cross-process safety.
- Threat scanner runs at create + each tick (defence in depth).
- Drops Hermes's per-job model/provider/credential overrides — OC routes
  through the active provider plugin uniformly.
- Drops Hermes's pre-run script feature for the v1 port; can be added later.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from croniter import croniter

from opencomputer.agent.config import _home
from opencomputer.cron.threats import CronThreatBlocked, assert_cron_prompt_safe

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage paths (profile-aware via _home())
# ---------------------------------------------------------------------------


def cron_dir() -> Path:
    """Return ``<profile_home>/cron/``, creating with secure permissions."""
    d = _home() / "cron"
    d.mkdir(parents=True, exist_ok=True)
    _secure_dir(d)
    return d


def jobs_file() -> Path:
    return cron_dir() / "jobs.json"


def output_dir() -> Path:
    d = cron_dir() / "output"
    d.mkdir(parents=True, exist_ok=True)
    _secure_dir(d)
    return d


# In-process lock for load → modify → save sequences. The scheduler
# additionally takes an `flock` so concurrent processes coordinate.
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ONESHOT_GRACE_SECONDS = 120
"""One-shot jobs created N seconds late still fire on the next tick."""


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------


def _secure_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except (OSError, NotImplementedError):
        pass


def _secure_file(path: Path) -> None:
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_aware(dt: datetime) -> datetime:
    """Make naive datetimes UTC-aware so comparisons across timezones work."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$", re.IGNORECASE)
_DURATION_MULTIPLIERS = {"m": 1, "h": 60, "d": 1440}
_CRON_FIELD_RE = re.compile(r"^[\d\*\-,/]+$")


def parse_duration(s: str) -> int:
    """Parse a duration string into minutes.

    Examples: ``"30m"`` → 30, ``"2h"`` → 120, ``"1d"`` → 1440.
    """
    match = _DURATION_RE.match(s.strip())
    if not match:
        raise ValueError(f"Invalid duration {s!r}. Use formats like '30m', '2h', '1d'")
    value = int(match.group(1))
    unit = match.group(2)[0].lower()
    return value * _DURATION_MULTIPLIERS[unit]


def parse_schedule(schedule: str) -> dict[str, Any]:
    """Parse a schedule string into a structured dict.

    Returns a dict with ``kind`` ∈ ``{"once","interval","cron"}`` and the
    fields appropriate to that kind. See module docstring for examples.
    """
    s = schedule.strip()
    s_lower = s.lower()

    if s_lower.startswith("every "):
        minutes = parse_duration(s[6:].strip())
        return {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"}

    parts = s.split()
    if len(parts) >= 5 and all(_CRON_FIELD_RE.match(p) for p in parts[:5]):
        try:
            croniter(s)
        except Exception as exc:
            raise ValueError(f"Invalid cron expression {s!r}: {exc}") from exc
        return {"kind": "cron", "expr": s, "display": s}

    if "T" in s or re.match(r"^\d{4}-\d{2}-\d{2}", s):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid timestamp {s!r}: {exc}") from exc
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return {
            "kind": "once",
            "run_at": dt.isoformat(),
            "display": f"once at {dt.strftime('%Y-%m-%d %H:%M')}",
        }

    try:
        minutes = parse_duration(s)
    except ValueError:
        pass
    else:
        run_at = _now() + timedelta(minutes=minutes)
        return {
            "kind": "once",
            "run_at": run_at.isoformat(),
            "display": f"once in {schedule}",
        }

    raise ValueError(
        f"Invalid schedule {schedule!r}. Use:\n"
        "  - Duration: '30m', '2h', '1d' (one-shot)\n"
        "  - Interval: 'every 30m', 'every 2h' (recurring)\n"
        "  - Cron: '0 9 * * *' (cron expression)\n"
        "  - Timestamp: '2026-02-03T14:00:00' (one-shot at time)"
    )


# ---------------------------------------------------------------------------
# Next-run computation + grace handling
# ---------------------------------------------------------------------------


def _recoverable_oneshot(schedule: dict[str, Any], now: datetime, last_run_at: str | None) -> str | None:
    if schedule.get("kind") != "once" or last_run_at:
        return None
    run_at = schedule.get("run_at")
    if not run_at:
        return None
    run_at_dt = _ensure_aware(datetime.fromisoformat(run_at))
    if run_at_dt >= now - timedelta(seconds=ONESHOT_GRACE_SECONDS):
        return run_at
    return None


def _compute_grace_seconds(schedule: dict[str, Any]) -> int:
    """Half the schedule period, clamped [120, 7200].

    Daily jobs catch up if missed within 2h; 5min jobs only get 2.5min.
    """
    MIN_GRACE = 120
    MAX_GRACE = 7200

    kind = schedule.get("kind")
    if kind == "interval":
        period_seconds = schedule.get("minutes", 1) * 60
        return max(MIN_GRACE, min(period_seconds // 2, MAX_GRACE))

    if kind == "cron":
        try:
            now = _now()
            cron = croniter(schedule["expr"], now)
            first = cron.get_next(datetime)
            second = cron.get_next(datetime)
            period_seconds = int((second - first).total_seconds())
            return max(MIN_GRACE, min(period_seconds // 2, MAX_GRACE))
        except Exception:
            return MIN_GRACE

    return MIN_GRACE


def compute_next_run(schedule: dict[str, Any], last_run_at: str | None = None) -> str | None:
    """Compute the next run time for a schedule, or ``None`` if no more runs."""
    now = _now()

    if schedule["kind"] == "once":
        return _recoverable_oneshot(schedule, now, last_run_at)

    if schedule["kind"] == "interval":
        if last_run_at:
            last = _ensure_aware(datetime.fromisoformat(last_run_at))
            return (last + timedelta(minutes=schedule["minutes"])).isoformat()
        return (now + timedelta(minutes=schedule["minutes"])).isoformat()

    if schedule["kind"] == "cron":
        cron = croniter(schedule["expr"], now)
        return cron.get_next(datetime).isoformat()

    return None


# ---------------------------------------------------------------------------
# Storage I/O
# ---------------------------------------------------------------------------


def load_jobs() -> list[dict[str, Any]]:
    """Load all jobs from disk. Returns ``[]`` if no file exists."""
    f = jobs_file()
    if not f.exists():
        return []
    try:
        with open(f, encoding="utf-8") as fh:
            return json.load(fh).get("jobs", [])
    except json.JSONDecodeError as exc:
        logger.error("Cron jobs.json corrupted: %s", exc)
        raise RuntimeError(f"Cron database corrupted: {exc}") from exc


def save_jobs(jobs: list[dict[str, Any]]) -> None:
    """Atomically write jobs to disk via tmp + os.replace."""
    f = jobs_file()
    fd, tmp_path = tempfile.mkstemp(dir=str(f.parent), suffix=".tmp", prefix=".jobs_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"jobs": jobs, "updated_at": _now().isoformat()}, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, f)
        _secure_file(f)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_job(
    *,
    schedule: str,
    name: str | None = None,
    prompt: str | None = None,
    skill: str | None = None,
    repeat: int | None = None,
    notify: str | None = None,
    plan_mode: bool = True,
    enabled_toolsets: list[str] | None = None,
    context_from: list[str] | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    """Create a new cron job.

    Either ``prompt`` or ``skill`` must be supplied. ``--skill`` is the
    preferred entry path because skills are vetted code; ``--prompt``
    requires the threat scanner to pass.

    Args:
        schedule: Schedule string (see :func:`parse_schedule`).
        name: Optional friendly name (defaults to first chars of prompt/skill).
        prompt: Free-text prompt fed to the agent. Threat-scanned.
        skill: Name of an installed skill. Skill content is loaded at run time.
        repeat: How many times to run; ``None`` = forever (default for
            recurring schedules), ``1`` = once (default for one-shot schedules).
        notify: Where to deliver output. ``None`` = save locally only.
            Channel names: ``"telegram"``, ``"discord"``, ``"webhook:<token>"``.
        plan_mode: When True (default), the cron run starts in plan mode so
            destructive tools require explicit consent. Set False with
            ``--yolo`` only for trusted skills.
        enabled_toolsets: Optional toolset allowlist. ``None`` = all default tools.

    Returns:
        The created job dict.

    Raises:
        ValueError: schedule unparseable or neither ``prompt`` nor ``skill`` set.
        CronThreatBlocked: prompt failed the threat scan.
    """
    if not prompt and not skill:
        raise ValueError("create_job requires either prompt= or skill=")
    if prompt:
        assert_cron_prompt_safe(prompt)

    parsed = parse_schedule(schedule)

    if repeat is not None and repeat <= 0:
        repeat = None
    if parsed["kind"] == "once" and repeat is None:
        repeat = 1

    job_id = uuid.uuid4().hex[:12]
    now_iso = _now().isoformat()
    label = (prompt or skill or "cron job")[:50].strip()

    job = {
        "id": job_id,
        "name": name or label,
        "prompt": prompt,
        "skill": skill,
        "schedule": parsed,
        "schedule_display": parsed["display"],
        "repeat": {"times": repeat, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": now_iso,
        "next_run_at": compute_next_run(parsed),
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "notify": notify,
        "plan_mode": bool(plan_mode),
        "enabled_toolsets": enabled_toolsets,
        # Wave 6.A — Hermes-port (5ac536592 + 852c7f3be).
        # context_from: list of upstream job IDs whose ``last_response`` is
        # prepended into this job's prompt so cron jobs can chain.
        # workdir: optional cwd for the agent during this run; defaults to
        # the current process cwd if unset.
        "context_from": list(context_from) if context_from else None,
        "workdir": workdir,
        # Captured at end of each successful run; consumed by downstream
        # jobs that list this job in their context_from. Empty until the
        # first successful run.
        "last_response": "",
    }

    with _jobs_lock:
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)

    return job


def get_job(job_id: str) -> dict[str, Any] | None:
    """Return one job by id (or ``None``)."""
    for j in load_jobs():
        if j["id"] == job_id:
            return j
    return None


def list_jobs(*, include_disabled: bool = False) -> list[dict[str, Any]]:
    """List jobs. By default hides disabled (paused / completed) jobs."""
    jobs = load_jobs()
    if not include_disabled:
        jobs = [j for j in jobs if j.get("enabled", True)]
    return jobs


def update_job(job_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    """Update a job by id, refreshing schedule fields when changed."""
    with _jobs_lock:
        jobs = load_jobs()
        for i, job in enumerate(jobs):
            if job["id"] != job_id:
                continue

            updated = {**job, **updates}
            schedule_changed = "schedule" in updates

            if schedule_changed:
                sched = updated["schedule"]
                if isinstance(sched, str):
                    sched = parse_schedule(sched)
                    updated["schedule"] = sched
                updated["schedule_display"] = sched["display"]
                if updated.get("state") != "paused":
                    updated["next_run_at"] = compute_next_run(sched)

            if updated.get("enabled", True) and not updated.get("next_run_at"):
                updated["next_run_at"] = compute_next_run(updated["schedule"])

            jobs[i] = updated
            save_jobs(jobs)
            return updated
    return None


def pause_job(job_id: str, reason: str | None = None) -> dict[str, Any] | None:
    return update_job(
        job_id,
        {
            "enabled": False,
            "state": "paused",
            "paused_at": _now().isoformat(),
            "paused_reason": reason,
        },
    )


def resume_job(job_id: str) -> dict[str, Any] | None:
    job = get_job(job_id)
    if not job:
        return None
    return update_job(
        job_id,
        {
            "enabled": True,
            "state": "scheduled",
            "paused_at": None,
            "paused_reason": None,
            "next_run_at": compute_next_run(job["schedule"]),
        },
    )


def trigger_job(job_id: str) -> dict[str, Any] | None:
    """Schedule a job to run on the next tick (regardless of its schedule)."""
    job = get_job(job_id)
    if not job:
        return None
    return update_job(
        job_id,
        {
            "enabled": True,
            "state": "scheduled",
            "paused_at": None,
            "paused_reason": None,
            "next_run_at": _now().isoformat(),
        },
    )


def remove_job(job_id: str) -> bool:
    with _jobs_lock:
        jobs = load_jobs()
        original = len(jobs)
        jobs = [j for j in jobs if j["id"] != job_id]
        if len(jobs) < original:
            save_jobs(jobs)
            return True
    return False


def mark_job_run(
    job_id: str,
    success: bool,
    error: str | None = None,
    delivery_error: str | None = None,
    response: str | None = None,
) -> None:
    """Record the outcome of a job run + advance next_run_at.

    ``response`` is the assistant's final message text for this run; saved
    as ``last_response`` so downstream jobs that list this job in their
    ``context_from`` can pull it in. ``None`` leaves the prior value.
    """
    with _jobs_lock:
        jobs = load_jobs()
        for i, job in enumerate(jobs):
            if job["id"] != job_id:
                continue

            now_iso = _now().isoformat()
            job["last_run_at"] = now_iso
            job["last_status"] = "ok" if success else "error"
            job["last_error"] = error if not success else None
            job["last_delivery_error"] = delivery_error
            if response is not None:
                # Cap stored response so a runaway 1MB job output doesn't
                # bloat jobs.json. 8KB is generous for downstream prompt
                # injection without overwhelming the next job's context.
                job["last_response"] = (response or "")[:8192]

            if job.get("repeat"):
                job["repeat"]["completed"] = job["repeat"].get("completed", 0) + 1
                times = job["repeat"].get("times")
                completed = job["repeat"]["completed"]
                if times is not None and times > 0 and completed >= times:
                    jobs.pop(i)
                    save_jobs(jobs)
                    return

            job["next_run_at"] = compute_next_run(job["schedule"], now_iso)
            if job["next_run_at"] is None:
                job["enabled"] = False
                job["state"] = "completed"
            elif job.get("state") != "paused":
                job["state"] = "scheduled"

            save_jobs(jobs)
            return

        logger.warning("mark_job_run: job_id %s not found, skipping save", job_id)


def advance_next_run(job_id: str) -> bool:
    """Pre-advance ``next_run_at`` for a recurring job before execution.

    Call BEFORE running so a crash mid-run doesn't cause the job to re-fire
    on next gateway restart. Converts at-least-once → at-most-once for
    recurring schedules. One-shot jobs are left alone so they can retry.
    """
    with _jobs_lock:
        jobs = load_jobs()
        for job in jobs:
            if job["id"] != job_id:
                continue
            kind = job.get("schedule", {}).get("kind")
            if kind not in ("cron", "interval"):
                return False
            now_iso = _now().isoformat()
            new_next = compute_next_run(job["schedule"], now_iso)
            if new_next and new_next != job.get("next_run_at"):
                job["next_run_at"] = new_next
                save_jobs(jobs)
                return True
    return False


def get_due_jobs() -> list[dict[str, Any]]:
    """Return jobs ready to run now.

    Stale recurring jobs (past their grace window) are fast-forwarded to the
    next future occurrence rather than re-fired, preventing burst replays
    after a long downtime.
    """
    now = _now()
    raw_jobs = load_jobs()
    jobs = copy.deepcopy(raw_jobs)
    due: list[dict[str, Any]] = []
    needs_save = False

    for job in jobs:
        if not job.get("enabled", True):
            continue

        next_run = job.get("next_run_at")
        if not next_run:
            recovered = _recoverable_oneshot(
                job.get("schedule", {}),
                now,
                last_run_at=job.get("last_run_at"),
            )
            if not recovered:
                continue
            next_run = recovered
            for rj in raw_jobs:
                if rj["id"] == job["id"]:
                    rj["next_run_at"] = recovered
                    needs_save = True
                    break

        next_run_dt = _ensure_aware(datetime.fromisoformat(next_run))
        if next_run_dt > now:
            continue

        schedule = job.get("schedule", {})
        kind = schedule.get("kind")
        grace = _compute_grace_seconds(schedule)

        if kind in ("cron", "interval") and (now - next_run_dt).total_seconds() > grace:
            new_next = compute_next_run(schedule, now.isoformat())
            if new_next:
                logger.info(
                    "Cron job '%s' missed window (next_run=%s, grace=%ds); fast-forwarding to %s",
                    job.get("name", job["id"]),
                    next_run,
                    grace,
                    new_next,
                )
                for rj in raw_jobs:
                    if rj["id"] == job["id"]:
                        rj["next_run_at"] = new_next
                        needs_save = True
                        break
                continue

        due.append(job)

    if needs_save:
        save_jobs(raw_jobs)

    return due


# ---------------------------------------------------------------------------
# Output persistence
# ---------------------------------------------------------------------------


def save_job_output(job_id: str, output: str) -> Path:
    """Save a job execution's output to ``<output_dir>/<job_id>/<ts>.md``."""
    job_out = output_dir() / job_id
    job_out.mkdir(parents=True, exist_ok=True)
    _secure_dir(job_out)

    ts = _now().strftime("%Y-%m-%d_%H-%M-%S")
    out_file = job_out / f"{ts}.md"

    fd, tmp_path = tempfile.mkstemp(dir=str(job_out), suffix=".tmp", prefix=".output_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(output)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, out_file)
        _secure_file(out_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return out_file


__all__ = [
    "ONESHOT_GRACE_SECONDS",
    "CronThreatBlocked",
    "advance_next_run",
    "compute_next_run",
    "create_job",
    "cron_dir",
    "get_due_jobs",
    "get_job",
    "jobs_file",
    "list_jobs",
    "load_jobs",
    "mark_job_run",
    "output_dir",
    "parse_duration",
    "parse_schedule",
    "pause_job",
    "remove_job",
    "resume_job",
    "save_job_output",
    "save_jobs",
    "trigger_job",
    "update_job",
]
