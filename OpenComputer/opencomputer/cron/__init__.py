"""OpenComputer cron — scheduled agent runs.

Public surface:

- :class:`opencomputer.cron.threats.CronThreatBlocked` — raised when a prompt
  fails the threat scan.
- Storage CRUD (jobs.py): ``create_job``, ``get_job``, ``list_jobs``,
  ``update_job``, ``pause_job``, ``resume_job``, ``trigger_job``,
  ``remove_job``, ``mark_job_run``, ``get_due_jobs``, ``parse_schedule``.
- Scheduler (scheduler.py): ``tick``, ``run_scheduler_loop``.

Storage lives at ``<profile_home>/cron/`` (profile-isolated). All
threats-scanning + capability-claim integration with F1 ConsentGate is
applied through :class:`opencomputer.tools.cron_tool.CronTool`.

See plan: ``~/.claude/plans/toasty-wiggling-eclipse.md`` Tier 1.1.
"""

from __future__ import annotations

from opencomputer.cron.jobs import (
    ONESHOT_GRACE_SECONDS,
    advance_next_run,
    compute_next_run,
    create_job,
    cron_dir,
    get_due_jobs,
    get_job,
    jobs_file,
    list_jobs,
    load_jobs,
    mark_job_run,
    output_dir,
    parse_duration,
    parse_schedule,
    pause_job,
    remove_job,
    resume_job,
    save_job_output,
    save_jobs,
    trigger_job,
    update_job,
)
from opencomputer.cron.scheduler import (
    DEFAULT_JOB_TIMEOUT_S,
    DEFAULT_MAX_PARALLEL,
    DEFAULT_TICK_INTERVAL_S,
    SILENT_MARKER,
    run_scheduler_loop,
    tick,
)
from opencomputer.cron.threats import (
    CronThreatBlocked,
    assert_cron_prompt_safe,
    scan_cron_prompt,
)

__all__ = [
    "DEFAULT_JOB_TIMEOUT_S",
    "DEFAULT_MAX_PARALLEL",
    "DEFAULT_TICK_INTERVAL_S",
    "ONESHOT_GRACE_SECONDS",
    "SILENT_MARKER",
    "CronThreatBlocked",
    "advance_next_run",
    "assert_cron_prompt_safe",
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
    "run_scheduler_loop",
    "save_job_output",
    "save_jobs",
    "scan_cron_prompt",
    "tick",
    "trigger_job",
    "update_job",
]
