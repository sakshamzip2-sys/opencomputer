"""``/background <prompt>`` — Hermes-parity isolated background turn.

Three sub-actions:

* ``/background <prompt>`` (or ``/background start <prompt>``) — kick off
  a fresh AgentLoop on a daemon thread and return the job id immediately.
* ``/background list`` — show recent jobs (newest first).
* ``/background show <job-id>`` — print the full result (or error) of one job.

Hermes-parity adapter notes:

* "No shared history" guarantee: the worker spawns a fresh session id, so
  the background turn never reads or writes the foreground session.
* "Inherits model, provider, toolsets" guarantee: the registered
  ``AgentLoop`` factory captures the currently-resolved provider + config
  at CLI/Gateway startup, so background turns use the same model/provider
  as the foreground.
* "Result appears as inline panel on completion" — MVP captures the result
  but does NOT push to the originating channel. Use ``/background show
  <id>`` to retrieve. Push-on-completion is a follow-up that needs adapter
  routing.
"""

from __future__ import annotations

from datetime import datetime, timezone

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


def _format_started_at(epoch: float) -> str:
    """Render an absolute epoch timestamp as a short HH:MM:SS UTC marker."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M:%S")


def _format_duration(start: float, end: float | None) -> str:
    if end is None:
        return "running"
    elapsed = max(0.0, end - start)
    if elapsed < 60.0:
        return f"{elapsed:.1f}s"
    return f"{elapsed / 60.0:.1f}m"


class BackgroundCommand(SlashCommand):
    name = "background"
    description = (
        "Run a turn in the background (start <prompt> | list | show <id>)"
    )
    aliases = ("bg",)

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        # Lazy import — the registry pulls in threading + asyncio internals
        # we don't want unconditionally loaded for callers that never use
        # /background.
        from opencomputer.agent.background_jobs import get_default_registry

        registry = get_default_registry()
        text = (args or "").strip()
        if not text:
            return SlashCommandResult(
                output=(
                    "/background — Hermes-parity isolated turn\n"
                    "  /background <prompt>     start a new background job\n"
                    "  /background list         show recent jobs\n"
                    "  /background show <id>    print one job's result"
                ),
                handled=True,
            )

        # The two reserved sub-verbs are 'list' and 'show'. Anything else
        # is treated as a prompt (with optional 'start ' prefix).
        head, _, rest = text.partition(" ")
        head_l = head.lower()

        if head_l == "list":
            return self._render_list(registry)
        if head_l == "show":
            return self._render_show(registry, rest.strip())
        if head_l == "start":
            prompt = rest.strip()
        else:
            prompt = text

        if not prompt:
            return SlashCommandResult(
                output="/background: empty prompt — pass the text after the verb",
                handled=True,
            )

        if not registry.factory_registered:
            return SlashCommandResult(
                output=(
                    "/background: AgentLoop factory not registered — only "
                    "available inside a live ``oc chat`` / ``oc gateway`` "
                    "session, not in tests or contexts that bypass the CLI "
                    "entrypoint."
                ),
                handled=True,
            )

        plan_mode = bool(getattr(runtime, "plan_mode", False))
        try:
            job_id = registry.submit(prompt, plan=plan_mode)
        except (ValueError, RuntimeError) as e:
            return SlashCommandResult(
                output=f"/background: {e}",
                handled=True,
            )

        return SlashCommandResult(
            output=(
                f"started background job {job_id} (plan_mode={plan_mode})\n"
                f"check with `/background show {job_id}`"
            ),
            handled=True,
        )

    def _render_list(self, registry) -> SlashCommandResult:
        jobs = registry.list_recent(limit=20)
        if not jobs:
            return SlashCommandResult(
                output="no background jobs yet",
                handled=True,
            )
        lines = ["recent background jobs (newest first):"]
        for j in jobs:
            head = j.prompt.splitlines()[0] if j.prompt else ""
            if len(head) > 60:
                head = head[:57] + "…"
            lines.append(
                f"  {j.job_id}  {j.status:<8}  "
                f"{_format_started_at(j.started_at)}  "
                f"{_format_duration(j.started_at, j.completed_at):<8}  {head}"
            )
        return SlashCommandResult(output="\n".join(lines), handled=True)

    def _render_show(self, registry, job_id: str) -> SlashCommandResult:
        if not job_id:
            return SlashCommandResult(
                output="/background show: missing job id (try `/background list`)",
                handled=True,
            )
        job = registry.get(job_id)
        if job is None:
            return SlashCommandResult(
                output=f"/background show: no job with id {job_id!r}",
                handled=True,
            )
        head = (
            f"job {job.job_id}  status={job.status}  "
            f"started={_format_started_at(job.started_at)}  "
            f"elapsed={_format_duration(job.started_at, job.completed_at)}"
        )
        if job.iterations is not None:
            head += f"  iters={job.iterations}"
        if job.session_id:
            head += f"  session={job.session_id[:12]}…"
        body_lines = [head, "", f"prompt: {job.prompt}"]
        if job.status == "running":
            body_lines.append("\n(still running — check again in a moment)")
        elif job.status == "error":
            body_lines.append(f"\nerror: {job.error or '(no detail)'}")
        elif job.status == "complete":
            body_lines.append("\n--- result ---")
            body_lines.append(job.result or "(empty response)")
        else:  # pending
            body_lines.append("\n(pending — worker thread not yet picked up)")
        return SlashCommandResult(output="\n".join(body_lines), handled=True)


__all__ = ["BackgroundCommand"]
