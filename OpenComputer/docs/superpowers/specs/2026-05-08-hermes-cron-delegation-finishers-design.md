# Hermes Cron + Delegation — Long-Tail Finishers (Design)

**Date:** 2026-05-08
**Status:** approved (auto mode), self-audited
**Source spec:** `/Users/saksham/Downloads/files (1)/hermes-cron-delegation-v2.md`

---

## 1. Goal

Close the remaining honest gaps between the Hermes Cron & Delegation reference spec
and the current OpenComputer implementation. PR #494 already shipped major parity
(no_agent / script-only, parallel batch, multi-profile API). This work picks up the
long tail: silent gaps, hardcoded mappings, missing CLI verbs, and a broken slash.

## 2. Scope (8 items)

| # | Gap | Outcome |
|---|---|---|
| 1 | `_deliver` hardcodes `{telegram, discord}` | Generalize to all 18 channels via Platform enum + registry |
| 2 | `enabled_toolsets` stored, never applied at run time | Thread through to AgentLoop.allowed_tools |
| 3 | `skill: str` only — spec uses `skills: list[str]` | Add `skills` list, keep singular as back-compat shim |
| 4 | `oc cron edit` CLI subcommand missing | Add edit subcommand wrapping update_job |
| 5 | `/cron` slash broken (refs nonexistent CronStore) + missing subcommands | Fix bug; add list/add/pause/resume/run/remove |
| 6 | `DELEGATE_BLOCKED_TOOLS` lacks Memory/SendMessage/ExecuteCode | Add to spec parity |
| 7 | `/agents` slash overlay deferred | Read-only live tree from SubagentRegistry |
| 8 | `notify="origin"` (back-to-origin chat) | Persist origin from session_context at create time |

## 3. Non-Goals (explicit YAGNI)

- Per-platform env-var fallbacks (`OPENCOMPUTER_CRON_SLACK_TARGET`). Use `<platform>:<chat_id>` form.
- `/cron edit` slash subcommand. CLI is enough.
- Live-refresh `/agents` tree. Print-on-demand only.
- Dashboard route changes — multi-skill round-trips through generic update endpoints.

## 4. Architecture

**No new modules.** Surgical edits to:

- `opencomputer/cron/jobs.py` — add `skills`, `origin_platform`, `origin_chat_id`, `origin_thread_id` fields; accept skills list in `create_job`; add `edit_job` helper for skill mutation.
- `opencomputer/cron/scheduler.py` — generalize `_deliver` + `_resolve_chat_id` for full Platform enum; thread `enabled_toolsets` into AgentLoop; honor `notify="origin"`.
- `opencomputer/cli_cron.py` — accept repeated `--skill`; add `edit` subcommand.
- `opencomputer/tools/cron_tool.py` — schema accepts skills array + origin capture from session_context.
- `opencomputer/tools/delegate.py` — extend `DELEGATE_BLOCKED_TOOLS`.
- `opencomputer/cli_ui/slash_handlers.py` — fix /cron bug; add subcommand routing; add /agents read-only tree.

## 5. Data shape changes

Job dict gains four optional fields (all `None`-defaulted, back-compat):

```python
"skills": list[str] | None,         # spec parity — multiple skills per job
"origin_platform": str | None,      # captured from session_context at create time
"origin_chat_id": str | None,
"origin_thread_id": str | None,
```

Singular `skill` retained — `_build_run_prompt` reads either, with `skills` taking precedence.

## 6. Behavior — generalized delivery

```python
target = (job.get("notify") or "").strip().lower()

# "origin" — use captured origin context
if target == "origin":
    plat, chat = job.get("origin_platform"), job.get("origin_chat_id")
    if not plat or not chat:
        return None  # silent fall-through to local-save only
    target = f"{plat}:{chat}"

# Generic platform lookup
parts = target.split(":", 1)
plat_str = parts[0]
try:
    platform = Platform(plat_str)
except ValueError:
    return f"unknown notify target {target!r}"

adapter = registry.get_channel_adapter(platform)
if adapter is None:
    return f"channel plugin {plat_str!r} not enabled in this profile"

chat_id = parts[1] if len(parts) > 1 else _resolve_default_chat_id(plat_str)
if not chat_id:
    return f"no chat_id for {target!r}; use {plat_str}:<chat_id>"
await adapter.send(chat_id, content)
```

## 7. Behavior — enabled_toolsets at run time

```python
# inside _build_agent_loop or _run_one_job
loop = AgentLoop(config=cfg)
toolsets = job.get("enabled_toolsets")
if toolsets:
    loop.allowed_tools = frozenset(toolsets)
```

Mirrors the existing pattern in DelegateTool.execute().

## 8. Behavior — multi-skill prompt

```python
# _build_run_prompt
skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
if skills:
    if len(skills) == 1:
        return f"{cron_hint}{upstream}Use the `{skills[0]}` skill and report your findings."
    bulleted = "\n".join(f"- `{s}`" for s in skills)
    return f"{cron_hint}{upstream}Use these skills together and combine into one report:\n{bulleted}"
return cron_hint + upstream + (job.get("prompt") or "")
```

## 9. Failure modes (audit map)

- Multi-skill, one missing → agent will note the gap; soft-fail.
- Generalized delivery, unknown platform → return error string (logged, doesn't raise).
- `enabled_toolsets` with invalid name → AgentLoop allowlist filter ignores unknowns.
- `/cron` slash bad args → print usage; SlashResult(handled=True).
- Origin mode, no captured context → silent fall-through to local-save.

## 10. Tests (new)

- `tests/test_cron_delivery_generic.py` — Platform enum lookup, error paths.
- `tests/test_cron_enabled_toolsets_runtime.py` — toolset propagation to AgentLoop.
- `tests/test_cron_multi_skill.py` — `skills` list creation + prompt building.
- `tests/test_cron_edit_cli.py` — edit subcommand including --add-skill / --remove-skill / --clear-skills.
- `tests/test_cron_slash_subcommands.py` — slash routing + back-compat empty-args.
- `tests/test_delegate_blocklist_extended.py` — Memory/SendMessage/ExecuteCode rejected.
- `tests/test_cron_origin_delivery.py` — origin capture + delivery path.
- `tests/test_agents_slash.py` — /agents tree rendering with mock registry.
