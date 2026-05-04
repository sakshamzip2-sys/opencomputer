# Hermes Tier-3 Finale — Design + Plan + Audit

**Date:** 2026-05-04
**Status:** Brainstorm + plan + audit consolidated. Execute now.
**Inputs:** Hermes kanban docs + tutorial; OC code surveyed.

---

## Goal

Close the Hermes-doc-derived items I called "out-of-scope" in the
prior wrap-up. Honest scope: 8 items in the original list; 3 already
shipped (silently — verbatim port did them); 5 truly remain.

| # | Item | Status before this PR-set |
|---|---|---|
| 1 | Matrix plugin auto-wires the ConsentGate bridge | gap |
| 2 | Dispatcher auto-passes `--skills kanban-worker` | **done** (db.py:2152) |
| 3 | Multi-board (`oc kanban boards switch <slug>`) | gap |
| 4 | Drag-drop kanban dashboard UI | **done** (verbatim hermes dist) |
| 5 | Lanes-by-profile view | **done** (verbatim hermes dist) |
| 6 | `/kanban` slash command bypassing running-agent guard | gap |
| 7 | Gateway auto-subscribe on `/kanban create` | gap |
| 8 | Output truncation for platform message-length caps | gap |

This is exactly what the user-discovery work in §1 of the survey
caught — three of the eight items had already shipped. Item 4/5 work
today via the kanban dashboard plugin's `dist/index.js`. Item 2 is
visible in any worker's `ps aux` output. Calling them done means I'm
not duplicating effort.

---

## Brainstorm

### What shape does each gap have?

**Item 1 — Matrix bridge auto-wire**

Today the bridge ships as a callable; nothing wires it to the gateway's
ConsentGate. Auto-wiring requires:
- `PluginAPI` exposing the consent gate (or some hook that lets a
  plugin install a prompt handler).
- `extensions/matrix/plugin.py register()` reading `matrix.consent_*`
  config and calling the bridge installer.

Two plumbing options:
- **A**: Add `consent_gate` field to `PluginAPI`; gateway sets it
  before plugin load. Direct, simplest.
- **B**: Add `set_consent_prompt_handler(handler)` to `PluginAPI` so
  plugins don't get the gate object directly. Better encapsulation;
  same effect.

Pick **B** — narrower surface, fewer ways for plugins to mess with
the gate's internals. The gate's `set_prompt_handler` is the only
mutation we want to expose.

**Item 3 — Multi-board**

Hermes has `~/.hermes/kanban/boards/<slug>/{kanban.db, workspaces, logs}`
plus `hermes kanban boards switch <slug>`. The DB path resolves
through:
1. `OC_KANBAN_DB` env (highest precedence — already implemented)
2. `OC_KANBAN_BOARD` env → `<root>/kanban/boards/<slug>/kanban.db`
3. Active-board file `<root>/kanban/.active-board` → same shape
4. Default `<root>/kanban.db` (legacy, single-board)

Slug validation: lowercase alphanumerics + hyphens/underscores, 1-64
chars, must start alphanumeric. Same as hermes.

CLI surface: `oc kanban boards {create,list,switch,rename,rm}`.

Critical: the dispatcher must inject `OC_KANBAN_BOARD` into worker
env so workers converge on the same board the dispatcher is using.
Mirrors the existing `OC_KANBAN_DB` + `OC_KANBAN_WORKSPACES_ROOT`
pattern at db.py:2143.

**Item 6 — `/kanban` slash command**

OC's `plugin_sdk.slash_command.SlashCommand` ABC exists. Coding-harness
already registers `/plan`, `/checkpoint`, `/diff`, etc. The kanban
extension just needs a `KanbanSlashCommand(SlashCommand)` whose
`execute()` dispatches into `oc kanban <verbs>` via `kanban_command()`.

Running-agent guard bypass: looking at OC's slash dispatcher, slash
commands are processed BEFORE the agent loop spins up. If a turn is
mid-flight, OC currently rejects new input — `/kanban list` should be
exempt because it's a board read, not new agent work. The guard lives
in `gateway/dispatch.py`; bypass = a marker on the SlashCommand class
that the dispatch path checks before queueing.

**Item 7 — Auto-subscribe on `/kanban create` from gateway**

When the user types `/kanban create "task X" --assignee foo` from a
gateway chat (telegram/discord/matrix), the originator should
auto-subscribe to terminal events for that task. Hermes does this via
`notify-subscribe` table writes. OC ships the table + CLI subcommand
(`oc kanban notify-subscribe`); just needs the gateway path to
auto-call it. Hook lives inside the `KanbanSlashCommand.execute()`
when the verb is `create`.

**Item 8 — Output truncation**

Notifier deliveries from gateway send notification text to channels
that have small message-length caps (Telegram 4096, Discord 2000, etc.).
Hermes truncates to 3800 chars + appends an ellipsis marker.

OC's gateway already has `outgoing_drainer.py` which sends queued
notifications. Add a small `truncate_for_platform(text, platform)`
helper that drops `BaseChannelAdapter.max_message_length`-1 chars
+ "\n\n…[truncated]" (already used by dingtalk adapter).

### Scope discipline

PR groupings minimize touched-files-per-PR:

- **PR-A** (items 6 + 7 + 8): the in-chat /kanban experience. All three
  changes converge on `extensions/kanban/` (new) + the gateway. Bundling
  them is more reviewable than 3 tiny PRs.
- **PR-B** (item 1): matrix consent bridge auto-wire. Plugin SDK +
  matrix plugin. Self-contained.
- **PR-C** (item 3): multi-board. Touches kanban/db.py + cli.py. Bigger
  but isolated.

Three PRs, each independently green.

---

## Plan (executable)

### PR-A — `/kanban` slash + auto-subscribe + truncation

**Branch:** `feat/wave6-kanban-slash`
**LOC:** ~400

1. Create `opencomputer/kanban/slash_command.py`:
   - `class KanbanSlashCommand(SlashCommand)`:
     - `name = "kanban"`, `aliases = ()`
     - `class_attribute = bypass_running_guard = True` (new flag)
     - `execute(args, context) -> SlashCommandResult` — runs
       `oc kanban <verb>` argparse path via the existing
       `kanban_command()` entry, captures stdout, returns truncated
       to 3800 chars.

2. Modify `opencomputer/kanban/cli.py`:
   - Export `kanban_command()` already; add a thin
     `run_kanban_argv(argv: list[str]) -> tuple[int, str]` helper that
     redirects stdout via `contextlib.redirect_stdout` so the slash
     command can capture text without spawning a subprocess.

3. Register the slash command from a new tiny plugin
   `opencomputer/kanban/plugin_register.py` that the kanban package
   exposes for the gateway to import — OC's plugin loader doesn't have
   to load anything new because kanban ships in-package, not as an
   `extensions/*` plugin. The slash command registers via
   `PluginRegistry.register_slash_command()` directly when the
   gateway boots.

4. Modify `opencomputer/agent/slash_dispatcher.py` to honor
   `bypass_running_guard` — if the matched command has the flag, run
   it even when an agent turn is mid-flight.

5. Inside `KanbanSlashCommand.execute()`, when verb == "create" AND
   the originating chat is from a gateway adapter, write a row to
   the existing `kanban_notify_subscribe` table for that task.

6. Output truncation helper `opencomputer/gateway/_truncate.py`:
   `def truncate_for_platform(text: str, max_len: int = 3800) -> str`.
   Used by the slash command result + outgoing_drainer notifier.

7. Tests:
   - `/kanban list` → returns table text, truncated if huge
   - `/kanban create … --assignee X` → creates task + adds notify row
   - `bypass_running_guard` flag → mid-turn slash command works
   - `truncate_for_platform` cuts at 3800 + appends ellipsis

### PR-B — Matrix consent bridge auto-wire

**Branch:** `feat/wave6-matrix-consent-autowire`
**LOC:** ~150

1. `plugin_sdk/__init__.py`: add a method on `PluginAPI` interface:
   `set_consent_prompt_handler(handler) -> None`. Plugins call this to
   install a ConsentGate prompt handler without touching the gate
   directly.

2. `opencomputer/plugins/registry.py`: implement
   `set_consent_prompt_handler` on the concrete `PluginAPI`. Closure
   captures the gate and forwards calls.

3. `opencomputer/gateway/server.py`: thread the gate through to the
   shared `PluginAPI` so `set_consent_prompt_handler` resolves to a
   real gate.

4. `extensions/matrix/plugin.py register()`:
   - Read `matrix.consent_handler` + `matrix.consent_chat_id` from
     config (use `parse_consent_config` from PR #444).
   - If enabled: build the handler via `make_matrix_prompt_handler`
     and install via `api.set_consent_prompt_handler(handler)`.
   - Failure to wire is logged but not fatal.

5. Tests:
   - PluginAPI exposes `set_consent_prompt_handler`
   - Calling it from a plugin updates the gate's handler
   - matrix plugin registers handler when config opted-in
   - matrix plugin no-ops when consent_handler=false
   - matrix plugin no-ops when chat_id missing

### PR-C — Multi-board

**Branch:** `feat/wave6-kanban-multi-board`
**LOC:** ~600

1. `opencomputer/kanban/db.py`:
   - New helpers: `boards_root() -> Path`,
     `board_db_path(slug: str | None) -> Path`,
     `active_board() -> str | None`,
     `set_active_board(slug: str | None) -> None`,
     `validate_slug(slug: str) -> None`.
   - `kanban_db_path()` updated to honor:
     - `OC_KANBAN_DB` (highest)
     - `OC_KANBAN_BOARD` env
     - Active-board state file
     - Legacy `<root>/kanban.db`

2. `opencomputer/kanban/cli.py`: new subcommands
   `boards {create,list,switch,rename,rm,active}`. Each writes to
   the active-board state file or creates `<root>/kanban/boards/<slug>/`
   tree.

3. `_default_spawn` (db.py:2143): inject `OC_KANBAN_BOARD` into worker
   env if active board is set, mirroring the existing OC_KANBAN_DB
   pattern.

4. Tests:
   - `boards create foo` → directory + state row
   - `boards switch foo` → active-board file written
   - `kanban_db_path()` honors `OC_KANBAN_BOARD`
   - Slug validation rejects bad slugs
   - Worker env gets `OC_KANBAN_BOARD` after switch

---

## Self-audit

10 lenses applied:

### A1. Silent API drift
- `SlashCommand.execute` signature: `(args, context)` — verified in
  `plugin_sdk/slash_command.py`.
- `SlashCommandResult.output` field: verified.
- `register_slash_command` lives on PluginAPI: verified at
  `loader.py:1056`.
- `PromptHandler` signature for ConsentGate: verified for PR #444.
- `_default_spawn` env-injection pattern: verified at db.py:2143.

### A2. Multi-board migration
Existing single-board databases sit at `<root>/kanban.db`. Migration
lazy: if `OC_KANBAN_BOARD` is unset, behavior is identical to today
(legacy path returned). Setting an active board is opt-in. Existing
users who never run `boards switch` see zero change. ✅

### A3. Slash-command running-guard bypass — race
The bypass means `/kanban list` runs while another agent turn is in
flight. Both writers share the kanban DB. Kanban DB uses WAL +
`BEGIN IMMEDIATE` so concurrent writes serialize cleanly. No race. ✅

### A4. Auto-subscribe duplication
If the user runs `/kanban create` twice for the same chat+task, the
second add must not create a duplicate subscription. Hermes uses an
upsert (check `kanban_notify_subscribe` schema). OC's table already
exists from PR #429 — verify upsert behavior or add `INSERT OR
IGNORE`. **Refinement:** explicit `INSERT OR IGNORE` plus
`(task_id, platform, chat_id)` UNIQUE.

### A5. Truncation cuts mid-codeblock
`text[:3800] + "\n\n…[truncated]"` can split a markdown code fence,
producing broken render on the channel. **Refinement:** when text
contains a code fence, scan back from the cut to find the previous
closed fence and cut there. Acceptable to fall back to the naive cut
if no closed fence is found within 200 chars of the boundary.

### A6. PluginAPI surface bloat
Adding `set_consent_prompt_handler` is a 1-method addition. Symmetric
with the existing `register_slash_command`. ✅

### A7. ConsentGate availability before plugin load
Order matters: `Gateway.__init__` constructs the loop (which builds
the gate) BEFORE plugins load via `PluginRegistry.load_all`. So at
plugin-load time the gate exists. Verified at `gateway/server.py`
init order. ✅

### A8. Slash dispatcher needs to know bypass flag
The flag goes on the SlashCommand class. The dispatcher reads
`getattr(cmd, "bypass_running_guard", False)` so older commands
without the attribute default to "no bypass" (current behavior). ✅

### A9. Plugin-loading order across boots
Matrix consent bridge requires matrix to be `inbound_sync: true` for
its handler to ever fire. If a user has consent_handler=true but
inbound_sync=false, the plugin already returns False from the
handler (PR #444's logic). Auto-wire path inherits that safety. ✅

### A10. Honest deferrals after this batch
After PRs A/B/C land, every Hermes-doc-named feature with a clear
implementation path is shipped. The remaining "out-of-scope" items
the docs name (multi-host coord, cross-board dependencies,
auto-assignment routing) are explicitly declared out-of-scope by
hermes itself. Nothing left.

---

## Final plan summary

| PR | Title | Branch | LOC | Tests |
|---|---|---|---|---|
| A | `/kanban` slash + auto-subscribe + truncation | feat/wave6-kanban-slash | ~400 | 8+ |
| B | Matrix consent bridge auto-wire | feat/wave6-matrix-consent-autowire | ~150 | 5+ |
| C | Multi-board | feat/wave6-kanban-multi-board | ~600 | 8+ |

Total ~1150 LOC across 3 PRs. Execute now.
