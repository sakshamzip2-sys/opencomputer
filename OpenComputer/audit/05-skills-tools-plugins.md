# Open Computer — Skills, Tools, & Plugins Audit

**Date:** 2026-04-28.
**Mode:** Read-only.
**Codebase:** `/Users/saksham/Vscode/claude/OpenComputer/`.
**Scope:** Skills system (location, format, discovery, loader, creation, versioning), tool registry + protocol + sandbox, plugin discovery + types + installed inventory, and concrete placement recommendations for the four planned components — (a) affect classifier, (b) static structural graph, (c) per-user personal graph, (d) inspect/reset commands.

The user's framing is correct: this codebase synthesizes Hermes (autonomous skill creation, three-pillar memory) with OpenClaw (manifest-first plugin SDK boundary). The synthesis means there are *three* extension surfaces — skills, tools, and plugins — each with different costs and capabilities. The placement decision in the final section reasons from observed patterns in this codebase, not from how Hermes does it generically.

---

## Skills system

### Where skills live

Two locations — bundled-immutable and runtime-mutable:

| Location | Purpose | Count |
|----------|---------|-------|
| `/opencomputer/skills/<slug>/SKILL.md` | Bundled skills shipped with the package; immutable on update | **55 directories** confirmed via `find opencomputer/skills -name SKILL.md \| wc -l` |
| `~/.opencomputer/<profile>/skills/<slug>/SKILL.md` | User-promoted skills active for this profile | Variable — populated by promotion or hand-creation |
| `~/.opencomputer/<profile>/evolution/quarantine/<slug>/SKILL.md` | Auto-evolution proposals awaiting review | Variable |
| `~/.opencomputer/<profile>/evolution/approved/<slug>/SKILL.md` | Stage between quarantine and active skills | Variable |
| `~/.opencomputer/<profile>/evolution/archive/<slug>/SKILL.md` | Discarded drafts (audit trail; never re-proposed) | Variable |

When `MemoryManager.list_skills()` runs, it merges the bundled set with the per-profile active set; **user skills shadow bundled skills** if names collide.

### SKILL.md format

YAML frontmatter delimited by `---` lines, followed by free-form Markdown body. **No Python code embedded** — skills are pure text instruction; any code is invoked via the agent's normal tools (`Bash`, `PythonExec`, `Edit`, etc.).

Required frontmatter:
- `name` — human-readable string.
- `description` — string, one-line summary used in prompt injection AND CLI listings.

Optional:
- `version` — semver string (default `"0.1.0"` if omitted; only ~10 of 55 bundled skills declare one).

Verbatim example — `opencomputer/skills/brainstorming/SKILL.md:1-4`:

```yaml
---
name: brainstorming
description: "You MUST use this before any creative work - creating features, building components, adding functionality, or modifying behavior. Explores user intent, requirements and design before implementation."
---
```

A second example — `opencomputer/skills/docker-workflow/SKILL.md:1-3`:

```yaml
---
name: docker-workflow
description: Use when writing Dockerfiles, docker-compose, debugging container builds, image size reduction, or layer caching
---
```

A third — `opencomputer/skills/gan-planner/SKILL.md:1-4`:

```yaml
---
name: gan-planner
description: GAN Harness — Planner role. Expands a one-line prompt into a full product specification with features, sprints, evaluation criteria, and design direction. Use to seed the Generator/Evaluator loop.
---
```

The body is conventional but unstructured — most skills follow a recipe pattern (numbered steps, checklists, anti-patterns) but this is a community norm, not a schema requirement.

### Discovery mechanism

Loader: `/opencomputer/agent/memory.py:list_skills()` (lines 457–502).

Workflow:
1. Iterate both `self.skills_path` (per-profile) and `self.bundled_skills_paths` (`opencomputer/skills/`).
2. For each skill directory, call `frontmatter.load(skill_dir/"SKILL.md")` (line 480).
3. Construct `SkillMeta(id, name, description, path, version)` from frontmatter (lines 493–497).
4. Optionally load sibling `references/` and `examples/` directories (Phase III.4 feature).
5. Return a flat list of `SkillMeta`. No registry, no caching beyond the current call; no activation gate per skill.

`SkillMeta` dataclass: `/opencomputer/agent/memory.py:84-99`.

### Loader behaviour — what gets injected into the prompt

This is the critical mechanic and the answer to *"do skills run code, or are they prompt text"*: **skills are prompt text only, with metadata injection at session-prompt build time and on-demand body loading via a tool**.

1. **Frozen base prompt** — the Jinja2 template `opencomputer/agent/prompts/base.j2:190-207` iterates the `skills` list and renders bullets:
   ```jinja
   {% if skills -%}
   # Skills available

   OpenComputer ships skills as miniature playbooks: each is a `SKILL.md` describing a specific task pattern, sometimes with helper code. When a skill matches the user's request, follow its workflow rather than improvising.

   {% for skill in skills -%}
   - **{{ skill.name }}** — {{ skill.description }}
   {% endfor %}
   ```
   Only `name` + `description` go into the prompt — the full body does NOT. This is deliberate: with 55 bundled skills, in-prompt body injection would blow the context window.

2. **On-demand body loading** — `MemoryManager.load_skill_body()` at `/opencomputer/agent/memory.py:504-510` is invoked when the agent calls the `Skill` tool (`/opencomputer/tools/skill_tool.py`). The tool returns the full body for the skill the agent named. The agent then follows the steps in-line.

3. **No code execution from SKILL.md.** When a skill body contains `Run: pytest tests/` or similar, the agent calls the `Bash` tool to execute. Skills do not have an executable `entry` field, no `run()` function, no Python sidecar. They are pure prose.

This is why packaging an *affect classifier* as a skill would be wrong — there's no code-execution surface. Skills are recipe books for the agent's existing tools, not new behaviour.

### Distinctive bundled skills (representative cross-section of the 55)

| Skill | Domain |
|-------|--------|
| `brainstorming` | Process — gates creative work on intent/spec exploration |
| `tdd-workflow` | Process — test-first methodology |
| `verification-before-completion` | Process — gates "done" claims on tests/lint/type-check |
| `continuous-learning` | Process — auto-extract reusable patterns (Stop hook) |
| `docker-workflow` | Domain — Dockerfile + multi-stage + caching |
| `gan-planner` / `gan-evaluator` / `gan-generator` | Multi-agent harness — three-role GAN loop |
| `opencomputer-skill-authoring` | Meta — how to write SKILL.md |
| `opencomputer-plugin-structure` | Meta — how to scaffold a plugin |
| `stock-market-analysis` | User-domain — Saksham's stock workflow |

The mix is deliberate: process skills sit alongside domain skills and meta-skills. All ride the same loader.

---

## Skill creation

Three paths exist for creating skills, in increasing order of automation:

### 1. User direct edit (the simplest path)

The user creates `~/.opencomputer/<profile>/skills/<my-slug>/SKILL.md` by hand or through `$EDITOR`. No CLI command exposes "create-skill" — the filesystem is the authoritative interface. The skill is picked up by `list_skills()` on the next session start.

There IS a related CLI: `oc skill scan <path>` (`/opencomputer/cli_skills.py:64`) which runs the Skills Guard scanner on a candidate before the user copies it into the active skills dir.

### 2. Agent-deliberate via `SkillManageTool`

File: `/opencomputer/tools/skill_manage.py:137-353`.

Tool schema:

```text
Name: skill_manage
Actions: create | edit | patch | delete | view | list
Parameters per action:
  create  → action, skill_id, description, body, [version]
  edit    → action, skill_id, description, body, [version]
  patch   → action, skill_id, description, body
  delete  → action, skill_id
  view    → action, skill_id
  list    → action
```

The agent decides during a conversation that a recurring pattern deserves saving and calls `skill_manage(action="create", skill_id="my-pattern", description="...", body="...")`. Path validation (kebab-case `[a-z0-9]+(?:-[a-z0-9]+)*` at line 38) and a Skills Guard scan (`_scan_skill_content()` at lines 42-66, gated through `_guard_or_error()` at lines 69-101) run before the file is written. Output lands at `~/.opencomputer/<profile>/skills/<skill_id>/SKILL.md`.

The Skills Guard scan **does** run on agent-created skills. Dangerous patterns block the write (verdict "ask" — requires user confirmation per `policy.py`); caution patterns log + pass.

### 3. Auto-evolution (the daemon path)

The most complex path. Trigger: `SessionEndEvent` published from the agent loop's `finally` block.

Pipeline:
1. **Subscriber** (`/extensions/skill-evolution/subscriber.py:58-64`) listens on the ingestion bus.
2. **Heuristic filter**: `pattern_detector.is_candidate_session()` looks for repeated tool patterns and returns `SkillDraftProposal(pattern_key, pattern_summary, sample_arguments, count)` (`/opencomputer/evolution/pattern_detector.py:38-60`).
3. **LLM judge**: `judge_candidate_async()` evaluates whether the pattern is skill-worthy. Two-stage filter — heuristic first to keep the LLM call rare.
4. **Synthesizer**: `SkillSynthesizer` in `/opencomputer/evolution/synthesize.py` drafts a SKILL.md and writes to `<profile>/evolution/quarantine/<slug>/SKILL.md`.
5. **User review**: `oc evolution skills list` (lines 50-81 in `/opencomputer/evolution/cli.py`) shows quarantined drafts; `oc evolution skills promote <slug>` (lines 84-100) copies from quarantine → main skills dir. Optional `--force` overwrites.
6. **Discard**: rejected drafts can be moved to `archive/`; no automatic re-propose.

**Crucially: agent cannot self-promote.** The promotion CLI is not exposed as a tool to the agent. The user must run `oc evolution skills promote …` themselves. This is a deliberate guardrail.

### CLI surface for skills (consolidated)

| Command | File:line | Purpose |
|---------|-----------|---------|
| `oc skill scan <path>` | `/opencomputer/cli_skills.py:64-120` | Static-analyze a candidate SKILL.md for threat patterns |
| `oc evolution skills list` | `/opencomputer/evolution/cli.py:50-81` | List quarantined auto-proposed skills |
| `oc evolution skills promote <slug>` | `/opencomputer/evolution/cli.py:84-100` | Move quarantine → active skills dir |
| `oc evolution skills review <slug>` | `/opencomputer/evolution/cli.py` | Inspect a quarantined skill before promotion |
| `oc skill list` (agent-callable) | via `skill_manage(action="list")` | Agent enumerates installed skills |

There is no `/save-as-skill` slash command. There IS the underlying tool path for the agent to invoke deliberately.

### Versioning

Metadata-only. There are no `skill-v1/` / `skill-v2/` directories — overwriting a SKILL.md replaces it in place. The `version` field in frontmatter is informational. The `archive/` directory is a graveyard, not a version history. **No upgrade path** is enforced by the loader — when a skill body is improved, the old version is gone.

### Skills Guard — the safety surface

Module: `/opencomputer/skills_guard/`. Three components:
- `scanner.py` — pattern matching, invisible-unicode detection, structural checks, `Finding` and `ScanResult` dataclasses (lines 1-80).
- `threat_patterns.py` — ~120 regex patterns keyed by category.
- `policy.py` — trust-tier model with verdicts `safe`/`caution`/`dangerous` and per-tier action `allow`/`ask`/`block`.

Trust tiers:

| Tier | Source | Scan? | Block policy |
|------|--------|-------|--------------|
| `builtin` | Bundled in repo | Skipped | Never blocked |
| `trusted` | Known-good authors | Optional | Caution+ blocks contextually |
| `community` | Third-party imports | Required | Dangerous blocks; caution+ blocks |
| `agent-created` | Via `SkillManageTool` | Required | Dangerous → "ask"; caution → pass + log |

Scan triggers: every `SkillManageTool` write, every `oc skill scan` invocation, every promotion from quarantine. **Not** on bundled-skill load (trust-on-load) or already-promoted user-skill load (trust-after-promotion).

---

## Tools

### Tool registry mechanism

File: `/opencomputer/tools/registry.py` (101 lines). Singleton `registry` instance.

`ToolRegistry` data: `self._tools: dict[str, BaseTool]` — keyed by schema name. Name collisions raise `ValueError` (lines 24-31).

API surface:
- `registry.register(tool: BaseTool) -> None` — imperative registration.
- `@register_tool` (lines 94-97) — convenience decorator that returns the tool unchanged after registering it.
- `registry.schemas() -> list[ToolSchema]` (lines 39-40) — returns all schemas.
- `registry.dispatch(call: ToolCall, *, session_id=None, turn_index=None, demand_tracker=None) -> ToolResult` (lines 45-88) — async dispatch; never raises — all exceptions become error `ToolResult`s. Best-effort demand tracking on tool-not-found.

Registration sites are imperative (in `__init__.py` or in plugin `register()` functions), not decorator-based. The decorator exists but is little-used in current code.

### Tool count by category

47 concrete `BaseTool` subclasses across four locations:

| Category | Tools | Where |
|----------|-------|-------|
| File I/O | Read, Write, Glob, Grep | `/opencomputer/tools/` |
| Code editing | Edit, MultiEdit, NotebookEditTool, TodoWriteTool, RewindTool, RunTestsTool, CheckpointDiffTool, ExitPlanModeTool | `/extensions/coding-harness/tools/` |
| Execution | Bash, PythonExec, AppleScriptRun, StartProcessTool, CheckOutputTool, KillProcessTool | mixed |
| Memory & search | Recall, SessionSearchTool, MemoryTool, SkillTool, SkillManageTool | `/opencomputer/tools/` |
| Delegation & control | DelegateTool, CronTool, SpawnDetachedTaskTool | `/opencomputer/tools/` |
| Web & network | WebFetch, WebSearch | `/opencomputer/tools/` |
| Voice / media | VoiceTranscribe, VoiceSynthesize | `/opencomputer/tools/` (via voice-mode plugin) |
| Desktop introspection (Tier 1, macOS) | ListAppUsageTool, ReadClipboardOnceTool, ScreenshotTool, ExtractScreenTextTool, ListRecentFilesTool, PointAndClickTool | `/extensions/coding-harness/introspection/tools/` |
| Browser control | BrowserNavigateTool, BrowserClickTool, BrowserFillTool, BrowserSnapshotTool, BrowserScrapeTool | `/extensions/browser-control/` |
| Dev tools | GitDiffTool, BrowserTool (headless), FalTool | `/extensions/dev-tools/` |
| Interaction | AskUserQuestionTool, PushNotificationTool | `/opencomputer/tools/` |

`/opencomputer/tools/` itself contains 36 Python files; some are helpers, so the concrete-tool count there is roughly 24, with the remaining tools coming from bundled extensions.

### Tool-call protocol

Provider-native, with a canonical internal representation.

| Provider | Wire format |
|----------|------------|
| Anthropic | `tool_use` content blocks; request includes `tools: [{name, description, input_schema}]`; response includes `[{type: "tool_use", id, name, input}]`. Marshalling in `/extensions/anthropic-provider/provider.py:264-371`. |
| OpenAI | `tool_calls: [{id, type: "function", function: {name, arguments: "<json-string>"}}]`. Arguments arrive as JSON-encoded strings; provider plugin `json.loads()` them. |
| Bedrock | Same shape as Anthropic via Converse API. |

Canonical internal type — `plugin_sdk/core.py:62-68`:

```python
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
```

Every provider plugin normalises to this before it reaches the registry. **No custom XML format** — `grep -rn "<tool_call>" opencomputer/ extensions/` returns zero hits.

### Sandbox vs direct execution

| Tool category | Execution mode | Sandboxing |
|---------------|----------------|------------|
| Read, Write, Glob, Grep | In-process async | None — direct filesystem syscalls (gated by consent and PreToolUse hooks for file scope) |
| Edit, MultiEdit, NotebookEdit, TodoWrite | In-process sync | None — direct mutations |
| Bash | `asyncio.create_subprocess_shell()` | **Pattern detector only** — `/opencomputer/tools/bash_safety.py` `detect_destructive()` flags risky patterns; the destructive-pattern flag gates `parallel_safe` and feeds into PreToolUse hooks. **No subprocess isolation, no chroot, no seccomp**. |
| PythonExec | In-process | No execution sandbox; just code inspection |
| WebFetch / WebSearch | `httpx.AsyncClient` / async backends | URL safety: `/opencomputer/security/url_safety.py` blocks RFC1918 / metadata IPs / DNS-resolution failures, validates each redirect Location |
| Browser tools | Subprocess Playwright | **Yes** — isolated browser context per call (verify per Risk Register §06) |
| MCP tools | Subprocess (stdio) or HTTP | Subprocess isolation per server; OSV malware check before launch |
| Voice (Whisper / TTS) | Subprocess (ffmpeg, mlx-whisper, etc.) | Minimal — piped to subprocess |
| DelegateTool | In-process AgentLoop | Memory isolation via context clone; **no process boundary**, depth limit `MAX_DEPTH = 2` |

The code has the **shape** of sandboxing but most tools execute in-process with the agent's full filesystem and network access. The real safety surfaces are the consent gate (F1) and PreToolUse hooks (blocking).

### Tool gating order

Sequence applied to each tool call (`loop.py:1612-1750`):

1. **Cheap-route gate** — `/opencomputer/agent/cheap_route.py` decides if the FIRST iteration of a session can route to a cheaper model.
2. **Filtered schemas** — `loop.py:1473` `_filtered_schemas()` filters by `self.allowed_tools` BEFORE the tool array reaches the provider (subagent blast-radius constraint).
3. **F1 consent gate** — `loop.py:1634-1689` checks `tool.capability_claims` against the `consent_grants` table. Tier 0 silent-deny; Tier 1/2 prompt; Tier 3 silent-grant; Tier 4 implicit. Bypass via `OPENCOMPUTER_CONSENT_BYPASS=1`.
4. **PreToolUse hook (blocking)** — `loop.py:1691-1710` fires `HookEvent.PRE_TOOL_USE`. Plugins can return `HookDecision(decision="block")`.
5. **Dispatch** — `registry.dispatch(call, …)` at `loop.py:1730`.
6. **TRANSFORM_TOOL_RESULT hook (blocking)** — handlers can rewrite `result.content`.
7. **TRANSFORM_TERMINAL_OUTPUT hook (blocking)** — Bash streaming only.

Order is critical: consent fires BEFORE PreToolUse, so a plugin hook cannot bypass consent decisions.

---

## Plugins

### Discovery — two-phase manifest scan

Files: `/opencomputer/plugins/discovery.py` (514 lines) and `/opencomputer/plugins/loader.py` (1156 lines).

**Phase 1 — discovery (cheap)** at `discovery.py:200-297` `discover(search_paths, force_rescan=False)`:
- Scans three roots: `extensions/` (bundled), `~/.opencomputer/plugins/` (user), and `~/.opencomputer/<profile>/plugins/` (per-profile).
- Reads only `plugin.json` manifest — no Python imports, no side effects.
- Caches results for `_DISCOVERY_TTL_SEC = 1` second.
- Validates symlink escapes and permissions.
- Returns `PluginCandidate(manifest, root_dir, manifest_path, id_source)` list.

**Phase 2 — loading (lazy)** at `loader.py:803-954` `load_plugin(candidate, api, activation_source=None)`:
- Imports the entry module (`manifest["entry"] = "plugin"` → `plugin.py`) via `importlib.util.spec_from_file_location()` with a unique synthetic name to avoid `sys.modules` collisions (lines 862-873).
- Calls `register(api)` inside the module.
- Captures registrations via before/after `_snapshot_registrations()` diff.
- Enforces single-instance lock (`~/.opencomputer/.locks/<plugin-id>.lock`) if `single_instance: true`.
- Validates manifest's `tool_names` claims against actual registrations — drift logs WARNING but does not block.
- Validates provider config against `config_schema` if declared.
- Auto-installs MCP servers from `mcp_servers` preset slugs.

### Manifest schema

Validated via Pydantic in `/opencomputer/plugins/manifest_validator.py`. Required: `id`, `name`, `version`, `entry`, `kind`. Optional: `description`, `author`, `license`, `enabled_by_default`, `single_instance`, `profiles`, `tool_names`, `mcp_servers`, `model_support`, `legacy_plugin_ids`, `setup`, `schema_version`.

Verbatim example — `extensions/coding-harness/plugin.json`:

```json
{
  "id": "coding-harness",
  "name": "Coding Harness",
  "version": "0.4.0",
  "description": "Coding agent toolkit — Edit, MultiEdit, TodoWrite, background process tools, plan mode, and OI Bridge Tier 1 macOS introspection (window/clipboard/screen/files).",
  "author": "OpenComputer Contributors",
  "license": "MIT",
  "kind": "mixed",
  "entry": "plugin",
  "tool_names": [
    "Edit", "MultiEdit", "TodoWrite", "ExitPlanMode",
    "StartProcess", "CheckOutput", "KillProcess", "Rewind",
    "CheckpointDiff", "RunTests",
    "list_app_usage", "read_clipboard_once", "screenshot",
    "extract_screen_text", "list_recent_files"
  ]
}
```

### Plugin types (the `kind` field)

| `kind` | What it does | Example plugins |
|--------|--------------|-----------------|
| `provider` | LLM backend abstraction or "external service" provider; implements `BaseProvider` | anthropic-provider, openai-provider, aws-bedrock-provider, memory-honcho, weather-example |
| `channel` | Inbound/outbound message adapter implementing `BaseChannelAdapter` | telegram, discord, slack, matrix, signal, whatsapp, imessage, email, api-server, webhook, homeassistant, mattermost |
| `tool` | Pure tool registration | dev-tools, browser-control, browser-bridge |
| `skill` | Contributes SKILL.md files only; no runtime registrations | (none deployed yet — bundled skills live in `opencomputer/skills/` rather than as a plugin kind) |
| `mixed` | Multi-faceted: hooks + tools + slash commands + injection providers | coding-harness, voice-mode, skill-evolution, ambient-sensors |

### Plugin SDK API

`PluginAPI` registration methods (`loader.py:710-800`):

```python
api.register_tool(tool: BaseTool) -> None
api.register_provider(name: str, provider: BaseProvider | type) -> None
api.register_channel(name: str, adapter: BaseChannelAdapter) -> None
api.register_hook(spec: HookSpec) -> None
api.register_injection_provider(provider: DynamicInjectionProvider) -> None
api.register_memory_provider(provider: MemoryProvider) -> None
api.register_slash_command(cmd: SlashCommand) -> None
api.register_doctor_contribution(contribution: HealthContribution) -> None
```

Plus context properties:

```python
api.activation_source -> PluginActivationSource    # "user_enable" | "bundled" | ...
api.request_context -> RequestContext | None       # per-request scope inside gateway dispatch
api.session_db_path -> Path | None                 # profile-local SQLite session DB
```

Context manager `api.in_request(ctx: RequestContext)` enters per-request scope; plugins read `api.request_context` inside `dispatch()`.

### Currently installed plugins (24 in `extensions/`)

| id | kind | enabled_by_default | Status | Notes |
|----|------|---------------------|--------|-------|
| anthropic-provider | provider | **true** | Active | Default LLM provider |
| openai-provider | provider | false | Available | Activate via profile config |
| memory-honcho | provider | **true** | Active | Default memory provider (see 04-user-modeling.md) |
| aws-bedrock-provider | provider | false | Available | boto3 + Converse API |
| weather-example | provider | false | Available | Demo/reference |
| coding-harness | mixed | **true** | Active | Edit/MultiEdit/TodoWrite + macOS Tier 1 introspection |
| dev-tools | tool | **true** | Active | GitDiff, headless browser, fal.ai |
| telegram | channel | false | Available | Bot token required |
| discord | channel | false | Available | Bot token required |
| slack | channel | false | Available | Token required |
| matrix | channel | false | Available | Client-server API |
| signal | channel | false | Available | signal-cli REST API |
| whatsapp | channel | false | Available | Cloud API (Graph) |
| imessage | channel | false | Available | BlueBubbles bridge (macOS only) |
| email | channel | false | Available | IMAP/SMTP |
| api-server | channel | false | Available | REST API (POST /v1/chat) |
| webhook | channel | false | Available | Generic HTTP webhook receiver |
| homeassistant | channel | false | Available | HA REST API |
| mattermost | channel | false | Available | Self-hosted Slack alternative |
| browser-control | tool | false | Available | Playwright automation |
| browser-bridge | tool | false | Available | Browser-extension event receiver |
| voice-mode | mixed | false | Available | Push-to-talk loop |
| skill-evolution | mixed | false | Available | Auto-skill evolution daemon |
| ambient-sensors | mixed | false | Available | Foreground-app polling |

Plus `extensions/oi-capability/` — a vestigial husk (see Risk Register in 06-privacy.md §RR-12).

### Plugin vs. extension terminology

In this codebase the terms are **synonymous**. CLAUDE.md uses "extension" colloquially; the code calls everything a "plugin". The distinction is by *location* only: bundled plugins live under `extensions/`, user plugins under `~/.opencomputer/plugins/`.

---

## Recommended placement for affect components (a–d)

Reasoning from observed patterns, not from "how Hermes does it":

- **Skills are pure prose.** No code surface. Wrong fit for anything that needs to *run*.
- **Tools are registry entries with `BaseTool` schemas.** They run when the agent decides to call them. Wrong fit for anything that needs to run *every turn unconditionally*.
- **Plugins are `register(api)` functions that hook into the contract surfaces.** Right fit for behaviour that needs to be wired into the agent's loop without modifying core.
- **Core code lives in `opencomputer/*` and is part of the package itself.** Right fit for behaviour that everyone needs and shouldn't be optional.

### (a) Affect classifier that runs every turn

**Recommendation: Plugin (kind: `mixed`), registered as a `PRE_LLM_CALL` hook OR as a `MemoryProvider.prefetch()` injection. Or — match the existing vibe-classifier precedent and put it in core if it's deterministic and cheap.**

The codebase already has a working **vibe classifier** at `/opencomputer/agent/vibe_classifier.py` (regex-based, sub-millisecond, runs per turn on the companion-persona path at `loop.py:1218-1271`). That code is in core, not a plugin — it ships default-on for the companion persona. So you have two precedents to choose from:

| Precedent | Why follow it |
|-----------|---------------|
| Match the vibe classifier (core code in `/opencomputer/agent/`) | Default-on; available even without plugins; deterministic; cheap |
| Package as a plugin via `PRE_LLM_CALL` hook (fire-and-forget) | Optional; user can disable; testable in isolation; doesn't require core changes |

If the new affect classifier is **deterministic and cheap** (regex / heuristic), follow the vibe-classifier precedent and add a sibling module under `/opencomputer/agent/` that is invoked from `loop.py` near line 1218. Persist results to a new column on `sessions.db.sessions` (next to `vibe`) via a schema migration in `state.py`.

If the new affect classifier is **expensive (LLM call)** or experimental, package as a plugin. Register a `PRE_LLM_CALL` hook with priority 10 (early, but observation-only). Persist results to your plugin's own SQLite under `<profile>/<plugin_id>/`.

If you need the affect signal to **shape the system prompt for the next turn**, register additionally as a `DynamicInjectionProvider` whose `collect()` returns a `<user-affect>…</user-affect>` block.

**Concrete pattern (plugin)**:

```python
# extensions/affect-classifier/plugin.py
from plugin_sdk.hooks import HookEvent, HookSpec, HookContext, HookDecision
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext

async def affect_classify(ctx: HookContext) -> HookDecision | None:
    return None  # fire-and-forget; observation only

class AffectInjectionProvider(DynamicInjectionProvider):
    priority = 50

    @property
    def provider_id(self) -> str:
        return "affect:v1"

    async def collect(self, ctx: InjectionContext) -> str | None:
        affect = await get_recent_affect(ctx.session_id)
        return f"<user-affect>{affect}</user-affect>" if affect else None

def register(api):
    api.register_hook(HookSpec(
        event=HookEvent.PRE_LLM_CALL,
        handler=affect_classify,
        fire_and_forget=True,
        priority=10,
    ))
    api.register_injection_provider(AffectInjectionProvider())
```

Manifest `kind: "mixed"`. `enabled_by_default: false` initially.

### (b) Static structural graph (semantic neighbors, compositions)

**Recommendation: Core code, with read access exposed as a tool.**

Static graphs are infrastructure — they do not change per user, they do not change per turn, and they should be queryable from anywhere. Compare with how the codebase ships tool ordering (`/opencomputer/agent/tool_ordering.py`) or the cheap-route gate (`/opencomputer/agent/cheap_route.py`) — both core utilities running on every turn, deterministically, with no user-specific state.

The graph itself should live under `/opencomputer/<your-graph-name>/store.py`. Read access is exposed as a tool (`/opencomputer/tools/<your-graph-name>_query.py`). Optionally surface a CLI sub-app at `/opencomputer/cli_<your-graph-name>.py` for inspection.

If the graph is genuinely *static*, an even simpler pattern: a Python module with a frozen dict / `typing.Final` graph + a read function. See how the persona registry is structured at `/opencomputer/awareness/personas/registry.py:34-49`.

### (c) Per-user personal graph (transitions, frequencies)

**Recommendation: Plugin (kind: `provider` — extending the `MemoryProvider` contract).**

This is the classic dialectic-memory shape: per-user, per-profile, mutable across turns, contributes context pre-LLM, can be queried by the agent. The exact contract is already there: `plugin_sdk/memory.py:30-205`'s `MemoryProvider` ABC. The Honcho plugin is the working precedent.

But there's a constraint: **`MemoryProvider` is single-tenant** — only one is active at a time. Honcho currently occupies that slot by default. So you have two patterns:

| Pattern | Trade-off |
|---------|-----------|
| Replace Honcho — your plugin becomes the new default `provider: state-graph:v1` in `MemoryConfig` | Cleanest contract; conflicts with existing Honcho deployment |
| Stack on top — register as `DynamicInjectionProvider` for read-side context + subscribe to `MemoryWriteEvent` / `TurnEndEvent` on the ingestion bus for write-side mutations | Coexists with Honcho |

Either way it's a plugin under `extensions/<personal-graph-name>/`, kind `provider` (or `mixed`). Storage: `<profile>/<plugin_id>/graph.sqlite` — mirror the F4 layout (nodes + edges + FTS5 + decay weights). Use the bridge pattern from `/opencomputer/user_model/honcho_bridge.py:25-26, 58-70, 171` to mark provenance and prevent feedback cycles.

### (d) User-facing inspect/reset commands for the personal graph

**Recommendation: A CLI sub-app at `/opencomputer/cli_<your-graph-name>.py`, registered as a Typer subcommand on `oc`.**

This is exactly how every other inspectable subsystem is wired: `cli_memory.py` for MEMORY.md / USER.md / SOUL.md, `cli_user_model.py` for the F4 graph, `cli_consent.py` for the F1 consent layer, `cli_audit.py` for the audit log, `cli_cost.py` for cost-guard.

Implementation skeleton (mirroring `cli_user_model.py`):

```python
# opencomputer/cli_state_graph.py
import typer

app = typer.Typer(help="Inspect/reset the personal state graph")

@app.command("nodes")
def list_nodes(kind: str = "", limit: int = 20): ...

@app.command("edges")
def list_edges(kind: str = "", limit: int = 20): ...

@app.command("search")
def search(query: str): ...

@app.command("reset")
def reset(yes: bool = False):
    """Hard reset — wipe graph + create .bak."""

@app.command("decay")
def decay(apply: bool = False): ...

@app.command("export")
def export(output: typer.FileTextWrite): ...
```

Then wire it into `opencomputer/cli.py` next to the existing `app.add_typer(memory_app, name="memory")` calls.

### Summary table

| Component | Home | Why |
|-----------|------|-----|
| (a) Affect classifier | Core code if cheap and default-on (matches vibe-classifier precedent); plugin via `PRE_LLM_CALL` hook + `DynamicInjectionProvider` if optional/expensive | Already-shipping precedent in `/opencomputer/agent/vibe_classifier.py`; plugin pattern proven by `extensions/skill-evolution/` |
| (b) Static structural graph | Core code; module + tool wrapper | No per-user state; needs to be queryable from anywhere; matches `tool_ordering.py` / `cheap_route.py` precedent |
| (c) Per-user personal graph | Plugin (`provider` or `mixed` kind), implementing `MemoryProvider` or stacking via injection + bus | Per-user mutable state IS the `MemoryProvider` shape; Honcho is the working precedent |
| (d) Inspect/reset commands | CLI sub-app at `/opencomputer/cli_<name>.py`, wired into `cli.py` | Matches `cli_memory.py`, `cli_user_model.py`, `cli_consent.py`, `cli_audit.py` precedents |

A combined deployment looks like: one plugin (`extensions/personal-state-graph/`) implementing (c), one core module (`/opencomputer/state_graph_static/`) implementing (b), one core module (`/opencomputer/agent/affect_classifier.py`) implementing (a), and one CLI sub-app (`/opencomputer/cli_state_graph.py`) implementing (d). Total: 1 plugin + 3 core modules + 1 schema migration.

### What NOT to do

- **Do not package any of this as a SKILL.md.** Skills are prose — they cannot run code.
- **Do not put per-user state in core.** The codebase's principle is that profile-specific state lives under `<profile>/` and is owned by a plugin or CLI sub-app, not by core code.
- **Do not register two `MemoryProvider`s simultaneously.** The slot is single-tenant; the second `register_memory_provider()` call raises `ValueError`.
- **Do not assume `MemoryProvider.on_session_end()` is invoked.** It's declared in the SDK but currently unwired in the agent loop. Subscribe to `SessionEndEvent` on the ingestion bus for end-of-session cleanup.

---

## File references

### Skills system

- `/opencomputer/skills/` — 55 bundled skills.
- `/opencomputer/agent/memory.py:84-99, 457-502, 504-510`.
- `/opencomputer/agent/prompts/base.j2:190-207`.
- `/opencomputer/agent/prompt_builder.py:190-239`.

### Skill creation

- `/opencomputer/tools/skill_manage.py:38, 42-66, 69-101, 137-353`.
- `/opencomputer/cli_skills.py:55-120`.
- `/extensions/skill-evolution/subscriber.py:58-64`.
- `/opencomputer/evolution/{pattern_detector,synthesize,cli}.py`.
- `/opencomputer/skills_guard/{scanner,threat_patterns,policy}.py`.

### Tools

- `/opencomputer/tools/registry.py:24-31, 39-40, 45-88, 94-97`.
- `/opencomputer/tools/bash_safety.py`; `/opencomputer/security/url_safety.py`; `/opencomputer/security/osv_check.py`.
- `/opencomputer/agent/tool_result_storage.py`; `/opencomputer/agent/budget_config.py`.
- `/extensions/anthropic-provider/provider.py:264-371`.
- `plugin_sdk/core.py:62-68`.
- `/opencomputer/agent/loop.py:1473, 1612-1750, 1730`.

### Plugins

- `/opencomputer/plugins/discovery.py:200-297`.
- `/opencomputer/plugins/loader.py:710-800, 803-954, 862-873`.
- `/opencomputer/plugins/manifest_validator.py`.
- `/extensions/coding-harness/plugin.json`; `/extensions/*/plugin.json` (24 manifests).
- `/plugin_sdk/__init__.py`.

### Cross-cutting (precedents to follow)

- `/opencomputer/cli_memory.py`, `cli_user_model.py`, `cli_consent.py`, `cli_audit.py`, `cli_cost.py` — CLI sub-app precedents.
- `/opencomputer/agent/vibe_classifier.py` — classifier-in-core precedent for (a).
- `/opencomputer/awareness/personas/{classifier,registry}.py` — heuristic classifier + bundled-defaults registry.
- `/extensions/skill-evolution/` — full plugin precedent with hook + bus + injection.
- `/extensions/memory-honcho/` — full `MemoryProvider` plugin precedent.
