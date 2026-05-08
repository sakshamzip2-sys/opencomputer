# Hermes CLI / TUI / Sessions v2 — parity design

**Date:** 2026-05-08
**Source doc:** `~/Downloads/hermes-cli-tui-sessions-v2.md`
**Status:** brainstorm-audited, ready for plan

## Goal

Close the remaining gap between OpenComputer's CLI/TUI/Sessions surface and the
Hermes CLI/TUI/Sessions v2 reference doc. After ~30 prior PRs the surface is
~90 % parity; this spec covers the genuinely-missing items found by a
discovery audit.

## Scope (after brainstorm audit)

The naive scope read from the doc looked like ~28 items across four themes
(CLI polish, slash parity, sessions polish, TUI polish). The audit cut ~9
items already shipped (`/help /model /goal /compress /skills /tools /voice
/steer /sessions /yolo /kanban /<skill-name>`, conversation recap on resume,
FTS5 session search, Markdown+LaTeX in TUI, bracketed-paste handler,
`chat -q/-c`, `chat resume`, etc.).

Net 21 items remain, organised as a single PR with four commits.

### Theme A — CLI polish (7 items)

| # | Item | Surface |
|---|---|---|
| A1 | Multiline paste preview `[pasted: N lines, M chars]` | `cli_ui/paste_preview.py` |
| A2 | Strip rendered `**bold**`/`*italic*`/`#` from CLI final assistant prose; preserve code/lists/tables | `cli_ui/markdown_strip.py` |
| A3 | Per-prompt elapsed time `⏱` (live) / `⏲` (frozen) toolbar | `cli_ui/per_prompt_elapsed.py` |
| A4 | Light terminal detection (env + COLORFGBG + OSC 11, 200 ms timeout) | `cli_ui/theme_detect.py` |
| A5 | Busy-indicator styles `kawaii \| minimal \| dots \| wings \| none` (config-driven, equal glyph widths) | `cli_ui/busy_indicator.py` |
| A6 | Quick commands `quick_commands:` from config.yaml (`exec`/`alias`, 30 s timeout, zero-token) | `agent/quick_commands.py` |
| A7 | `Ctrl+Z` suspend (Unix) — `enable_suspend=True` on prompt-toolkit | `cli_ui/input_loop.py` |

### Theme B — Slash parity (3 missing + 1 audit pass)

| # | Item | Surface |
|---|---|---|
| B1 | `/rollback [N]` — list checkpoints / restore Nth via existing `cli_checkpoints` | `slash_commands_impl/rollback_cmd.py` |
| B2 | `/busy [interrupt\|queue\|steer\|status]` (deprecates `/queue-mode` as alias) | `slash_commands_impl/busy_cmd.py` |
| B3 | `/details [section] [hidden\|collapsed\|expanded\|reset]` — TUI section visibility setter | `slash_commands_impl/details_cmd.py` |
| B4 | Audit pass — verify `/tools /skills /voice /model /compress /help` handlers do what the doc says | (per-handler tweaks) |

### Theme C — Sessions polish (6 items)

| # | Item | Surface |
|---|---|---|
| C1 | `oc sessions` plural top-level alias of `oc session` | `cli.py` (1 add_typer) |
| C2 | `oc sessions stats` — counts by source + DB size | `cli_session.py` |
| C3 | `oc sessions export <path>` — JSONL dump (full or filtered) | `cli_session.py` |
| C4 | `oc sessions rename <id> "title"` — CLI side of existing `/rename` slash | `cli_session.py` |
| C5 | Resume by name with lineage match (`oc chat -c "name"` → latest in `name [#N]` lineage) | `cli.py:_resolve_resume_target` |
| C6 | Resume by title string (`oc chat --resume "title"`) | `cli.py:_resolve_resume_target` |
| C7 | Title lineage helper `next_title_in_lineage(base)` (used at fork time, best-effort) | `agent/title_generator.py` |

### Theme D — TUI polish (5 items)

| # | Item | Surface |
|---|---|---|
| D1 | Light terminal detection in TUI (env `OPENCOMPUTER_TUI_THEME` + COLORFGBG + OSC 11) | `ui-tui/src/theme.ts`, `ui-tui/src/lib/themeDetect.ts` |
| D2 | Working dir + git branch in TUI status line (mtime-cached `git/HEAD`) | `ui-tui/src/components/appChrome.tsx` |
| D3 | `/sessions` modal picker — verify `sessionPicker.tsx` is wired into slash dispatch | `ui-tui/src/components/sessionPicker.tsx` |
| D4 | `/reload` — re-read `.env` + `config.yaml` without restart (handler may already exist; verify) | `cli_ui/slash_handlers.py` |
| D5 | `/mouse` — toggle `display.mouse_tracking`, persists | `slash_commands_impl/mouse_cmd.py`, `ui-tui/...` |

## Architecture

### A1 — Multiline paste preview

prompt-toolkit's bracketed-paste handler at `input_loop.py:220` already swallows
the paste payload; today it inserts the full text into the buffer. New behaviour:

1. If the pasted payload contains ≥ 3 newlines OR > 240 chars, store the raw
   payload in a side dict keyed by a UUID-suffixed marker (`__PASTE_<uuid8>__`).
2. Insert the marker as a single visible token: `[pasted: 47 lines, 1842 chars]`.
3. On `Enter`, before sending, scan the buffer text for any
   `[pasted: ...]` markers and splice the original payload back in.
4. `Ctrl+C` clears markers + side dict.

Edge cases:

- Paste-in-paste — each gets its own marker.
- Multiple markers in one buffer — splice in scan order.
- User edits the marker text — fall back to literal (no replacement). Document
  the quirk as a known limitation.

### A2 — Markdown stripping in CLI final responses

OpenComputer renders the assistant turn through a Rich console with
`Markdown(text)` for the final flush in `cli_ui/streaming.py`. Hermes doc says
strip `**bold**` and `*italic*` wrappers (and ATX headings) so the displayed
text reads as terminal prose, not raw markup-leaking source.

Approach: introduce `cli_ui/markdown_strip.py` with one pure function
`strip_for_terminal(md: str) -> str` that:

- preserves fenced code blocks (\`\`\`lang ... \`\`\`), inline code, lists,
  tables, block quotes, links;
- strips `**X**` → `X`, `*X*` → `X`, `_X_` → `X`, leading `#`/`##` → bold-only
  (Rich already styles ATX headings; we drop the literal `#` markers).

Apply ONLY at finalize time (`StreamingRenderer.finalize_turn(...)`) and ONLY
to assistant text. Streaming chunks are unchanged. Tool results / system
messages unchanged. Gateway / TUI / Markdown.tsx unaffected.

Off-switch: `display.markdown_strip: true` (default) / `false`.

### A3 — Per-prompt elapsed time

Existing `status_line.py:315` shows session-wide duration. Add a separate
prompt-toolkit BottomToolbar slot (or right-aligned status line cell, behind
existing context bar) with two states:

- **live**: `⏱ 12s` — increments every 1 s while agent is running.
- **frozen**: `⏲ 32s / total 3m 45s` — stops on turn complete; resets to live
  when next user prompt arrives.

State tracked in `cli_ui/per_prompt_elapsed.py`:

```py
class PromptClock:
    def start(self) -> None: ...    # called on user prompt sent
    def stop(self) -> None: ...     # called on turn finalize
    def render(self) -> str: ...    # called by status line on tick
```

Reset on `Ctrl+C` interrupt: stop + clear so the next prompt starts fresh.

### A4 — Light terminal detection

Detection layers, highest priority first (matches Hermes doc):

1. `OPENCOMPUTER_TUI_THEME` env (`light` / `dark` / 6-char hex bg).
2. `COLORFGBG` env (xterm-style `0;15` etc.).
3. OSC 11 background probe: write `\033]11;?\033\\` to stdout, read the
   reply with a 200 ms read timeout. Parse `rgb:RRRR/GGGG/BBBB` reply, compute
   luminance; > 0.5 → light, else dark.
4. Default: dark.

Output: a normalized `Theme` dataclass with `kind: 'light' | 'dark'` and a
hex bg colour. Consumed by `cli_ui/style.py` for status-line / banner accents.
Pure function — no state. The probe is wrapped in `try/except + timeout` so a
dumb terminal can never block startup.

OSC 11 implementation note: must be done before prompt-toolkit takes the
TTY. We do it once in `cli_banner.py` (the very first thing on launch) and
cache the result.

### A5 — Busy indicator styles

Today the kawaii animation is hard-coded in the busy-spinner renderer. Pull
the glyph table out into `cli_ui/busy_indicator.py`:

```py
STYLES = {
    "kawaii":  ["◜", "◠", "✧٩(ˊᗜˋ*)و✧", ...],
    "minimal": ["⋯", "···", "·"],
    "dots":    ["⠁", "⠃", "⠇", "⠧", ...],
    "wings":   ["≼", "≼", "≽", "≽"],
    "none":    [""],
}
```

Each style has a uniform-width invariant: every frame in the same style is
the same display-width (using `wcwidth`). Renderer pads to the max width
on init so the status bar doesn't jitter on rotation.

Selection: `display.busy_indicator.style` from config.yaml (default `kawaii`),
overridable per-session via internal `runtime.custom["busy_style"]`.

### A6 — Quick commands

`~/.opencomputer/config.yaml` reads a top-level `quick_commands:` map:

```yaml
quick_commands:
  status:
    type: exec
    command: systemctl status oc-agent
  gpu:
    type: exec
    command: nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader
  restart:
    type: alias
    target: /gateway restart
```

Loaded once per process via `agent/quick_commands.py:load_quick_commands(cfg)`.

Dispatch lifecycle (called from REPL input handler BEFORE slash dispatch):

```py
def maybe_run_quick(cmd: str, args: str) -> QuickResult | None:
    qc = QUICK_COMMANDS.get(cmd)
    if not qc:
        return None
    if qc.type == "exec":
        return run_exec(qc.command, args, timeout=30)
    if qc.type == "alias":
        if depth >= 5: return error("alias loop")
        return rerun_through_slash_dispatcher(qc.target, args, depth+1)
```

Implementation details:

- `run_exec` uses `subprocess.run(shell=True, timeout=30)` — SIGTERM on
  timeout, SIGKILL after 1 s. Output captured + truncated to 4 KB.
- Aliases recurse through the slash dispatcher (so `/gateway restart` flows
  through normal slash parsing). Alias depth capped at 5.
- Quick commands run BEFORE slash dispatch so a quick command can shadow a
  slash name. Document this prominently — same as Hermes.
- Quick commands work in CLI today; gateway-platform parity is out of scope.
- New `oc config quick-commands list` CLI subcommand prints all loaded entries.
- One-line warning at config load if a quick-command name collides with a
  registered slash. Helps users notice surprising shadows.

### A7 — Ctrl+Z suspend

prompt-toolkit's `Application(enable_suspend=True)` already supports SIGTSTP.
Enable it in our `Application` construction in `input_loop.py`, guarded by
`if sys.platform != "win32"`.

After resume (`fg`), prompt-toolkit re-takes the TTY and reissues the prompt.
Print the same one-line hint as Hermes:

```
OpenComputer Agent has been suspended. Run fg to bring OpenComputer Agent back.
```

via SIGTSTP handler that prints to stderr before re-raising the default.

### B1 — `/rollback [N]`

Wraps existing `cli_checkpoints` (`opencomputer/cli_checkpoints.py`):

- `/rollback` (no arg) — print the last 10 checkpoints with index, label,
  timestamp, file count.
- `/rollback N` — restore the Nth most-recent checkpoint via the same
  RewindStore call CLI uses.

Implementation: `slash_commands_impl/rollback_cmd.py` imports
`opencomputer.checkpoint.RewindStore` (the underlying library, not the CLI
typer app — typer-inside-slash is fiddly). Returns a `SlashCommandResult`
with the formatted table or success message.

### B2 — `/busy [interrupt|queue|steer|status]`

- New canonical name. State stored in `runtime.custom["busy_input_mode"]`.
- `/queue-mode` is preserved as a deprecation-emitting alias (writes the same
  key, prints "/queue-mode is renamed to /busy. /queue-mode still works.").
- `interrupt` (default) — agent interrupt on user message during turn.
- `queue` — message queued for next turn.
- `steer` — message injected via `agent/steer.py` after next tool call (falls
  back to `queue` if no tool call this turn).
- `status` — print current mode + describe each.

### B3 — `/details [section] [mode]`

Args:

- `/details [hidden|collapsed|expanded|cycle]` — global section mode (writes
  `runtime.custom["details_mode"]`).
- `/details <section> [hidden|collapsed|expanded|reset]` — single-section
  override. Sections: `thinking`, `tools`, `subagents`, `activity`. Writes
  `runtime.custom["sections"][<section>]`.

The TUI already reads these keys (`ui-tui/src/components/thinking.tsx:736`).
Just wire the slash command. CLI display ignores them today — that's fine.

### B4 — Audit pass

Run a quick semantic check that each existing slash handler does what the
Hermes doc specifies. Items to verify (not replace):

- `/tools` — must list registered tools by name.
- `/skills` — must support `browse` arg → invoke skills_hub.
- `/voice` — must support `on / off / tts / status`.
- `/help` — must group by category, support `/help <command>` lookup.
- `/compress` — must call CompactionEngine.force_compact() (already wired
  via `_force_compact_next_turn` in loop.py — verify it actually fires).
- `/model` — must support `provider:model` syntax (cross-provider swap).

Each finding becomes a small inline fix in the relevant `*_cmd.py` or
`slash_handlers.py` entry — not a new file.

### C1–C4 — `oc sessions` plural top-level

Add a single `app.add_typer(session_app, name="sessions")` line so `oc sessions`
and `oc session` resolve to the same subapp. Then add three new subcommands:

- `@session_app.command("stats")` — counts by source from SQL `GROUP BY source`,
  total messages, DB file size, oldest / newest session timestamps.
- `@session_app.command("export")` — JSONL dump. Args: `<path>`, `--source`,
  `--session-id`, `--include-messages` (default true). One JSON object per line.
- `@session_app.command("rename")` — `<id> <title...>`. Title joined from argv
  rest so `oc sessions rename ID debugging auth flow` works without quotes.

### C5–C6 — Resume by name / title

Extend `_resolve_resume_target(spec)` in `cli.py`:

```py
# After existing last/pick handling, before falling through:
# 1. Exact title match — must be unique (titles already have unique idx).
row = db.find_session_by_title(spec)
if row: return row["id"]
# 2. Lineage match — find newest in 'name', 'name #2', 'name #3' family.
rows = db.find_sessions_by_title_lineage(spec)
if rows: return rows[0]["id"]   # rows ordered by started_at DESC
# 3. Otherwise treat as id-prefix (existing behaviour downstream).
return None
```

New `SessionDB` methods:

- `find_session_by_title(title: str) -> Row | None` — `SELECT * WHERE title = ?`.
- `find_sessions_by_title_lineage(base: str) -> list[Row]` —
  `WHERE title = :base OR title GLOB :base || ' #*' ORDER BY started_at DESC`.

`oc chat -c "my project"` flows through `cont=True; resume=""; ` → existing
code converts to `"last"`. We add: when `cont=True` AND a positional `action`
is supplied, route through the new helper. Audit existing chat() entry to
confirm the flag combinations work.

### C7 — Title lineage

Add `next_title_in_lineage(db, base) -> str` in `agent/title_generator.py`:

- Query `find_sessions_by_title_lineage(base)`.
- If empty: return `base`.
- Else: pick the highest existing `#N`, return `base + " #" + str(N+1)`.

Hook it up at `oc session fork` time via a new `--inherit-title` flag.
Compaction-fork integration is a future PR.

### D1 — Light terminal detection in TUI

Mirror A4 in TS. New `ui-tui/src/lib/themeDetect.ts`:

- Reads `process.env.OPENCOMPUTER_TUI_THEME` first.
- Reads `process.env.COLORFGBG`.
- OSC 11 query via raw `process.stdout.write('\x1b]11;?\x1b\\')` + 200 ms
  `process.stdin` read.
- Returns `'light' | 'dark'`.

Result is consumed in `theme.ts` to swap the active palette. Default `dark`.

### D2 — Working dir + git branch in TUI status

Add a small `useGitBranch()` React hook:

- On mount, read `${cwd}/.git/HEAD` (or worktree pointer) using fs.promises.
- Cache the result keyed on file mtime; refresh only when mtime changes.
- Display `~/projects/foo (main)` in the status bar component.

For symlinked worktrees (`-w` mode), follow `.git` if it's a file.

### D3–D5 — `/sessions /reload /mouse`

These already have CommandDef rows in `cli_ui/slash.py`. Verify the TUI
slash-routing:

- `/sessions` → opens `sessionPicker.tsx` modal.
- `/reload` → reloads `.env` + `config.yaml` in-process.
- `/mouse` → toggles `display.mouse_tracking` config and emits a renderer
  event to enable/disable mouse tracking.

For `/reload` we reload in-process (not via gateway endpoint); the slash
already runs in the agent process. Re-reads `~/.opencomputer/.env` and
`~/.opencomputer/config.yaml`, hot-applies them via existing config_store.

## Data flow

- A1: stdin → bracketed-paste handler → `_paste_store[uuid] = full_text` →
  buffer holds marker → on send, splice → agent loop sees full text.
- A6: stdin → `maybe_run_quick(cmd, args)` BEFORE slash dispatch → if matched,
  short-circuit (exec or alias-recurse) → return result without any LLM call.
- B1: slash dispatch → `RollbackCommand.execute(args, runtime)` → reads
  `cfg.checkpoint.store_path` → returns formatted result.
- C5: `oc chat <name>` → `_resolve_resume_target(spec)` → SessionDB title /
  lineage lookup → resolve to id → existing chat resume path.

## Testing strategy

| Area | Approach |
|---|---|
| A1 paste preview | Unit-test the splice function with N markers; integration test in input_loop with simulated bracketed-paste. |
| A2 markdown strip | Pure-function unit tests with golden fixtures (code blocks preserved, bold stripped). |
| A3 elapsed | PromptClock state machine unit tests — start, stop, reset on Ctrl+C. |
| A4 theme detect | Mock env + COLORFGBG + a scripted OSC 11 reply over a pipe. |
| A5 busy styles | Width-uniformity assertion per style via `wcwidth`. |
| A6 quick commands | `tmp_path` config.yaml; assert exec result + alias recursion + 30 s timeout (with a 0.1 s fake `sleep`). |
| A7 Ctrl+Z | Skipped on Windows; unit test `enable_suspend=True` flag wiring only. |
| B1 /rollback | Mock RewindStore; assert listing format + restore call. |
| B2 /busy | Direct execute() test; assert key write and /queue-mode alias deprecation message. |
| B3 /details | Direct execute() tests for global + per-section paths. |
| C1–C4 | Typer CliRunner tests against a tmp `sessions.db`. |
| C5–C7 | Mock SessionDB; assert title vs lineage vs prefix dispatch. |
| D1 TUI theme | Mock `process.env`; pure-function detection test. |
| D2 git branch | Tmp git repo, switch branches, assert hook output. |
| D3–D5 | Existing TUI Vitest; add tests for slash → modal/handler wiring. |

Coverage target: 90 %+ on every new module. Full pytest + ruff + Vitest before
PR open. (See memory: `feedback_full_suite_audit.md` — never push without
deep testing.)

## Risks / open issues

1. **OSC 11 hang on dumb terminals** — mitigated by 200 ms timeout and broad
   `try/except`. Worst case: detection fails silently, dark default kicks in.
2. **prompt-toolkit `enable_suspend=True` semantics** — tested manually on
   macOS / Linux; Windows path is no-op. Documented.
3. **Title-lineage helper without compaction-fork** — helper ships unused
   except by manual `oc session fork --inherit-title`. That's still correct
   behaviour; we don't promise auto-lineage on compaction yet.
4. **Quick-command shadowing of slash** — documented behaviour; we print a
   one-line warning at config load if a quick-command name collides.
5. **`/reload` in-process** — re-reads files but does NOT restart the model
   client or re-init the agent; only config-store-backed values pick up the
   change. Document this constraint.

## Out of scope

- Compaction-as-fork (separate spec, larger).
- Cross-platform quick-commands in messaging gateway (skills already cover).
- `/terminal-setup` for VS Code / Cursor / Windsurf bindings (Hermes-specific
  Mac integration, low ROI for OC's audience).
- Background sessions output panel — `/background` already exists; richer
  panel rendering belongs to a TUI iteration.

## Acceptance criteria

- ☐ All 21 items pass unit tests.
- ☐ `oc chat` paste of 100-line block shows compact preview; on Enter the
  full text reaches the agent.
- ☐ `oc chat -c "name"` resumes the most recent `name` or `name #N` session.
- ☐ `oc sessions stats` prints counts and DB size.
- ☐ `/busy steer` interrupts mid-turn at next tool call boundary.
- ☐ `/rollback 2` restores the 2nd-most-recent checkpoint.
- ☐ Light terminal forced via `OPENCOMPUTER_TUI_THEME=light` swaps theme.
- ☐ Quick command `/status` from REPL runs `systemctl status oc-agent`
  with a 30 s timeout.
- ☐ `Ctrl+Z` in REPL suspends process (Unix), `fg` resumes cleanly.
- ☐ Per-prompt clock shows live `⏱` then frozen `⏲` after each turn.
- ☐ Full pytest + ruff + Vitest green; CI green clean (no admin bypass).

## Migration

- `/queue-mode` users keep working with deprecation warning.
- `oc session …` keeps working alongside new `oc sessions …` plural.
- New config keys (`display.busy_indicator.style`, `display.markdown_strip`)
  default to current behaviour, no-op for existing installs.
