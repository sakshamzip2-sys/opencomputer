# OpenComputer — Best-of-Three Port Plan (the recipes)

**Date:** 2026-05-17
**Companion to:** `2026-05-17-best-of-three-audit.md` (the gap analysis).
**Scope:** Concrete, scoped recipes for the top-10 gaps. Each recipe = scope + file pointers + acceptance criteria + size. No code diffs; recipes are scoped so a follow-up PR is straightforward.

---

## Reading rules

- **Size**: S = ≤1 day, M = 1-3 days, L = 3-7 days. Effort assumes one engineer who knows the OC codebase.
- **Risk**: how easy is rollback if the recipe breaks? L means hard rollback, S means trivial.
- **Order matters**: recipes are independent unless explicitly marked DEPENDS ON.
- The 10 recipes are **ordered by leverage** (best first). Skip down if your sprint budget runs out.

---

## Recipe 1 — User markdown slash commands (`.opencomputer/commands/*.md` → `/foo`)

**Why first**: highest leverage per dev-day in the entire audit. Today writing a custom command means a `.py` file + `slash.py` edit + restart. Claude Code's killer feature: drop `~/.claude/commands/deploy.md` and get `/deploy`. Zero code, zero restart.

**Source pattern**: Claude Code per `code.claude.com/docs/en/plugins-reference`. Hermes also does this for skill commands (`agent/skill_commands.py`).

**Scope**:
1. New module `opencomputer/agent/markdown_commands.py` — scans 3 dirs at boot:
   - `~/.opencomputer/<profile>/commands/*.md` (per-profile user commands)
   - `~/.opencomputer/commands/*.md` (global user commands)
   - `<cwd>/.opencomputer/commands/*.md` (project commands, opt-in via env var)
2. Each `.md` file has optional YAML frontmatter (`description`, `args_hint`, `category`, `model_override`, `tools`); the body becomes the prompt template that gets injected as a user message.
3. Markdown commands appear in `/help`, in autocomplete, and resolve through the same `resolve_command()` path as Python commands.
4. Same Python `CommandDef` shape gets registered dynamically — they coexist with built-in commands.
5. Variable substitution: `{{args}}` in the body gets the user's `/foo blah blah` args.
6. Conflict policy: project > per-profile > global > built-in. Last-wins, but log a WARNING.

**Files touched**:
- `opencomputer/agent/markdown_commands.py` (new, ~150 LOC)
- `opencomputer/cli_ui/slash.py` (modified — `SLASH_REGISTRY` gets extended at boot with the markdown entries)
- 1 new test file

**Acceptance criteria**:
- `mkdir -p ~/.opencomputer/commands && echo "Summarize the conversation so far in 3 bullets." > ~/.opencomputer/commands/tldr.md`
- Running `oc chat` shows `/tldr` in autocomplete and `/help`
- Typing `/tldr` injects the markdown body as a user message and the agent responds
- Frontmatter `description: "..."` shows up in `/help`
- Variable substitution: `~/.opencomputer/commands/explain.md` with body `"Explain {{args}} like I'm a junior engineer."` — `/explain monads` injects the substituted text

**Size**: S (1 day). **Risk**: S (additive — no existing code path changes).

**Unblocks**: nothing depends on this. Ship standalone.

---

## Recipe 2 — Wire the 7 unregistered slash commands

**Why second**: cheapest win in the audit. The code already exists.

**Source pattern**: OC itself. `opencomputer/agent/slash_commands_impl/` has 40 `_cmd.py` files but `opencomputer/cli_ui/slash.py` only declares 38 `CommandDef` entries. Grep proof:

```
agents_cmd.py        → no CommandDef
background_cmd.py    → no CommandDef
btw_cmd.py           → no CommandDef
copy_cmd.py          → no CommandDef
history_cmd.py       → no CommandDef
rollback_cmd.py      → no CommandDef
restore_cmd.py       → no CommandDef
save_cmd.py          → no CommandDef
title_cmd.py         → no CommandDef
```

**Scope**: for each of the 7+ `_cmd.py` files that lack a `CommandDef` entry, add one to `SLASH_REGISTRY` in `opencomputer/cli_ui/slash.py`. Read each impl's docstring to confirm `name`, `description`, `category`, `args_hint`, `aliases`.

**Files touched**:
- `opencomputer/cli_ui/slash.py` (one block of additions, ~30 lines)

**Acceptance criteria**:
- `/copy` puts the last assistant response on the system clipboard
- `/background <prompt>` (alias `/btw`) spawns a delegate subagent, returns task id
- `/agents` (alias `/tasks`) lists active subagents with status
- `/rollback N` reverts the last N file edits
- `/save <name>` persists the current session under a name
- `/title <text>` sets the session title
- `/history` shows paged session history
- `oc chat → /help` shows all 7 new entries

**Size**: XS (2 hours). **Risk**: XS (each handler already exists; just wiring).

**Unblocks**: nothing.

---

## Recipe 3 — Wire `activation_planner.py` into the load pipeline

**Why third**: cuts cold-start time 5-10x for users with 86 extensions installed. The code already exists; just nothing calls it.

**Source pattern**: OpenClaw's manifest-first activation. OC has the planner at `opencomputer/plugins/activation_planner.py:47` (`plan_activations()`), wired into nothing. Grep across the tree:

```
$ grep -rn plan_activations opencomputer/
opencomputer/plugins/activation_planner.py:27:    "plan_activations",
opencomputer/plugins/activation_planner.py:47:def plan_activations(
```

Two hits, both self-references. The function returns the deterministic set of plugin IDs that should activate given current triggers (active providers / channels / requested tools / invoked commands / active model id) — but nothing builds the triggers or calls it.

**Scope**:
1. In `opencomputer/agent/loop.py` (or wherever `PluginRegistry` materializes), build an `ActivationTriggers` snapshot at session start from:
   - `runtime.provider` (active model provider)
   - `runtime.channel` (gateway adapter, if applicable)
   - `runtime.cli_command` (current top-level CLI command)
   - active model id from config
2. Call `plan_activations(candidates, triggers)` to get the narrowed plugin ID list.
3. Pass that list to `loader.load_plugin()` instead of "load everything."
4. Add an env-var escape hatch `OPENCOMPUTER_LOAD_ALL_PLUGINS=1` to force the old behavior for debugging.
5. Hook event: emit `PluginActivationPlanned(plugin_ids=[...], triggers={...})` for observability.

**Files touched**:
- `opencomputer/agent/loop.py` or `opencomputer/cli.py` (1 modification, ~20 LOC)
- `opencomputer/plugins/loader.py` (likely 1 small change to accept a narrow list)
- 1 new test asserting load count drops with narrower triggers

**Acceptance criteria**:
- With 86 extensions installed and a single-provider session (e.g., `--provider anthropic`), `oc chat` boot loads ≤10 plugins instead of 86
- Cold-start time drops measurably (target: <300ms vs current ~2s with all extensions enabled)
- `OPENCOMPUTER_LOAD_ALL_PLUGINS=1 oc chat` loads everything (escape hatch works)
- Test asserts `plan_activations()` returns the correct narrowed set for `triggers={provider="anthropic"}` against a fixture catalog

**Size**: S (1 day, mostly because the planner is already written). **Risk**: M (some plugin might rely on early-load side effects — needs careful first run).

**Unblocks**: nothing; pure performance.

---

## Recipe 4 — Color tokens registry + skin engine (3 skins)

**Why fourth**: enables every theme / UI customization plugin. Already detailed in `2026-05-17-deep-parity-and-visual-spec.md` — recapping here for completeness.

**Source pattern**: Hermes's `sources/hermes-agent/hermes_cli/skin_engine.py:1` (891 LOC, data-driven, 8 built-in skins).

**Scope**:
1. **Part A** (half day): `opencomputer/cli_ui/tokens.py` — `ColorTokens` dataclass with 29 token names (7 existing + 22 missing). Each token has an OC-pink default. Replace every literal hex in `cli_banner.py`, `cli_ui/*`, `agent/display.py` with `tokens.COLORS[<name>]`.
2. **Part B** (1.5 days): `opencomputer/cli_ui/skin_engine.py` — port Hermes's skin engine (~600 LOC after stripping prompt_toolkit cruft). Ship 3 built-in skins: `default` (today's OC pink, byte-identical), `mono` (grayscale), `daylight` (light terminal). `~/.opencomputer/<profile>/skins/*.yaml` for user skins.
3. **Part C** (half day): `opencomputer/cli_skin.py` — `oc skin list|set|preview` CLI.

**Files touched**:
- `opencomputer/cli_ui/tokens.py` (new)
- `opencomputer/cli_ui/skin_engine.py` (new, ~600 LOC)
- `opencomputer/cli_skin.py` (new, ~80 LOC)
- `opencomputer/cli_banner.py` (modified, removes literal hex)
- `opencomputer/agent/display.py` (modified, removes literal hex)

**Acceptance criteria**:
- `oc skin list` shows 3 skins
- `oc skin set mono` followed by `oc chat` renders splash in grayscale
- `oc skin set default` produces splash byte-identical to today (snapshot test asserts this — **critical pixel-per-pixel preservation**)
- User drops YAML in `~/.opencomputer/<profile>/skins/foo.yaml` → `oc skin set foo` picks it up
- `cli_banner.py` references zero literal hex codes (verified by grep)

**Size**: M (2-3 days). **Risk**: M (the byte-identical-default-skin assertion is the rigor).

**Unblocks**: Recipes 7 (KawaiiSpinner skin overrides) and any future visual plugin.

---

## Recipe 5 — Plugin marketplaces (plural, named)

**Why fifth**: ecosystem play. Single hardcoded catalog doesn't scale to a community.

**Source pattern**: Claude Code's `plugin marketplace add owner/repo` + `plugin install bar/sometool`. Each marketplace is a git repo with a `marketplace.json` listing the plugins it hosts. Trust = per-marketplace signing key.

**Scope**:
1. New: `opencomputer/plugins/marketplaces.py` — manages `~/.opencomputer/marketplaces.yaml` with named registry entries: `{name, url, trust_key, added_at}`.
2. Extend `opencomputer/plugins/remote_install.py` so `fetch_catalog()` walks all configured marketplaces, not just one.
3. CLI:
   - `oc plugin marketplace add <name> <url>` — clone the repo, fetch `marketplace.json`, store signing key
   - `oc plugin marketplace remove <name>`
   - `oc plugin marketplace list` — shows configured marketplaces with status
   - `oc plugin search <query>` — searches across all marketplaces
   - `oc plugin install <marketplace>/<plugin>` — explicit marketplace prefix
4. Trust policy: each marketplace's signing key is stored on `add`; `install` validates the plugin signature against THAT marketplace's key (not a global key). Multi-key trust.

**Files touched**:
- `opencomputer/plugins/marketplaces.py` (new, ~200 LOC)
- `opencomputer/plugins/remote_install.py` (modified)
- `opencomputer/cli_plugin.py` (modified — new subcommands)
- 2 new tests

**Acceptance criteria**:
- `oc plugin marketplace add foo https://github.com/foo/oc-marketplace`
- `oc plugin marketplace list` shows `foo` with trust key fingerprint
- `oc plugin search analyzer` returns matches across all marketplaces with their source labels
- `oc plugin install foo/heavy-analyzer` installs from the right marketplace
- Removing a marketplace doesn't break already-installed plugins from it

**Size**: M (3-4 days). **Risk**: M (catalog signing semantics need care).

**Unblocks**: future community plugin ecosystem.

---

## Recipe 6 — Hot-reload plugins

**Why sixth**: critical plugin-author DX. Today: edit `plugin.py`, restart `oc chat`, reload your session, hope nothing changed.

**Source pattern**: Hermes has `/reload-skills` and `/reload-mcp` but not `/reload-plugin <id>` for full plugins. Nobody really has this right; OC can be first.

**Scope**:
1. New slash command `/reload-plugin <id>` (and `/reload-plugin all`) in `opencomputer/agent/slash_commands_impl/plugin_reload_cmd.py` (already exists — extend it).
2. In `opencomputer/plugins/loader.py`, add `reload_plugin(plugin_id)`:
   - Find currently-loaded module via `_plugin_module_cache[plugin_id]`
   - Unregister its tools / hooks / channels from the relevant registries (this needs registries to track per-plugin contributions — likely a `_provenance` dict)
   - `importlib.reload(module)`
   - Clear the sibling-name `sys.modules` cache (already done at first load)
   - Call `register(api)` again
3. Tools registered by name must support re-registration without `ValueError` collision (today they raise). Add `force=True` kwarg to `ToolRegistry.register()` that bypasses the collision check.
4. Hooks engine must support per-plugin unregister.

**Files touched**:
- `opencomputer/plugins/loader.py` (modified, ~80 LOC)
- `opencomputer/tools/registry.py` (modified — `force=True` kwarg)
- `opencomputer/hooks/engine.py` (modified — per-plugin unregister)
- `opencomputer/agent/slash_commands_impl/plugin_reload_cmd.py` (modified)
- `opencomputer/cli_ui/slash.py` (modified — register `/reload-plugin`)

**Acceptance criteria**:
- Edit `extensions/weather-example/plugin.py`, change a string
- `/reload-plugin weather-example` in chat
- Call the tool from the agent — sees the new string
- No restart, no session loss
- `/reload-plugin all` reloads everything safely
- Reload fails cleanly on a broken plugin (rollback, error surfaced, old plugin still loaded)

**Size**: M (2 days). **Risk**: M-L (registries need per-plugin provenance — invasive but isolatable).

**Unblocks**: dev-loop for plugin authors.

---

## Recipe 7 — KawaiiSpinner / animated tool feedback

**Why seventh**: visible delight, but tactically less load-bearing than 1-6.

**Source pattern**: Hermes's `sources/hermes-agent/agent/display.py:573` (`KawaiiSpinner` — 10 spinner shapes, 10 waiting faces, 15 thinking faces, 15 verbs, optional wings).

**Scope** (depends on Recipe 4 for skin tokens):
1. New: `opencomputer/cli_ui/spinner.py` — port `KawaiiSpinner` verbatim. Strip the prompt_toolkit `StdoutProxy` branch (~200 LOC of dead code in OC's context — we don't use `patch_stdout`).
2. Re-theme defaults to OC vibe: `(♡＾▽＾♡)`, `(◍•ᴗ•◍)❤`, `( ˘ ³˘)♥`, etc. Same shape as Hermes, different aesthetic.
3. Skin engine override: skin's `spinner.waiting_faces`, `spinner.thinking_faces`, `spinner.thinking_verbs`, `spinner.wings` all read.
4. Wire into the agent loop where `rich.Console.status(...)` is currently used.
5. Add `/indicator <kaomoji|emoji|unicode|ascii|minimal>` slash command (matches Hermes's).

**Files touched**:
- `opencomputer/cli_ui/spinner.py` (new, ~250 LOC after StdoutProxy strip)
- 2-3 agent-loop call sites
- `opencomputer/agent/slash_commands_impl/indicator_cmd.py` (new)
- `opencomputer/cli_ui/slash.py` (modified)

**Acceptance criteria**:
- During tool call, OC chat shows animated spinner with rotating faces + verb (`(◍•ᴗ•◍)❤ synthesizing... ⠋`)
- Skin override works: `oc skin set mono` swaps to monochrome faces (post Recipe 4)
- `/indicator minimal` reduces to single dot (escape hatch for fatigue)
- Non-TTY (gateway, cron, `oc -p name 'prompt'`) sees no animation at all (`_is_tty` guard)

**Size**: M (1.5 days). **Risk**: S (additive; rich.Console.status fallback if anything breaks).

**Unblocks**: nothing.

---

## Recipe 8 — `oc plugins doctor <id>`

**Why eighth**: debugging. Today, when a plugin doesn't work, you read its source.

**Source pattern**: Hermes's `hermes plugins doctor`. OC has `cli_plugin_scaffold.py` for new-plugin authoring but not for installed-plugin diagnosis.

**Scope**:
1. New CLI: `oc plugins doctor <id>` or `oc plugins doctor --all`.
2. Per plugin, run these checks and report status:
   - Manifest schema valid (`opencomputer/plugins/manifest_validator.py`)
   - Entry module importable (try-import without calling `register`)
   - Required env vars present (`requires_env`)
   - Source policy allows the plugin's source kind
   - Plugin signature verifies against the catalog (if catalog-installed)
   - Hooks registered count (matches manifest declared count?)
   - Tools registered count
   - Last-load error from `~/.opencomputer/<profile>/plugins/.load_errors.json` (if exists)
   - Blocked by `plugins.disabled` config?

**Files touched**:
- `opencomputer/cli_plugin.py` (modified — add `doctor` subcommand)
- 1 new test
- Possibly add `~/.opencomputer/<profile>/plugins/.load_errors.json` write-on-error pattern to `loader.py` (small change)

**Acceptance criteria**:
- `oc plugins doctor weather-example` prints a row-per-check table with PASS/FAIL/SKIP
- `oc plugins doctor --all` prints the same for every installed plugin
- A plugin with missing `OPENWEATHER_API_KEY` shows `requires_env: FAIL (missing OPENWEATHER_API_KEY)`
- A plugin whose `register()` throws shows `register: FAIL <error_message>` with the traceback

**Size**: S (1 day). **Risk**: S (read-only).

**Unblocks**: every future plugin debugging conversation.

---

## Recipe 9 — Output styles slot

**Why ninth**: niche power-user but unique-to-Claude-Code so it earns the slot.

**Source pattern**: Claude Code's `output_styles` plugin component. Examples: `explanatory-output-style` (more verbose, more "here's why"), `learning-output-style` (more pedagogical, more breaking down).

**Scope**:
1. New plugin component: `OutputStyle` (dataclass at `plugin_sdk/output_style.py`).
2. Each style declares: `name`, `system_prompt_addition`, `tool_use_preferences`, `response_post_processor` (optional callable).
3. Plugins register output styles via `api.register_output_style(...)`.
4. New slash command `/output-style <name>` to switch.
5. Built-in styles: `default`, `concise`, `explanatory`, `learning`. Bundled in core, not as a plugin.
6. The active style's `system_prompt_addition` gets injected via `DynamicInjectionProvider` mechanism (already exists at `opencomputer/agent/injection.py`).

**Files touched**:
- `plugin_sdk/output_style.py` (new, ~50 LOC)
- `plugin_sdk/__init__.py` (modified — export `OutputStyle`)
- `opencomputer/agent/output_styles.py` (new — registry + 4 built-ins, ~200 LOC)
- `opencomputer/plugins/loader.py` (modified — `api.register_output_style`)
- `opencomputer/agent/injection.py` (modified — pull active style's prompt addition)
- `opencomputer/agent/slash_commands_impl/output_style_cmd.py` (new)
- `opencomputer/cli_ui/slash.py` (modified)

**Acceptance criteria**:
- `/output-style explanatory` → next response is noticeably more detailed
- `/output-style learning` → next response breaks down concepts more
- `/output-style default` → back to normal
- A plugin can register a custom style: `api.register_output_style(OutputStyle(name="executive", system_prompt_addition="Lead with the bottom line."))`

**Size**: M (2 days). **Risk**: M (touches `injection.py` which has 25+ events depending on it; needs careful test coverage).

**Unblocks**: future personality / format plugins.

---

## Recipe 10 — Per-plugin update notifier

**Why tenth**: maintenance ergonomics. Today users don't know when their plugins have updates.

**Source pattern**: Hermes's per-skill update notifier. `oc update` checks OC itself; we extend to plugins.

**Scope**:
1. New module `opencomputer/plugins/update_check.py` — given the installed-index (sources + versions), poll each plugin's source for newer version:
   - `catalog` source → re-fetch catalog, compare versions
   - `git` source → `git ls-remote` HEAD vs installed ref
   - `url` source → HEAD request, compare ETag or content-hash
   - `pypi` source → `pip index versions <pkg>`
2. Cache results in `~/.opencomputer/<profile>/plugins/.update_cache.json` (6 hour TTL — same as `cli_update_check.py`).
3. At session start (or on `oc plugins update-check`), surface a count: "3 plugin updates available — `oc plugins list --updates` to see."
4. `oc plugins update [<id>|--all]` to perform the updates.

**Files touched**:
- `opencomputer/plugins/update_check.py` (new, ~150 LOC)
- `opencomputer/cli_plugin.py` (modified — `update-check`, `update` subcommands)
- `opencomputer/cli_banner.py` (modified — surface count in splash, behind a config flag)

**Acceptance criteria**:
- `oc plugins update-check` polls all installed plugins, returns count
- Output shows per-plugin: current version → available version, source URL
- `oc plugins update weather-example` updates one plugin
- `oc plugins update --all` updates all (with confirmation)
- 6h cache prevents repeated network polls

**Size**: S (1 day). **Risk**: S (read-mostly; only `--all` does writes).

**Unblocks**: nothing.

---

## Suggested sprint plans

### Half-day (2 hours)
Just Recipe 2. Wire the 7 written-but-unregistered commands. Immediate visible win.

### Two-day sprint
Recipe 1 + Recipe 2 + Recipe 3. User markdown commands + the 7 commands + activation planner wired. Cold-start gets faster AND the user gets 7+ new commands AND can write their own. **Highest user-visible delta in the audit.**

### Five-day sprint
Recipes 1, 2, 3, 4, 8. Adds skin engine and plugin doctor on top. After this, OC matches Claude Code on plugin authoring DX and matches Hermes on visual customization.

### Two-week sprint
All ten recipes. ~15-18 dev-days total. After this OC is at "best-of-three on plugin architecture" structurally, and remains ahead of all three on awareness, security, multi-surface orchestration, and OS integration.

---

## Risk register (cross-cutting)

| Risk | Mitigation |
|---|---|
| **Activation planner narrows too aggressively, breaks plugin that relies on early-load side effect** | Recipe 3 ships with `OPENCOMPUTER_LOAD_ALL_PLUGINS=1` escape hatch. First production run = enable the env var, run a week, only then flip to narrow. |
| **Hot-reload registries miss per-plugin provenance, leaks stale tool registrations** | Recipe 6 requires `ToolRegistry`/`HookEngine` to add per-plugin tracking BEFORE reload works. Order: provenance first (separate PR), then reload (separate PR). |
| **Skin engine port introduces regression on default skin** | Recipe 4's byte-identical-default-skin snapshot test is non-negotiable. CI fails if today's splash drifts even by one byte. |
| **User markdown commands shadow built-ins (e.g., user's `/help.md`)** | Recipe 1 conflict policy: project > per-profile > global > built-in. Log WARN on every conflict so user sees it. |
| **Marketplace signing keys lost or rotated** | Recipe 5 stores key fingerprint per-marketplace at `add` time; trust check is per-marketplace. Rotation = `oc plugin marketplace remove + add`. |
| **Output styles fight with persona injection** | Recipe 9 uses the same `DynamicInjectionProvider` mechanism as personas — order them: persona first (slot 1), output style second (slot 4b), so persona always wins on tone. |

---

## What this plan deliberately does NOT include

Cross-reference with the audit doc §8 ("What NOT to copy"). These appear in the source repos but are net-negative for OC:

- Always-on status bar → defer until user demand
- 8 built-in skins → ship 3, let users author the rest
- `/yolo` mode → undermines OC's consent invariant; skip
- Plugin process isolation → industry-norm in-process is fine; signed catalogs + Tirith pre-exec + source policy is OC's practical answer
- `hermes debug share` → cloud upload pattern; OC stays local
- Nous Portal OAuth → vendor-specific control plane; out of positioning
- LSP servers as plugin component slot → bundled extension shape is sufficient; user demand hasn't surfaced

---

## File list summary (everything touched by the 10 recipes)

| Recipe | Files new | Files modified |
|---|---|---|
| 1 | `agent/markdown_commands.py` | `cli_ui/slash.py` |
| 2 | — | `cli_ui/slash.py` |
| 3 | — | `agent/loop.py`, `plugins/loader.py` |
| 4 | `cli_ui/tokens.py`, `cli_ui/skin_engine.py`, `cli_skin.py`, 3 skin YAMLs | `cli_banner.py`, `agent/display.py` |
| 5 | `plugins/marketplaces.py` | `plugins/remote_install.py`, `cli_plugin.py` |
| 6 | — | `plugins/loader.py`, `tools/registry.py`, `hooks/engine.py`, `agent/slash_commands_impl/plugin_reload_cmd.py`, `cli_ui/slash.py` |
| 7 | `cli_ui/spinner.py`, `agent/slash_commands_impl/indicator_cmd.py` | agent loop call sites, `cli_ui/slash.py` |
| 8 | — | `cli_plugin.py`, `plugins/loader.py` |
| 9 | `plugin_sdk/output_style.py`, `agent/output_styles.py`, `agent/slash_commands_impl/output_style_cmd.py` | `plugin_sdk/__init__.py`, `agent/injection.py`, `cli_ui/slash.py` |
| 10 | `plugins/update_check.py` | `cli_plugin.py`, `cli_banner.py` |

Net: 10 new files + ~12 modified across 5 dev-weeks. Compatible with current `plugin_sdk` contract; one minor-version SDK bump for Recipe 9 (`OutputStyle` export).

---

Last verified: OC `git rev-parse HEAD` = `3849a7eb`, 2026-05-17.
