# Hermes Cron + Delegation — Long-Tail Finishers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close 8 honest gaps between the Hermes Cron & Delegation spec and current OpenComputer (silent feature gaps, hardcoded channel map, missing CLI/slash verbs, broken /cron handler, multi-skill jobs, blocklist parity, origin delivery).

**Architecture:** No new modules. Surgical edits to 6 existing files (`cron/jobs.py`, `cron/scheduler.py`, `cli_cron.py`, `tools/cron_tool.py`, `tools/delegate.py`, `cli_ui/slash_handlers.py`). Field additions on the job dict are optional and back-compat. Reuses Platform enum + registry for delivery generalization.

**Tech Stack:** Python 3.12+, asyncio, croniter, Typer, Rich, pytest. Uses existing OC plugin SDK (`Platform`, `RuntimeContext`), session_context contextvars, and PluginRegistry.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `OpenComputer/opencomputer/cron/jobs.py` | Job CRUD + storage. Add `skills`, `origin_*` fields. Edit helper. | Modify |
| `OpenComputer/opencomputer/cron/scheduler.py` | Run loop + delivery. Generalize `_deliver`. Apply `enabled_toolsets`. Multi-skill prompt. Origin lookup. | Modify |
| `OpenComputer/opencomputer/cli_cron.py` | CLI surface. Repeat `--skill`. New `edit` subcommand. | Modify |
| `OpenComputer/opencomputer/tools/cron_tool.py` | Agent-callable tool. `skills` array in schema. Origin capture from session_context. | Modify |
| `OpenComputer/opencomputer/tools/delegate.py` | DELEGATE_BLOCKED_TOOLS expansion. | Modify |
| `OpenComputer/opencomputer/cli_ui/slash_handlers.py` | `/cron` bug fix + subcommand routing. New `/agents` read-only tree. | Modify |
| `OpenComputer/tests/test_cron_delivery_generic.py` | Platform enum lookup tests. | Create |
| `OpenComputer/tests/test_cron_enabled_toolsets_runtime.py` | Toolset propagation tests. | Create |
| `OpenComputer/tests/test_cron_multi_skill.py` | Skills list creation + prompt tests. | Create |
| `OpenComputer/tests/test_cron_edit_cli.py` | Edit subcommand tests. | Create |
| `OpenComputer/tests/test_cron_slash_subcommands.py` | Slash routing tests. | Create |
| `OpenComputer/tests/test_delegate_blocklist_extended.py` | Blocklist parity tests. | Create |
| `OpenComputer/tests/test_cron_origin_delivery.py` | Origin capture + delivery tests. | Create |
| `OpenComputer/tests/test_agents_slash.py` | /agents tree rendering tests. | Create |

---

## Task 1: Generalize delivery — Platform enum lookup

**Files:**
- Modify: `OpenComputer/opencomputer/cron/scheduler.py:476-527` (`_deliver`, `_resolve_chat_id`)
- Test: `OpenComputer/tests/test_cron_delivery_generic.py`

- [ ] **Step 1: Write the failing test**

Create `OpenComputer/tests/test_cron_delivery_generic.py`:

```python
"""Hermes parity: cron delivery generalized to all Platform enum channels."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.cron.scheduler import _deliver


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_str", ["slack", "matrix", "mattermost", "email", "signal", "whatsapp"])
async def test_deliver_routes_to_platform_via_registry(platform_str):
    job = {"id": "j1", "name": "n", "notify": f"{platform_str}:#channel-1"}
    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock(return_value=None)
    with patch("opencomputer.plugins.registry.PluginRegistry.instance") as inst:
        registry = MagicMock()
        registry.get_channel_adapter = MagicMock(return_value=fake_adapter)
        inst.return_value = registry
        err = await _deliver(job, "hello")
    assert err is None
    fake_adapter.send.assert_awaited_once_with("#channel-1", "hello")


@pytest.mark.asyncio
async def test_deliver_unknown_platform_returns_error():
    job = {"id": "j1", "name": "n", "notify": "made_up_platform:1234"}
    err = await _deliver(job, "hello")
    assert err is not None
    assert "unknown" in err.lower()


@pytest.mark.asyncio
async def test_deliver_missing_adapter_returns_error():
    job = {"id": "j1", "name": "n", "notify": "slack:#x"}
    with patch("opencomputer.plugins.registry.PluginRegistry.instance") as inst:
        registry = MagicMock()
        registry.get_channel_adapter = MagicMock(return_value=None)
        inst.return_value = registry
        err = await _deliver(job, "hello")
    assert err is not None
    assert "not enabled" in err.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd OpenComputer && pytest tests/test_cron_delivery_generic.py -v
```

Expected: FAIL — current `_deliver` only knows telegram/discord, returns `"unknown notify target"` for slack/etc.

- [ ] **Step 3: Replace `_deliver` and `_resolve_chat_id` with Platform enum lookup**

In `OpenComputer/opencomputer/cron/scheduler.py`, replace lines 476–527 (the two functions) with:

```python
async def _deliver(job: dict[str, Any], content: str) -> str | None:
    """Best-effort delivery of cron output to the configured channel.

    Hermes parity: any channel registered with the PluginRegistry is a
    valid notify target — the spec lists 17+ platforms (telegram, discord,
    slack, whatsapp, signal, matrix, mattermost, email, sms, homeassistant,
    dingtalk, feishu, wecom, weixin, qqbot, teams, irc, webhook).

    Special targets:
        ``"local"`` / ``""`` / ``None`` → no-op (saved locally only).
        ``"origin"`` → use the originating chat captured at create time
            (``origin_platform`` + ``origin_chat_id``); falls through to
            local-save when origin context is absent.

    Returns ``None`` on success / no-op; returns an error string on failure.
    """
    target = (job.get("notify") or "").strip().lower()
    if not target or target == "local":
        return None

    # Hermes parity: notify="origin" → resolve to platform:chat_id captured at create time.
    if target == "origin":
        plat = (job.get("origin_platform") or "").strip().lower()
        chat = (job.get("origin_chat_id") or "").strip()
        if not plat or not chat:
            logger.info(
                "Cron job %s notify=origin but origin context missing; saving locally only",
                job.get("id", "?"),
            )
            return None
        target = f"{plat}:{chat}"

    try:
        from opencomputer.plugins.registry import PluginRegistry
        from plugin_sdk.core import Platform

        registry = PluginRegistry.instance()
        plat_str, _, suffix = target.partition(":")

        try:
            platform = Platform(plat_str)
        except ValueError:
            return f"unknown notify target {target!r} (not in Platform enum)"

        adapter = registry.get_channel_adapter(platform)
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

    Other platforms have no env shortcut — callers must use ``<platform>:<chat_id>``.
    """
    env_map = {
        "telegram": "TELEGRAM_CRON_CHAT_ID",
        "discord": "DISCORD_CRON_CHANNEL",
    }
    var = env_map.get(platform.lower())
    if not var:
        return None
    return os.environ.get(var, "").strip() or None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd OpenComputer && pytest tests/test_cron_delivery_generic.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cron/scheduler.py OpenComputer/tests/test_cron_delivery_generic.py
git commit -m "feat(cron): generalize notify delivery via Platform enum (Hermes parity)"
```

---

## Task 2: Apply enabled_toolsets at run time (silent-gap fix)

**Files:**
- Modify: `OpenComputer/opencomputer/cron/scheduler.py:182-201` (`_build_agent_loop`)
- Test: `OpenComputer/tests/test_cron_enabled_toolsets_runtime.py`

- [ ] **Step 1: Write the failing test**

Create `OpenComputer/tests/test_cron_enabled_toolsets_runtime.py`:

```python
"""enabled_toolsets must propagate from job dict to AgentLoop.allowed_tools."""
from __future__ import annotations

import pytest

from opencomputer.cron.scheduler import _build_agent_loop


@pytest.mark.asyncio
async def test_enabled_toolsets_propagates_to_loop():
    job = {"id": "j1", "name": "n", "enabled_toolsets": ["Read", "Grep"]}
    loop = await _build_agent_loop(job)
    assert loop.allowed_tools is not None
    assert set(loop.allowed_tools) == {"Read", "Grep"}


@pytest.mark.asyncio
async def test_no_toolsets_means_no_allowlist():
    job = {"id": "j1", "name": "n", "enabled_toolsets": None}
    loop = await _build_agent_loop(job)
    # Default behavior — no allowlist set ⇒ inherits parent's full registry.
    assert loop.allowed_tools is None or loop.allowed_tools == frozenset()


@pytest.mark.asyncio
async def test_empty_toolsets_list_means_no_tools():
    job = {"id": "j1", "name": "n", "enabled_toolsets": []}
    loop = await _build_agent_loop(job)
    assert loop.allowed_tools == frozenset()
```

- [ ] **Step 2: Run to verify failure**

```bash
cd OpenComputer && pytest tests/test_cron_enabled_toolsets_runtime.py -v
```

Expected: FAIL — current `_build_agent_loop` doesn't read `enabled_toolsets`.

- [ ] **Step 3: Update `_build_agent_loop` to apply toolsets**

In `OpenComputer/opencomputer/cron/scheduler.py`, replace `_build_agent_loop` with:

```python
async def _build_agent_loop(job: dict[str, Any]) -> Any:
    """Construct a fresh :class:`AgentLoop` configured for a cron run.

    Cron jobs run in their own session, in plan mode by default, with a
    capped iteration budget. The loop inherits the active provider plugin
    from config — there's no per-job provider override.

    Hermes parity: ``enabled_toolsets`` on the job dict becomes
    ``loop.allowed_tools``. ``None`` = inherit full tool set; ``[]`` =
    no tools (pure-reasoning cron); list of names = only those tools.
    """
    from opencomputer.agent.config_store import load_config
    from opencomputer.agent.loop import AgentLoop

    cfg = load_config()
    cfg = cfg.with_loop_overrides(max_iterations=min(cfg.loop.max_iterations, 30))

    loop = AgentLoop(config=cfg)

    # Hermes parity: enabled_toolsets actually applied at run time.
    toolsets = job.get("enabled_toolsets")
    if toolsets is not None:
        loop.allowed_tools = frozenset(toolsets)

    return loop
```

- [ ] **Step 4: Run tests**

```bash
cd OpenComputer && pytest tests/test_cron_enabled_toolsets_runtime.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cron/scheduler.py OpenComputer/tests/test_cron_enabled_toolsets_runtime.py
git commit -m "fix(cron): apply enabled_toolsets to AgentLoop at run time (silent-gap fix)"
```

---

## Task 3: Multi-skill jobs — `skills: list[str]` field

**Files:**
- Modify: `OpenComputer/opencomputer/cron/jobs.py:296-409` (create_job signature + job dict)
- Modify: `OpenComputer/opencomputer/cron/scheduler.py:234-252` (`_build_run_prompt`)
- Test: `OpenComputer/tests/test_cron_multi_skill.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_cron_multi_skill.py`:

```python
"""Hermes parity: multiple skills per cron job."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.cron.jobs import create_job


@pytest.fixture
def isolated_home(tmp_path):
    with patch("opencomputer.agent.config._home", return_value=tmp_path):
        yield tmp_path


def test_create_with_skills_list_persists(isolated_home):
    job = create_job(schedule="every 1h", skills=["blogwatcher", "maps"])
    assert job["skills"] == ["blogwatcher", "maps"]
    assert job["skill"] is None


def test_create_with_singular_skill_back_compat(isolated_home):
    job = create_job(schedule="every 1h", skill="blogwatcher")
    assert job["skill"] == "blogwatcher"
    assert job["skills"] is None


def test_create_with_both_prefers_skills_list(isolated_home):
    job = create_job(schedule="every 1h", skill="X", skills=["A", "B"])
    assert job["skills"] == ["A", "B"]


def test_build_run_prompt_multi_skill():
    from opencomputer.cron.scheduler import _build_run_prompt
    job = {"skills": ["blogwatcher", "maps"]}
    prompt = _build_run_prompt(job)
    assert "blogwatcher" in prompt
    assert "maps" in prompt
    assert "combine" in prompt.lower()


def test_build_run_prompt_single_skill_in_list():
    from opencomputer.cron.scheduler import _build_run_prompt
    job = {"skills": ["solo"]}
    prompt = _build_run_prompt(job)
    assert "solo" in prompt
    assert "combine" not in prompt.lower()


def test_build_run_prompt_singular_skill_back_compat():
    from opencomputer.cron.scheduler import _build_run_prompt
    job = {"skill": "legacy"}
    prompt = _build_run_prompt(job)
    assert "legacy" in prompt
```

- [ ] **Step 2: Run to verify failure**

```bash
cd OpenComputer && pytest tests/test_cron_multi_skill.py -v
```

Expected: FAIL — `create_job` doesn't accept `skills`.

- [ ] **Step 3: Add `skills` parameter to `create_job`**

In `OpenComputer/opencomputer/cron/jobs.py`, modify `create_job` signature and body. Find the parameter list (line 296-312) and add `skills` after `skill`. Update validation, label, and the job dict:

```python
def create_job(
    *,
    schedule: str,
    name: str | None = None,
    prompt: str | None = None,
    skill: str | None = None,
    skills: list[str] | None = None,
    repeat: int | None = None,
    notify: str | None = None,
    plan_mode: bool = True,
    enabled_toolsets: list[str] | None = None,
    context_from: list[str] | None = None,
    workdir: str | None = None,
    no_agent: bool = False,
    script: str | None = None,
    script_timeout_seconds: int | None = None,
    origin_platform: str | None = None,
    origin_chat_id: str | None = None,
    origin_thread_id: str | None = None,
) -> dict[str, Any]:
    """Create a new cron job. ... (existing docstring + Hermes-parity additions: skills list, origin_*)."""
    if no_agent:
        if not script:
            raise ValueError("create_job: --no-agent requires --script <name>")
        if prompt or skill or skills:
            raise ValueError("create_job: --no-agent is exclusive with --prompt/--skill/--skills")
    elif not prompt and not skill and not skills:
        raise ValueError("create_job requires either prompt= or skill= or skills=")
    if prompt:
        assert_cron_prompt_safe(prompt)

    parsed = parse_schedule(schedule)

    if repeat is not None and repeat <= 0:
        repeat = None
    if parsed["kind"] == "once" and repeat is None:
        repeat = 1

    # Hermes parity: skills list takes precedence over singular skill
    # for prompt-building. If both supplied, store both but skills wins.
    effective_skills = list(skills) if skills else None
    effective_skill = None if effective_skills else skill

    job_id = uuid.uuid4().hex[:12]
    now_iso = _now().isoformat()
    label_source = (
        prompt
        or (effective_skills[0] if effective_skills else None)
        or skill
        or (f"[script: {script}]" if script else None)
        or "cron job"
    )
    label = label_source[:50].strip()

    job = {
        "id": job_id,
        "name": name or label,
        "prompt": prompt,
        "skill": effective_skill,
        "skills": effective_skills,
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
        "context_from": list(context_from) if context_from else None,
        "workdir": workdir,
        "last_response": "",
        "no_agent": bool(no_agent),
        "script": script,
        "script_timeout_seconds": script_timeout_seconds,
        # Hermes parity (2026-05-08): origin context for notify="origin".
        "origin_platform": origin_platform,
        "origin_chat_id": origin_chat_id,
        "origin_thread_id": origin_thread_id,
    }

    with _jobs_lock:
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)

    return job
```

- [ ] **Step 4: Update `_build_run_prompt` for multi-skill**

In `OpenComputer/opencomputer/cron/scheduler.py`, replace the `_build_run_prompt` function (around line 234) with:

```python
def _build_run_prompt(job: dict[str, Any]) -> str:
    """Construct the user prompt the agent should answer for this run.

    Hermes parity: ``skills: list[str]`` takes precedence over singular
    ``skill``. Multi-skill jobs ask the agent to chain the skills and
    produce a combined report.
    """
    cron_hint = (
        "[SYSTEM: You are running as a scheduled cron job. "
        "DELIVERY: Your final response will be automatically delivered to the "
        "configured channel — do NOT call send_message yourself. "
        "SILENT: If there is genuinely nothing new to report, respond with "
        'exactly "[SILENT]" (nothing else) to suppress delivery.]\n\n'
    )
    upstream = _build_context_from_block(job)

    skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
    if skills:
        if len(skills) == 1:
            return f"{cron_hint}{upstream}Use the `{skills[0]}` skill and report your findings."
        bulleted = "\n".join(f"- `{s}`" for s in skills)
        return (
            f"{cron_hint}{upstream}Use these skills together and combine the "
            f"results into one report:\n{bulleted}"
        )

    return cron_hint + upstream + (job.get("prompt") or "")
```

- [ ] **Step 5: Run tests**

```bash
cd OpenComputer && pytest tests/test_cron_multi_skill.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/cron/jobs.py OpenComputer/opencomputer/cron/scheduler.py OpenComputer/tests/test_cron_multi_skill.py
git commit -m "feat(cron): multiple skills per job (Hermes parity)"
```

---

## Task 4: `oc cron edit` CLI subcommand

**Files:**
- Modify: `OpenComputer/opencomputer/cli_cron.py` (add edit subcommand + multi-skill option to create)
- Test: `OpenComputer/tests/test_cron_edit_cli.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_cron_edit_cli.py`:

```python
"""Hermes parity: oc cron edit — change schedule/prompt/skill on existing jobs."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli_cron import cron_app
from opencomputer.cron.jobs import create_job, get_job

runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path):
    with patch("opencomputer.agent.config._home", return_value=tmp_path):
        yield tmp_path


def test_edit_schedule(isolated_home):
    job = create_job(schedule="every 1h", skill="x")
    res = runner.invoke(cron_app, ["edit", job["id"], "--schedule", "every 4h"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert "every 240m" == updated["schedule"]["display"]


def test_edit_prompt(isolated_home):
    job = create_job(schedule="every 1h", skill="x")
    res = runner.invoke(cron_app, ["edit", job["id"], "--prompt", "do new thing"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert updated["prompt"] == "do new thing"


def test_edit_replace_skills(isolated_home):
    job = create_job(schedule="every 1h", skills=["a", "b"])
    res = runner.invoke(cron_app, ["edit", job["id"], "--skill", "c", "--skill", "d"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert updated["skills"] == ["c", "d"]


def test_edit_add_skill(isolated_home):
    job = create_job(schedule="every 1h", skills=["a"])
    res = runner.invoke(cron_app, ["edit", job["id"], "--add-skill", "b"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert updated["skills"] == ["a", "b"]


def test_edit_remove_skill(isolated_home):
    job = create_job(schedule="every 1h", skills=["a", "b"])
    res = runner.invoke(cron_app, ["edit", job["id"], "--remove-skill", "a"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert updated["skills"] == ["b"]


def test_edit_clear_skills(isolated_home):
    job = create_job(schedule="every 1h", skills=["a", "b"])
    res = runner.invoke(cron_app, ["edit", job["id"], "--clear-skills"])
    assert res.exit_code == 0, res.output
    updated = get_job(job["id"])
    assert not updated.get("skills")


def test_edit_unknown_id(isolated_home):
    res = runner.invoke(cron_app, ["edit", "nonexistent", "--prompt", "x"])
    assert res.exit_code == 2


def test_create_with_multi_skill(isolated_home):
    res = runner.invoke(
        cron_app,
        ["create", "--schedule", "every 1h", "--skill", "a", "--skill", "b", "--name", "T"],
    )
    assert res.exit_code == 0, res.output
```

- [ ] **Step 2: Run failing test**

```bash
cd OpenComputer && pytest tests/test_cron_edit_cli.py -v
```

Expected: FAIL — no `edit` command, `--skill` only accepts one value.

- [ ] **Step 3: Update `cli_cron.py` — make --skill repeatable + add edit**

In `OpenComputer/opencomputer/cli_cron.py`:

a) Change the `cron_create` `skill` parameter type from `str | None` to `list[str] | None` and update the `create_job` call to pass it as `skills`:

```python
@cron_app.command("create")
def cron_create(
    schedule: Annotated[str, typer.Option("--schedule", "-s", help="Schedule expression.")],
    name: Annotated[str | None, typer.Option("--name", "-n", help="Friendly name.")] = None,
    skill: Annotated[list[str] | None, typer.Option("--skill", help="Skill to invoke. Repeat for multiple.")] = None,
    prompt: Annotated[str | None, typer.Option("--prompt", "-p", help="Free-text prompt.")] = None,
    repeat: Annotated[int | None, typer.Option("--repeat", help="Run N times.")] = None,
    notify: Annotated[str | None, typer.Option("--notify", help="Where to deliver.")] = None,
    auto: Annotated[bool, typer.Option("--auto", help="Disable plan_mode.")] = False,
    yolo: Annotated[bool, typer.Option("--yolo", help="[deprecated] alias for --auto.")] = False,
    no_agent: Annotated[bool, typer.Option("--no-agent", help="Run a script.")] = False,
    script: Annotated[str | None, typer.Option("--script", help="Script name.")] = None,
    script_timeout: Annotated[int | None, typer.Option("--script-timeout", help="Per-job override.")] = None,
) -> None:
    """Create a new scheduled job."""
    skills = list(skill) if skill else None
    if no_agent:
        if not script:
            typer.secho("Error: --no-agent requires --script <name>", fg="red", err=True)
            raise typer.Exit(2)
        if skills or prompt:
            typer.secho("Error: --no-agent is exclusive with --skill/--prompt", fg="red", err=True)
            raise typer.Exit(2)
    elif not skills and not prompt:
        typer.secho("Error: must supply --skill or --prompt (or --no-agent --script)", fg="red", err=True)
        raise typer.Exit(2)

    if yolo:
        from opencomputer.cli import _emit_yolo_deprecation
        _emit_yolo_deprecation()
        auto = True

    try:
        # Hermes parity: skills= takes precedence; pass list when present.
        if skills and len(skills) == 1:
            create_kwargs = {"skill": skills[0]}
        elif skills:
            create_kwargs = {"skills": skills}
        else:
            create_kwargs = {}

        job = create_job(
            schedule=schedule,
            name=name,
            prompt=prompt,
            repeat=repeat,
            notify=notify,
            plan_mode=not auto,
            no_agent=no_agent,
            script=script,
            script_timeout_seconds=script_timeout,
            **create_kwargs,
        )
    except CronThreatBlocked as exc:
        typer.secho(f"Blocked by threat scan: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc
    except ValueError as exc:
        typer.secho(f"Error: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc

    typer.secho(f"Created cron job {job['id']} '{job['name']}'", fg="green")
    typer.echo(f"  schedule:    {job['schedule_display']}")
    typer.echo(f"  next_run_at: {job.get('next_run_at') or 'n/a'}")
    typer.echo(f"  notify:      {job.get('notify') or 'local'}")
    typer.echo(f"  plan_mode:   {job.get('plan_mode')}")
    if job.get("skills"):
        typer.echo(f"  skills:      {job['skills']}")
    if job.get("no_agent"):
        typer.echo(f"  script:      {job.get('script')}")
        typer.echo("  no_agent:    True")
```

b) Add the `edit` subcommand at the bottom of the file (above `__all__`):

```python
@cron_app.command("edit")
def cron_edit(
    job_id: Annotated[str, typer.Argument(help="Job id.")],
    schedule: Annotated[str | None, typer.Option("--schedule", "-s", help="New schedule.")] = None,
    prompt: Annotated[str | None, typer.Option("--prompt", "-p", help="New prompt.")] = None,
    skill: Annotated[list[str] | None, typer.Option("--skill", help="REPLACE skills with these.")] = None,
    add_skill: Annotated[list[str] | None, typer.Option("--add-skill", help="Append a skill.")] = None,
    remove_skill: Annotated[list[str] | None, typer.Option("--remove-skill", help="Drop a skill.")] = None,
    clear_skills: Annotated[bool, typer.Option("--clear-skills", help="Remove all skills.")] = False,
    notify: Annotated[str | None, typer.Option("--notify", help="New delivery target.")] = None,
    workdir: Annotated[str | None, typer.Option("--workdir", help="New working directory (empty string clears).")] = None,
    repeat: Annotated[int | None, typer.Option("--repeat", help="New repeat count.")] = None,
) -> None:
    """Edit an existing cron job (Hermes parity)."""
    from opencomputer.cron.jobs import update_job
    job = get_job(job_id)
    if not job:
        typer.secho(f"job_id={job_id!r} not found", fg="red", err=True)
        raise typer.Exit(2)

    updates: dict[str, object] = {}
    if schedule is not None:
        updates["schedule"] = schedule  # update_job re-parses
    if prompt is not None:
        from opencomputer.cron.threats import assert_cron_prompt_safe
        assert_cron_prompt_safe(prompt)
        updates["prompt"] = prompt
    if notify is not None:
        updates["notify"] = notify or None
    if workdir is not None:
        updates["workdir"] = workdir or None
    if repeat is not None:
        rep = job.get("repeat") or {"times": None, "completed": 0}
        rep["times"] = repeat if repeat > 0 else None
        updates["repeat"] = rep

    # Skill mutation. Order: clear → set → add → remove.
    new_skills = list(job.get("skills") or ([job["skill"]] if job.get("skill") else []))
    skill_touched = False
    if clear_skills:
        new_skills = []
        skill_touched = True
    if skill:
        new_skills = list(skill)
        skill_touched = True
    if add_skill:
        for s in add_skill:
            if s not in new_skills:
                new_skills.append(s)
        skill_touched = True
    if remove_skill:
        new_skills = [s for s in new_skills if s not in set(remove_skill)]
        skill_touched = True
    if skill_touched:
        # Normalize: when the user explicitly mutates skills via edit, always
        # store as the plural list form (clearer, simpler, matches user intent).
        # Singular `skill` field is cleared so the back-compat shim doesn't
        # double-emit the first skill in the run prompt.
        if not new_skills:
            updates["skills"] = None
            updates["skill"] = None
        else:
            updates["skills"] = new_skills
            updates["skill"] = None

    if not updates:
        typer.secho("Nothing to update. Pass at least one --schedule/--prompt/--skill/etc.", fg="yellow", err=True)
        raise typer.Exit(0)

    try:
        updated = update_job(job_id, updates)
    except ValueError as exc:
        typer.secho(f"Error: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc

    if updated is None:
        typer.secho(f"job_id={job_id!r} not found (race?)", fg="red", err=True)
        raise typer.Exit(2)
    typer.secho(f"Updated cron job {updated['id']} '{updated['name']}'", fg="green")
    typer.echo(f"  schedule:    {updated['schedule_display']}")
    if updated.get("skills"):
        typer.echo(f"  skills:      {updated['skills']}")
    elif updated.get("skill"):
        typer.echo(f"  skill:       {updated['skill']}")
```

- [ ] **Step 4: Run tests**

```bash
cd OpenComputer && pytest tests/test_cron_edit_cli.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_cron.py OpenComputer/tests/test_cron_edit_cli.py
git commit -m "feat(cron): oc cron edit subcommand + repeatable --skill (Hermes parity)"
```

---

## Task 5: `/cron` slash bug fix + subcommand routing

**Files:**
- Modify: `OpenComputer/opencomputer/cli_ui/slash_handlers.py:912-931` (fix `_handle_cron_inline`)
- Test: `OpenComputer/tests/test_cron_slash_subcommands.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_cron_slash_subcommands.py`:

```python
"""Hermes parity: /cron list/add/pause/resume/run/remove."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencomputer.cli_ui.slash_handlers import _handle_cron_inline, SlashContext


@pytest.fixture
def ctx():
    return SlashContext(
        console=MagicMock(),
        session_id="s1",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=list,
    )


@pytest.fixture
def isolated_home(tmp_path):
    with patch("opencomputer.agent.config._home", return_value=tmp_path):
        yield tmp_path


def test_cron_no_args_lists_jobs(ctx, isolated_home):
    from opencomputer.cron.jobs import create_job
    create_job(schedule="every 1h", skill="x")
    res = _handle_cron_inline(ctx, [])
    assert res.handled
    # Empty case — printed something


def test_cron_list_subcommand(ctx, isolated_home):
    res = _handle_cron_inline(ctx, ["list"])
    assert res.handled


def test_cron_add_creates_job(ctx, isolated_home):
    from opencomputer.cron.jobs import list_jobs
    res = _handle_cron_inline(ctx, ["add", "every 1h", "Check status"])
    assert res.handled
    jobs = list_jobs()
    assert len(jobs) == 1
    assert "Check status" in jobs[0]["prompt"]


def test_cron_pause_resume(ctx, isolated_home):
    from opencomputer.cron.jobs import create_job, get_job
    job = create_job(schedule="every 1h", skill="x")
    _handle_cron_inline(ctx, ["pause", job["id"]])
    assert get_job(job["id"])["state"] == "paused"
    _handle_cron_inline(ctx, ["resume", job["id"]])
    assert get_job(job["id"])["state"] == "scheduled"


def test_cron_remove(ctx, isolated_home):
    from opencomputer.cron.jobs import create_job, get_job
    job = create_job(schedule="every 1h", skill="x")
    _handle_cron_inline(ctx, ["remove", job["id"]])
    assert get_job(job["id"]) is None


def test_cron_help_on_unknown(ctx, isolated_home):
    res = _handle_cron_inline(ctx, ["fakecmd"])
    assert res.handled
```

- [ ] **Step 2: Run failing test**

```bash
cd OpenComputer && pytest tests/test_cron_slash_subcommands.py -v
```

Expected: FAIL — current handler imports nonexistent `cron.store.CronStore`.

- [ ] **Step 3: Replace `_handle_cron_inline` with subcommand router**

In `OpenComputer/opencomputer/cli_ui/slash_handlers.py`, replace lines 912-931 with:

```python
def _handle_cron_inline(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/cron [list|add|pause|resume|run|remove] [args...]`` — Hermes parity.

    Bare ``/cron`` lists jobs (back-compat with the prior read-only handler).
    Subcommands wrap :mod:`opencomputer.cron.jobs` directly.
    """
    sub = args[0].lower() if args else "list"
    rest = args[1:]

    try:
        from opencomputer.cron.jobs import (
            create_job,
            get_job,
            list_jobs,
            pause_job,
            remove_job,
            resume_job,
            trigger_job,
        )
        from opencomputer.cron.threats import CronThreatBlocked
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]Cron unavailable: {e}[/yellow]")
        return SlashResult(handled=True)

    if sub == "list":
        jobs = list_jobs(include_disabled=("all" in rest or "-a" in rest))
        if not jobs:
            ctx.console.print("[dim]No cron jobs configured. Use `/cron add <schedule> <prompt>`.[/dim]")
            return SlashResult(handled=True)
        lines = [f"## Cron jobs ({len(jobs)})\n"]
        for j in jobs:
            target = j.get("skill") or (j.get("skills") and ",".join(j["skills"])) or (j.get("prompt") or "")[:40]
            lines.append(f"  {j['id'][:8]} {j['name'][:30]:<30} {j.get('schedule_display', ''):<18} {target}")
        ctx.console.print("\n".join(lines))
        return SlashResult(handled=True)

    if sub == "add":
        if len(rest) < 2:
            ctx.console.print('[yellow]Usage: /cron add "<schedule>" "<prompt>" [--skill name][/yellow]')
            return SlashResult(handled=True)
        # Simple parser: first arg = schedule, remainder = prompt (may include --skill flags).
        sched = rest[0]
        # Pull out --skill name pairs
        skills: list[str] = []
        prompt_parts: list[str] = []
        i = 1
        while i < len(rest):
            tok = rest[i]
            if tok == "--skill" and i + 1 < len(rest):
                skills.append(rest[i + 1])
                i += 2
            else:
                prompt_parts.append(tok)
                i += 1
        prompt_text = " ".join(prompt_parts).strip() or None
        try:
            kwargs: dict = {"schedule": sched, "prompt": prompt_text}
            if skills and len(skills) == 1:
                kwargs["skill"] = skills[0]
                kwargs["prompt"] = None  # skill takes precedence; don't double-charge
            elif skills:
                kwargs["skills"] = skills
                kwargs["prompt"] = None
            job = create_job(**kwargs)
        except CronThreatBlocked as e:
            ctx.console.print(f"[red]Blocked: {e}[/red]")
            return SlashResult(handled=True)
        except ValueError as e:
            ctx.console.print(f"[red]Error: {e}[/red]")
            return SlashResult(handled=True)
        ctx.console.print(f"[green]✓[/green] Created cron {job['id']} ({job['schedule_display']})")
        return SlashResult(handled=True)

    if sub in ("pause", "resume", "run", "remove"):
        if not rest:
            ctx.console.print(f"[yellow]Usage: /cron {sub} <job_id>[/yellow]")
            return SlashResult(handled=True)
        job_id = rest[0]
        actions = {"pause": pause_job, "resume": resume_job, "run": trigger_job, "remove": lambda i: remove_job(i)}
        result = actions[sub](job_id)
        if not result:
            ctx.console.print(f"[red]Cron job {job_id!r} not found[/red]")
            return SlashResult(handled=True)
        ctx.console.print(f"[green]✓[/green] /cron {sub} {job_id}")
        return SlashResult(handled=True)

    if sub in ("help", "?"):
        ctx.console.print(
            "## /cron commands\n"
            "  /cron list [all]                    — show jobs (default)\n"
            "  /cron add <schedule> <prompt>       — create with prompt\n"
            "  /cron add <schedule> --skill X      — create with skill\n"
            "  /cron pause|resume|run|remove <id>  — manage by id\n"
        )
        return SlashResult(handled=True)

    ctx.console.print(f"[yellow]Unknown /cron subcommand: {sub!r}. Try /cron help.[/yellow]")
    return SlashResult(handled=True)
```

- [ ] **Step 4: Run tests**

```bash
cd OpenComputer && pytest tests/test_cron_slash_subcommands.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/slash_handlers.py OpenComputer/tests/test_cron_slash_subcommands.py
git commit -m "fix(slash): /cron — fix CronStore bug + add subcommands (Hermes parity)"
```

---

## Task 6: `DELEGATE_BLOCKED_TOOLS` parity expansion

**Files:**
- Modify: `OpenComputer/opencomputer/tools/delegate.py:27-35`
- Test: `OpenComputer/tests/test_delegate_blocklist_extended.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_delegate_blocklist_extended.py`:

```python
"""Hermes spec: subagents must NEVER receive Memory, SendMessage, or ExecuteCode."""
from __future__ import annotations

from opencomputer.tools.delegate import DELEGATE_BLOCKED_TOOLS


def test_memory_blocked():
    assert "Memory" in DELEGATE_BLOCKED_TOOLS


def test_send_message_blocked():
    assert "SendMessage" in DELEGATE_BLOCKED_TOOLS


def test_execute_code_blocked():
    assert "ExecuteCode" in DELEGATE_BLOCKED_TOOLS


def test_existing_blocks_still_present():
    """Don't regress existing blocks while extending the set."""
    for name in ("delegate", "AskUserQuestion", "Clarify", "ExitPlanMode"):
        assert name in DELEGATE_BLOCKED_TOOLS
```

- [ ] **Step 2: Run failing test**

```bash
cd OpenComputer && pytest tests/test_delegate_blocklist_extended.py -v
```

Expected: 3 fail (the three new entries are absent).

- [ ] **Step 3: Extend the set**

In `OpenComputer/opencomputer/tools/delegate.py`, replace the `DELEGATE_BLOCKED_TOOLS` definition with:

```python
DELEGATE_BLOCKED_TOOLS: frozenset[str] = frozenset({
    "delegate",          # no recursive delegation (depth check is the second line of defense)
    "AskUserQuestion",   # subagent has no user
    "Clarify",           # subagent has no user
    "ExitPlanMode",      # subagent doesn't own plan mode
    # Hermes spec parity (2026-05-08): subagents must not write to shared
    # persistent memory, push messages cross-platform, or run arbitrary code.
    "Memory",            # no writes to shared persistent memory
    "SendMessage",       # no cross-platform side effects
    "ExecuteCode",       # no escape via arbitrary code execution
})
```

- [ ] **Step 4: Run tests**

```bash
cd OpenComputer && pytest tests/test_delegate_blocklist_extended.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/tools/delegate.py OpenComputer/tests/test_delegate_blocklist_extended.py
git commit -m "feat(delegate): block Memory/SendMessage/ExecuteCode in subagents (Hermes spec parity)"
```

---

## Task 7: `/agents` slash — read-only subagent tree

**Files:**
- Modify: `OpenComputer/opencomputer/cli_ui/slash_handlers.py` (add `_handle_agents_inline` + register)
- Test: `OpenComputer/tests/test_agents_slash.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_agents_slash.py`:

```python
"""Hermes parity: /agents — print live subagent tree."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.cli_ui.slash_handlers import _handle_agents_inline, SlashContext
from opencomputer.agent.subagent_registry import SubagentRecord


@pytest.fixture
def ctx():
    console = MagicMock()
    return SlashContext(
        console=console,
        session_id="s1",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=list,
    )


def test_agents_empty(ctx):
    with patch("opencomputer.agent.subagent_registry.SubagentRegistry.instance") as inst:
        registry = MagicMock()
        registry.list_records = MagicMock(return_value=[])
        inst.return_value = registry
        res = _handle_agents_inline(ctx, [])
    assert res.handled
    ctx.console.print.assert_called()


def test_agents_renders_running(ctx):
    rec = SubagentRecord(
        agent_id="a1",
        parent_id=None,
        goal="Research X",
        started_at=datetime.now(UTC),
        state="running",
    )
    with patch("opencomputer.agent.subagent_registry.SubagentRegistry.instance") as inst:
        registry = MagicMock()
        registry.list_records = MagicMock(return_value=[rec])
        inst.return_value = registry
        res = _handle_agents_inline(ctx, [])
    assert res.handled
    # Confirm something was printed
    assert ctx.console.print.called
```

- [ ] **Step 2: Run failing test**

```bash
cd OpenComputer && pytest tests/test_agents_slash.py -v
```

Expected: FAIL — `_handle_agents_inline` doesn't exist.

- [ ] **Step 3: Add the handler + register it**

In `OpenComputer/opencomputer/cli_ui/slash_handlers.py`, add this function near `_handle_cron_inline`:

```python
def _handle_agents_inline(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/agents`` — read-only tree of running + recently-finished subagents."""
    try:
        from opencomputer.agent.subagent_registry import SubagentRegistry
        records = SubagentRegistry.instance().list_records()
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]Subagent registry unavailable: {e}[/yellow]")
        return SlashResult(handled=True)

    if not records:
        ctx.console.print("[dim]No subagents running or recently finished.[/dim]")
        return SlashResult(handled=True)

    # Group by parent_id. None == top-level.
    by_parent: dict[str | None, list] = {}
    for r in records:
        by_parent.setdefault(r.parent_id, []).append(r)

    lines = [f"## Subagents ({len(records)})\n"]
    state_icon = {"running": "▶", "completed": "✓", "failed": "✗", "killed": "⊘"}

    def _emit(rec, depth: int) -> None:
        indent = "  " * depth
        icon = state_icon.get(rec.state, "?")
        elapsed = ""
        if rec.ended_at:
            elapsed = f" ({(rec.ended_at - rec.started_at).total_seconds():.1f}s)"
        elif rec.state == "running":
            from datetime import UTC, datetime
            elapsed = f" ({(datetime.now(UTC) - rec.started_at).total_seconds():.0f}s)"
        goal = (rec.goal or "")[:60]
        lines.append(f"{indent}{icon} {rec.agent_id[:8]} [{rec.state}]{elapsed}  {goal}")
        for child in by_parent.get(rec.agent_id, []):
            _emit(child, depth + 1)

    for top in by_parent.get(None, []):
        _emit(top, 0)
    ctx.console.print("\n".join(lines))
    return SlashResult(handled=True)
```

Register the handler. Find the dispatch table near line 1098 and add `"agents": _handle_agents_inline,`. Confirm by:

```bash
grep -n '"cron":' OpenComputer/opencomputer/cli_ui/slash_handlers.py
```

If `SubagentRegistry` lacks `list_records`, add it:

```bash
grep -n "def list_records\|def list\b" OpenComputer/opencomputer/agent/subagent_registry.py
```

If absent, add this method to `SubagentRegistry` in `OpenComputer/opencomputer/agent/subagent_registry.py`:

```python
def list_records(self) -> list[SubagentRecord]:
    """Return all subagent records (running + finished)."""
    with self._records_lock:
        return list(self._records.values())
```

- [ ] **Step 4: Run tests**

```bash
cd OpenComputer && pytest tests/test_agents_slash.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/slash_handlers.py OpenComputer/opencomputer/agent/subagent_registry.py OpenComputer/tests/test_agents_slash.py
git commit -m "feat(slash): /agents — read-only subagent tree (Hermes parity)"
```

---

## Task 8: `notify="origin"` — capture + deliver to origin chat

**Files:**
- Modify: `OpenComputer/opencomputer/tools/cron_tool.py` — capture origin in CronTool.execute
- Test: `OpenComputer/tests/test_cron_origin_delivery.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_cron_origin_delivery.py`:

```python
"""Hermes parity: notify='origin' delivers back to the chat where the job was created."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.cron.jobs import create_job
from opencomputer.cron.scheduler import _deliver
from opencomputer.gateway.session_context import set_session_vars, clear_session_vars
from opencomputer.tools.cron_tool import CronTool
from plugin_sdk.core import ToolCall


@pytest.fixture
def isolated_home(tmp_path):
    with patch("opencomputer.agent.config._home", return_value=tmp_path):
        yield tmp_path


@pytest.mark.asyncio
async def test_crontool_captures_origin_from_session_context(isolated_home):
    set_session_vars(platform="telegram", chat_id="-100123", thread_id="17585", user_id="u")
    try:
        tool = CronTool()
        call = ToolCall(
            id="t1",
            name="cron",
            arguments={
                "action": "create",
                "schedule": "every 1h",
                "skill": "x",
                "notify": "origin",
            },
        )
        result = await tool.execute(call)
        assert not result.is_error, result.content
        # Re-read the job from disk and confirm origin fields populated.
        from opencomputer.cron.jobs import list_jobs
        jobs = list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["origin_platform"] == "telegram"
        assert jobs[0]["origin_chat_id"] == "-100123"
        assert jobs[0]["origin_thread_id"] == "17585"
        assert jobs[0]["notify"] == "origin"
    finally:
        clear_session_vars()


@pytest.mark.asyncio
async def test_deliver_origin_uses_captured_context():
    job = {
        "id": "j1",
        "name": "n",
        "notify": "origin",
        "origin_platform": "slack",
        "origin_chat_id": "#engineering",
    }
    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock(return_value=None)
    with patch("opencomputer.plugins.registry.PluginRegistry.instance") as inst:
        registry = MagicMock()
        registry.get_channel_adapter = MagicMock(return_value=fake_adapter)
        inst.return_value = registry
        err = await _deliver(job, "hi")
    assert err is None
    fake_adapter.send.assert_awaited_once_with("#engineering", "hi")


@pytest.mark.asyncio
async def test_deliver_origin_missing_falls_through_to_local():
    job = {"id": "j1", "name": "n", "notify": "origin"}
    err = await _deliver(job, "hi")
    assert err is None  # silent fall-through
```

- [ ] **Step 2: Run failing test**

```bash
cd OpenComputer && pytest tests/test_cron_origin_delivery.py -v
```

Expected: First test fails (CronTool doesn't capture origin); third test fails (_deliver doesn't handle "origin" yet — wait, Task 1 already added it; verify).

- [ ] **Step 3: Update CronTool to capture origin from session_context**

In `OpenComputer/opencomputer/tools/cron_tool.py`, modify the create branch in `_dispatch` to capture origin from session_context:

```python
        if action == "create":
            schedule = _require(args, "schedule")
            skill = (args.get("skill") or "").strip() or None
            skills_arg = args.get("skills")
            skills = (
                [s for s in skills_arg if isinstance(s, str) and s.strip()]
                if isinstance(skills_arg, list)
                else None
            ) or None
            prompt = (args.get("prompt") or "").strip() or None
            if not skill and not skills and not prompt:
                raise ValueError("create requires either 'skill', 'skills', or 'prompt'")

            # Hermes parity: capture origin from session_context so notify="origin"
            # can route back here. Only meaningful in chat-spawned crons.
            from opencomputer.gateway.session_context import get_session_env
            origin_platform = get_session_env("OPENCOMPUTER_SESSION_PLATFORM", "") or None
            origin_chat_id = get_session_env("OPENCOMPUTER_SESSION_CHAT_ID", "") or None
            origin_thread_id = get_session_env("OPENCOMPUTER_SESSION_THREAD_ID", "") or None

            create_kwargs: dict[str, object] = {}
            if skills:
                create_kwargs["skills"] = skills
            elif skill:
                create_kwargs["skill"] = skill

            job = _create_job(
                schedule=schedule,
                name=args.get("name"),
                prompt=prompt,
                repeat=args.get("repeat"),
                notify=(args.get("notify") or None),
                plan_mode=bool(args.get("plan_mode", True)),
                origin_platform=origin_platform,
                origin_chat_id=origin_chat_id,
                origin_thread_id=origin_thread_id,
                **create_kwargs,
            )
            return {"action": "create", "job": _summarize(job)}
```

Also update the schema to accept the `skills` array. Find the `properties` block and add:

```python
                    "skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Hermes parity: invoke multiple skills together. Mutually exclusive with `skill`.",
                    },
```

- [ ] **Step 4: Run tests**

```bash
cd OpenComputer && pytest tests/test_cron_origin_delivery.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/tools/cron_tool.py OpenComputer/tests/test_cron_origin_delivery.py
git commit -m "feat(cron): notify='origin' — capture chat context, deliver back (Hermes parity)"
```

---

## Task 9: Cross-cutting verification + final integration test

**Files:**
- Run full suite + ruff
- Test: existing `tests/test_cron_*` files

- [ ] **Step 1: Run all cron + delegation tests**

```bash
cd OpenComputer && pytest tests/test_cron_ tests/test_delegate_ tests/test_agents_slash.py -v
```

Expected: all pass. Existing tests must still pass with the new fields/back-compat shim.

- [ ] **Step 2: Run full suite**

```bash
cd OpenComputer && pytest tests/ -q -x
```

Expected: All pass. Investigate any unrelated breakage before proceeding.

- [ ] **Step 3: Run ruff**

```bash
cd OpenComputer && ruff check opencomputer/ plugin_sdk/ tests/
```

Expected: No new violations.

- [ ] **Step 4: Smoke-test the slash + CLI changes manually**

```bash
# (Optional) confirm the CLI surface
cd OpenComputer && python -c "
from opencomputer.cli_cron import cron_app
from typer.testing import CliRunner
print(CliRunner().invoke(cron_app, ['edit', '--help']).output)
"
```

Expected: Help text shows --schedule/--prompt/--skill/--add-skill/--remove-skill/--clear-skills/--notify/--workdir/--repeat options.

- [ ] **Step 5: Final commit + push**

```bash
git status
git log --oneline -10
git push
```

---

## Self-Review Checklist

After completing all tasks, confirm:

- [ ] Spec coverage: each gap (#1-#8) has a task. ✓
- [ ] No placeholders or TBDs.
- [ ] Type names consistent: `Memory`, `SendMessage`, `ExecuteCode` (PascalCase tool names matching OC's actual registrations).
- [ ] `enabled_toolsets` tested at run time, not just at storage.
- [ ] Multi-skill: both `skill` (back-compat) and `skills` (new) covered.
- [ ] `/cron` slash bug fix verified by removing the `CronStore` reference.
- [ ] `notify="origin"` falls through silently (logs only) when origin context absent.
- [ ] DELEGATE_BLOCKED_TOOLS preserves the existing 4 entries.

---

## Risk Log

- **Subagent kill semantics on /agents** — the tree shows status; killing is via `oc agents kill <id>` (existing). No regression risk.
- **`notify="origin"` behavior when CronTool is invoked from CLI (not chat)** — session_context returns empty strings; job persists with `origin_*=None`; `_deliver` falls through to local-save. Tested.
- **`cli_cron edit` `update_job` semantics** — the existing helper already handles schedule re-parsing. The skill mutation logic happens before passing to update_job, so no changes needed there.
