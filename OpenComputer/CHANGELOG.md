# Changelog

All notable changes to OpenComputer are listed here. Follows [Keep a Changelog](https://keepachangelog.com/) conventions. **Versioning: date-stamped (`YYYY.M.D`)** — ship-when-ready, no semver theatre. The `plugin_sdk/` contract is the only stability surface.

## [Unreleased]

### Removed (OpenCLI residue cleanup)

Follow-up to the 2026-04-25 OpenCLI scraper plugin removal (commit
`cae1a58`). The plugin's source was deleted then but six docstring
mentions and an empty `extensions/opencli-scraper/__pycache__/` tree
remained, misleading new contributors into thinking F6 wiring was
still pending. This change finishes the sweep.

- Removed `extensions/opencli-scraper/` empty directory tree (only
  `__pycache__/` files remained from the prior deletion; no source).
- `opencomputer/ingestion/bus.py` — replaced "Phases 4/5 (F6 OpenCLI
  scraper, F7 OI bridge) publish here" with "plugin publishers (web
  fetchers, file watchers, OI bridge tools) emit here".
- `opencomputer/security/__init__.py` — replaced "Future F6 (OpenCLI
  scraper) and F7 (OI bridge) plugins will pipe..." paragraph with
  current statement that WebFetch + the coding-harness OI bridge are
  the actual consumers.
- `opencomputer/security/sanitize.py` — removed the obsolete
  "Future F6 / F7 wiring" docstring section entirely.
- `opencomputer/security/instruction_detector.py` — replaced "web
  pages via the F6 OpenCLI scraper" example with "web pages via
  WebFetch".
- `plugin_sdk/ingestion.py` — replaced `"opencli_scraper"` in the
  source-id convention example with `"web_fetch"`; updated
  `WebObservationEvent` docstring from "Phases 4/5 (F6 OpenCLI
  scraper, F7 OI web tools)" to "Web fetchers (e.g. WebFetch tool,
  OI bridge)".

No functional or behavioral change. Docstrings + dead-directory cleanup
only. CI: 80 tests in the affected modules still pass.

### Fixed (Gateway — friendly user-facing errors on dispatch failures)

When the agent loop raised inside `Dispatch.handle_message` (e.g. an
upstream LLM 504, a 429 rate-limit, an auth failure, or a network
hiccup), the gateway returned the literal
`[error: InternalServerError: Error code: 504 - {'error': {...}}]`
to the channel — leaking SDK class names + raw exception args at the
user. Telegram users in particular saw what looked like the bot
"silently dying" because the message looked like internal noise.

- `opencomputer/gateway/dispatch.py` — new `_format_user_facing_error`
  helper maps exceptions to one-liners keyed off `status_code` (5xx →
  "model service returned an error (504), try again in a moment", 429 →
  rate-limit message, 401/403 → auth message) and class name
  (`APIConnectionError` / `*Timeout` → network-issue message). Full
  traceback is still logged via `logger.exception` so debugging isn't
  weakened.
- `tests/test_dispatch_friendly_errors.py` (new, 9 tests) covers the
  504 repro, the 5xx batch, 429, 401/403, network/timeout names, and
  the unknown-exception fallback that retains the class name without
  leaking the raw repr.

### Added (Grok-style terminal chat experience — Round 5)

The `opencomputer chat` terminal got the four upgrades that make Grok's
CLI feel responsive: spinner, live markdown, tool-call status panel,
and a thinking block above the answer. Falls back to the prior plain-
stream path on non-TTY (so `echo … | opencomputer chat` still produces
clean piped output).

- `opencomputer/cli_ui/streaming.py` (new, ~250 LOC) —
  `StreamingRenderer` wraps Rich's `Live` with: spinner before first
  token (`◐ Thinking…`), live markdown re-render at 4 fps with
  syntax-highlighted code blocks, tool-call status panel showing the
  last 3 dispatches with ✓/✗ + elapsed time, post-hoc thinking panel
  rendering `ProviderResponse.reasoning`, and a token-rate footer
  (`98 tok/s`).
- `opencomputer/cli.py::chat()` — `_run_turn` now uses the renderer on
  TTY; `_run_turn_plain` retained for piped stdin. New
  `_wire_streaming_renderer_hooks()` registers a single `PRE_TOOL_USE`
  HookSpec + a `tool_call` bus subscription (one-time per process).
  Both check `current_renderer()` so they no-op on non-TTY runs.
- 16 new tests in `tests/test_streaming_renderer.py`: enter/exit
  sentinel state, buffer accumulation, thinking panel emit/skip,
  token-rate footer, tool panel start/end, last-3 row eviction,
  concurrent same-name calls get distinct rows, mid-stream code-fence
  defence, args-preview truncation + newline strip, duration
  formatting, zero-elapsed safety, late-completion idempotency.

Live streaming of THINKING chunks is deferred — Anthropic's SDK
exposes them separately from assistant chunks; for v1 we render the
thinking panel post-hoc from the persisted `reasoning` field.
### Added (Layered Awareness V2.C — Life-Event Detector + Plural Personas, 2026-04-27)

- **Life-Event Detector framework** — `LifeEventPattern` ABC + sliding-window
  evidence accumulator with exponential decay (default 7-day half-life,
  14-day window). Patterns subscribe to F2 SignalEvent bus; firings either
  surface as chat hints (`surfacing="hint"`) or stay silent F4 graph edges
  (`surfacing="silent"` for HealthEvent / RelationshipShift — never auto-surface).
- **6 starter patterns** — JobChange, ExamPrep, Burnout, RelationshipShift,
  HealthEvent, Travel. Surface threshold 0.7 default.
- **Pattern registry + bus subscription** — `LifeEventRegistry` owns the
  pattern instances + firing queue; `subscribe_to_bus()` wires it to the
  default TypedEventBus. Per-pattern exception isolation.
- **`opencomputer awareness patterns {list,mute,unmute}`** CLI + persistent
  mute state at `<profile_home>/awareness/muted_patterns.json`.
- **`opencomputer awareness personas list`** CLI.
- **Persona auto-classifier** — `classify(ClassificationContext)` reads
  foreground app, time of day, recent files, last messages → returns
  `ClassificationResult(persona_id, confidence, reason)`. Heuristic-based
  in V2.C; V2.D may swap to LLM.
- **5 default personas** — coding, trading, relaxed, admin, learning.
  YAML at `opencomputer/awareness/personas/defaults/*.yaml`. User overrides
  via `<profile_home>/personas/*.yaml`.
- **Persona overlay wired into AgentLoop** — at session start, classifier
  runs, persona's `system_prompt_overlay` lands as `{{ persona_overlay }}`
  slot in base.j2 (between user_facts and skills).
- **Foreground-app detector** — best-effort macOS osascript probe; falls
  back to "" on non-macOS.
- **F1 capability claims** — `awareness.life_event.observe`,
  `awareness.life_event.surface`, `awareness.persona.classify`,
  `awareness.persona.switch` (all IMPLICIT).

V2.D (Curious Companion — agent asks indirect questions to fill knowledge
gaps) is the natural next plan.

Spec + plan: `OpenComputer/docs/superpowers/plans/2026-04-27-layered-awareness-v2c.md`

## [2026.4.27] — Round 4 ship: undeferred items, all 5 landed

User reviewed the deferral list and pushed back on 5 items they
wanted shipped. All 5 in this release plus archit-2's Layered
Awareness V2.B Background Deepening (#155):

- #156 Memory dreaming via `opencomputer cron` (item 4)
- #157 MCP catalog 5→20 entries + `catalog` synonym (item 2)
- #158 Per-profile credential isolation (item 5)
- #159 LLM-mediated recall synthesis — Hermes pattern, not vectors (item 1)
- #160 Telegram webhook mode (item 3)
- #155 Layered Awareness V2.B (archit-2)

Plan + audit lived at `~/.claude/plans/replicated-purring-dewdrop.md`.
The most-counterintuitive finding from investigation: cron infra was
already merged so item 4 was hours, not days; vector search was the
WRONG answer for "better than keyword" — Hermes pattern (FTS5 + LLM
synthesis) is.

### Added (Telegram webhook mode — Round 4 Item 3)

Polling stays the default; webhook is opt-in via config. User asked
for this once they could run a tunnel (ngrok/cloudflared) on Mac
or deploy to a VPS, so the HTTPS-endpoint blocker no longer
disqualifies it. Webhook mode is strictly better than polling — saves
bandwidth, no 30-second poll latency, no dropped updates on
connection blips.

- `extensions/telegram/webhook_helper.py` (new) — wraps Telegram's
  setWebhook / deleteWebhook / getWebhookInfo APIs, plus an
  `aiohttp`-based receiver server with constant-time secret-token
  verification (X-Telegram-Bot-Api-Secret-Token header). Tunnel
  detection helpers: `detect_ngrok_url()` probes ngrok's
  `127.0.0.1:4040/api/tunnels`, `detect_cloudflared_running()` checks
  for the cloudflared process via pgrep.
- `extensions/telegram/adapter.py::TelegramAdapter` — new config
  fields (`mode`, `webhook_url`, `webhook_port`, `webhook_secret`).
  `connect()` branches on `mode == "webhook"` to call new
  `_start_webhook_mode()` instead of starting the polling loop;
  `disconnect()` cleans up the aiohttp server AND deregisters at
  Telegram (so polling can resume cleanly).
- 11 new tests in `tests/test_telegram_webhook.py`: secret-token
  generation (Telegram-compatible chars + uniqueness), setWebhook
  payload shape, setWebhook error path, deleteWebhook handles
  network failure, secret-header verification (match / mismatch /
  missing), ngrok-URL detection (https tunnel + connection refused),
  cloudflared detection handles missing pgrep.

We deliberately use raw aiohttp (already in pyproject deps) instead
of porting hermes' `python-telegram-bot.start_webhook()` — keeps the
dep surface unchanged + matches OC's existing httpx-only HTTP posture.

Why no CLI surface yet (deferred to follow-up):
- The webhook receiver works end-to-end via config alone
- A `opencomputer telegram tunnel detect` command would be ~30 LOC
  but adds a typer subapp; ship the core first, polish in next PR
- Live tunnel-integration testing requires running ngrok during CI;
  defer until needed

### Added (LLM-mediated recall synthesis — Round 4 Item 1)

User asked: "think of something better than strict keyword pattern
matching." Hermes (`tools/session_search_tool.py`) proves the
pattern: keep FTS5 retrieval (cheap), but post-process the top
candidates through a cheap LLM that synthesises a focused answer
with citations. We port that pattern using `claude-haiku-4-5`.

- `opencomputer/agent/recall_synthesizer.py` (new) — given a query +
  list of FTS5 candidates, calls a cheap-model LLM to produce a 1-3
  sentence answer with bracket-style citations (`[N]`) anchored to
  the candidate indexes. Citation guard rejects out-of-range
  references so the synthesizer can't hallucinate non-existent
  sessions. Skips silently when: <3 candidates (raw is short
  enough), `OPENCOMPUTER_RECALL_SYNTHESIS=0`, `synthesize=False`
  arg, or any LLM failure (network, auth, etc.). Returns `None` on
  any skip/failure path so callers fall back to raw FTS5 — synthesis
  is never substitutive, only additive.
- `opencomputer/tools/recall.py::_do_search` — after gathering FTS5
  hits, calls `_maybe_synthesize()` and prepends a `## Synthesis`
  section before the existing `## Episodic` / `## Messages` blocks
  when synthesis succeeds. Per-call opt-out via `synthesize: false`
  argument.
- 12 new tests in `tests/test_recall_synthesis.py`: skip-when-too-few-
  candidates, skip-when-explicit-false, skip-on-env-opt-out, happy
  path with citation, citation guard rejects out-of-range, accepts
  no-citation answers ("no matching memory found"), provider raises
  → return None, empty response → return None, citation guard unit
  tests (4 cases).

Why this is the right "10x better" answer (per the audit + plan):
- Hermes proves the pattern is meaningfully better than raw FTS5
  for open-ended recall ("when did I ask about X?").
- Zero new dependencies — no 400 MB sentence-transformers download.
- Inherits the user's existing provider config (Claude Router proxy,
  Anthropic-compatible endpoints, etc.) — no new setup.
- Composable upgrade path: if 2 weeks of dogfood show FTS5
  retrieval is the actual bottleneck, ADD a vector layer next.
  The synthesis layer doesn't change.

### Added (per-profile credential isolation — Phase 14.F / Round 4 Item 5)

Closes the gap where credentials lived only in the global
`~/.opencomputer/.env`, so two profiles (e.g. `work` and `personal`)
shared the same `ANTHROPIC_API_KEY`. Now per-profile `.env` files
take precedence, with global as fallback for backwards compat.

- `opencomputer/security/env_loader.py` — new `load_for_profile(name)`
  helper. Resolution: profile-local `<oc_home>/profiles/<name>/.env`
  → global `<oc_home>/.env`. Existing shell-set vars always win
  (dotenv convention). Errors during load are swallowed at debug —
  startup never crashes on a malformed .env.
- `opencomputer/cli.py::main()` — wires `load_for_profile()` into
  the startup sequence right after `_apply_profile_override()`. Users
  no longer need to manually `source` their .env before every
  `opencomputer` invocation.
- 8 new tests in `tests/test_per_profile_env.py`: global-only loads
  (default profile path), profile-local overrides global, profile-local
  falls back to global for unset keys, default profile skips
  `profiles/default/.env` (the root IS default), shell-set vars
  beat file-loaded ones, brand-new install (no .env files) is empty
  not a crash, loose-perm .env doesn't crash startup (env_loader
  fail-closed but caller swallows).

Backwards compat: existing global `~/.opencomputer/.env` continues
to work for users with a single profile. Migration is opportunistic —
the day a user creates a second profile and writes
`~/.opencomputer/profiles/work/.env`, that file's values shadow
global for the `work` profile only.

### Added (MCP catalog expansion: 5 → 20 entries + `mcp catalog` synonym)

Round 4 Item 2. The bundled `mcp/presets.py` shipped 5 entries; we
extend to 20 covering the most-requested MCPs.

- `opencomputer/mcp/presets.py` — 15 new presets:
  **Official MCP servers:** `sqlite`, `gitlab`, `google-drive`, `slack`,
  `memory` (knowledge graph), `puppeteer`, `sequential-thinking`,
  `time`, `everart`.
  **Community / third-party:** `notion`, `linear`, `sentry`, `context7`,
  `perplexity`, `docker`.
  Each preset declares `slug`, `description`, runtime config (npx /
  uvx), `required_env`, and `homepage` for user verification.
- `opencomputer/cli_mcp.py` — new `opencomputer mcp catalog` command
  as a friendlier-named synonym for the existing `mcp presets`. Both
  print the same listing; `presets` retained for backwards compat.
- 9 new tests in `tests/test_mcp_catalog_expansion.py`: ≥15 entries
  bundled, major third-party servers (notion/linear/sentry/sqlite/
  gitlab/context7) present, every preset has a homepage URL, every
  preset has a description ≥20 chars, secret-requiring presets explain
  where to get the credential, both `catalog` and `presets` commands
  list the same entries, no duplicate slugs.
- 1 existing test updated (`test_five_presets_bundled` → `test_original_
  five_presets_still_present`) — relaxes equality to subset check so
  the original 5 are guaranteed present + count ≥15. Backwards-compat
  guard for any third-party scripts pinning to the original 5 slugs.

### Changed (`memory dream-on` registers a cron job; `dream-off` removes it)

Round 4 Item 4. Closes the gap where `dream-on --interval daily` only
flipped a config flag and printed "set up cron yourself" — most users
would never wire it up. Now it uses the cron infra
(`opencomputer/cron/`, already merged) automatically.

- `opencomputer/cli_memory.py::memory_dream_on()` — after the existing
  config flip, calls `cron.jobs.create_job()` with `name="memory-dreaming"`
  and schedule `0 3 * * *` (daily) or `0 * * * *` (hourly). On
  `--interval` change, removes the previous job first via
  `_remove_existing_dream_cron_job()` so re-runs replace cleanly
  rather than accumulating duplicates.
- `opencomputer/cli_memory.py::memory_dream_off()` — mirrors the
  cleanup. Idempotent; safe to call when no job exists.
- 8 new tests in `tests/test_dream_on_creates_cron_job.py`: name +
  schedule per interval, hourly variant, replace-on-interval-change,
  idempotent on repeat, dream-off cleanup, dream-off no-op, doesn't
  touch unrelated cron jobs, invalid interval doesn't leave half state.

User must still run `opencomputer cron daemon` (or use the LaunchAgent
from PR #153) for the schedule to actually fire — we don't start the
daemon for the user. The `dream-on` output names this requirement.

### Added (Coding Harness Parity V3.A — 2026-04-27)

OpenComputer's coding harness now matches Claude Code's quality across
seven engineered surfaces. A user choosing `oc code` should not need to
fall back to `claude` for any common workflow.

- **Benchmark suite** (T0) — 5 canonical tasks (refactor / add test / fix
  type error / write script / debug failure) wired through AgentLoop.
  Records `tool_calls + iterations + elapsed + success`. Opt-in via
  `pytest -m benchmark`. Establishes the quality yardstick.
- **`PythonExec` tool** (T1) — sandboxed Python via `asyncio.create_subprocess_exec`.
  Denylist (`os.system`, `subprocess`, `eval`, `exec`, `__import__`,
  `/.ssh/`, etc.) blocks obvious abuse pre-spawn. Closes the OI principle
  gap: ad-hoc data analysis (pandas, sklearn) without `bash python3 -c`
  ceremony.
- **`profile-scraper` skill** (T2) — structured laptop knowledge ingestion
  with 12 source functions (identity, projects, browser history,
  shell history, git activity, recent files, app inventory, system info,
  secrets audit, git emails, package managers). `{field, value, source,
  confidence, timestamp}` schema. Denylist for `~/.ssh`, Messages.app,
  financial PDFs. 10-snapshot retention. `secrets_audit` returns
  `{file, count}` payloads — never the matched token value.
- **Engineered `base.j2`** (T3) — system prompt grew from 47 lines to
  ~250 lines / ~14k chars. New sections: working rules, tool-use
  discipline, plan/yolo modes, memory integration, error recovery,
  workspace context, doing-tasks loop, refusal policy.
  PromptContext extended with `os_name`, `workspace_context`,
  `plan_mode`, `yolo_mode` (safe defaults preserve existing callers).
- **Tool description audit** (T4) — every one of 35 registered tools now
  has a description ≥120 chars (median ~595 chars, max 982 for `Edit`).
  Each teaches when to use, when NOT to use, and pitfalls. Destructive
  tools (`Edit`, `MultiEdit`, `Write`, `Bash`, `PythonExec`,
  `AppleScriptRun`) carry warning/guidance text.
- **Engineered `Edit`/`MultiEdit` error messages** (T5) — every error
  return path now nudges toward the fix. "old_string not unique" lists
  the two remediation paths (more context vs `replace_all=true`); "file
  not Read first" enforced via new `_file_read_state.py` tracker (no
  longer just documented; now actually checked). Per-edit batch errors
  in MultiEdit identify which edit failed (`edit #N of M failed`).
- **Diff visualization** (T6) — Edit/MultiEdit success messages now
  include a unified diff (`difflib.unified_diff`, `n=3` context, capped
  at 500 lines via `MAX_DIFF_LINES`). Closes the model's self-verify
  loop without re-Reading.
- **`oc code [path]`** command (T7) — snappy entry-point matching
  `claude` ergonomics. `oc` shorthand was already present in
  `[project.scripts]`. Mirrors `chat` semantics with the new helper
  `_run_chat_session` to keep both DRY.
- **Workspace context loader** (T8) — `load_workspace_context()` walks
  up to 5 ancestor directories from cwd looking for `OPENCOMPUTER.md`,
  `CLAUDE.md`, `AGENTS.md`. All three are loaded if present. Per-file
  100KB cap with truncation marker. Wired at session-start in
  `agent/loop.py` so the result lands on the FROZEN base prompt
  (prefix-cache safe).
- **`NotebookEdit` smoke** (T9) — 16 fixture-based tests against real
  `.ipynb` v4.5 format. No bugs found in the existing 192-LOC tool;
  schema documented (`path`, `mode`, `cell_index`, `cell_type`, `source`).
- **`/scrape` slash command** (T10) — built-in registry alongside
  plugin-authored commands. `/scrape`, `/scrape --full`, `/scrape --diff`
  (compares the two most recent snapshots).

V3.B follow-ups parked: streaming PythonExec output mid-execution; LSP
integration for `oc code`; continuous benchmark CI integration; empirical
cross-comparison harness with Claude Code.

Spec + plan: `OpenComputer/docs/superpowers/plans/2026-04-27-coding-harness-parity-v3a.md`

## [2026.4.26.post3] — vision-completion ship + release-CI typo fix

post2 was tagged but never published — the new wheel-smoke guard from
PR #148 caught its own typo (used short-form plugin ids `anthropic`
when the real ids are `anthropic-provider`). Working as intended:
the guard fired and stopped a release before publish. Fixed in
release.yml; this `post3` is the actual ship.

## [2026.4.26.post2] — vision-completion ship: Honcho default-on, agent auto-knows-user, launchd, --resume picker

Five PRs in a single session covering the user's explicit vision
("the chat llm should know about the user before the user even
starts using it" + "honcho should be on by default") plus the
operational polish that makes OC actually usable as a daily tool.
Includes: #150 (Honcho default-on with daemon auto-start), #152
(auto-bootstrap profile on first chat), #149 (Telegram scoped lock
+ 409 retry, shipped in this release window), #153 (macOS LaunchAgent
for the gateway daemon), #154 (chat resume picker), #148 (release.yml
wheel-smoke step). Plus archit-2's #151 (Layered Awareness V2.A
follow-ups: consent enforcement, calendar permission handling,
multi-browser support).

### Added (`opencomputer chat --resume {last,pick}` magic spellings)

CLAUDE.md §5 Phase 15.A — checkpoint table shipped, CLI surface was
missing. Without this, `--resume` only worked when the user had
copied a UUID from `opencomputer sessions`. Now:

- `opencomputer chat --resume last` — resumes the most-recent
  session by `started_at`. Falls back to a fresh session (with a
  dim notice) when there are none yet.
- `opencomputer chat --resume pick` — interactive picker showing the
  last 10 sessions (id prefix, platform, message count, title);
  prompt accepts a number or blank-for-fresh.
- Existing `--resume <session-id>` keeps working unchanged.

New `opencomputer.cli._resolve_resume_target(spec)` helper handles
both magic spellings and is unit-tested. 6 new tests in
`tests/test_chat_resume_picker.py`.

### Added (macOS LaunchAgent for `opencomputer gateway`)

Standard macOS pattern for "always-on" services. CLAUDE.md user-prefs
notes Telegram is the user's primary surface; a laptop reboot
shouldn't kill the gateway.

- `scripts/launchd/com.opencomputer.gateway.plist.template` (new) —
  LaunchAgent plist with `RunAtLoad=true`, `KeepAlive=true`,
  `ThrottleInterval=60` (no busy-restart loops), explicit
  `EnvironmentVariables.PATH` (LaunchAgent inherits a sparse PATH),
  `ProcessType=Interactive` (defeats App Nap when foregrounded
  elsewhere), and log paths under `~/.opencomputer/logs/`.
- `scripts/launchd/install.sh` (new) — substitutes the absolute
  `opencomputer` path resolved at install time (LaunchAgent's PATH
  can't find it from the bare name), writes the plist, runs
  `launchctl unload ; launchctl load`, verifies the job is listed.
  Idempotent. Refuses with a clear error on non-macOS.
- `scripts/launchd/uninstall.sh` (new) — `launchctl unload` + delete
  the plist. Idempotent.
- `scripts/launchd/README.md` — install / verify / uninstall docs +
  behavioural notes (sparse PATH, env vars not from dotfiles, etc.).
- 9 new tests in `tests/test_launchd_plist.py`: template XML validity,
  load-bearing keys present (KeepAlive / RunAtLoad / ThrottleInterval),
  install.sh syntax + `set -euo pipefail` + macOS guard + binary
  resolution, dry-run renders both placeholders.

Linux/VPS users keep using `docker compose up -d` against the bundled
`docker-compose.yml`; systemd unit files land in a future PR.

### Added (auto-trigger profile bootstrap on first chat — user vision)

User said verbatim this session: "the chat llm should know about the
user before the user even starts using it." PR #143 shipped the
`profile_bootstrap.orchestrator` (identity scan + git history scan +
calendar + browser history) but as a manual `opencomputer profile
bootstrap` invocation. Most users would never discover it, so on
first chat the agent had no identity facts in context.

- `opencomputer/profile_bootstrap/auto_trigger.py` (new) — small
  policy module + background-thread runner. `should_auto_bootstrap()`
  returns False when: marker file exists (already done), stdin not
  TTY (CI / piped), or `OPENCOMPUTER_NO_AUTO_BOOTSTRAP=1` is set.
  `kick_off_in_background()` runs the orchestrator in a daemon thread
  with quick-mode args (identity + git only — no calendar / browser
  since those need entitlements on macOS Sequoia and can be slow on
  power users with thousands of history entries).
- `opencomputer/cli.py::chat()` — calls `kick_off_in_background()`
  right after the update-check prefetch. When it fires, prints a
  one-line dim notice ("Building your profile in background — won't
  interrupt this session") so the user knows what's happening but
  doesn't wait. Bootstrap result lands in `user_model/graph.sqlite`
  for the NEXT prompt-builder pass to pull from.
- 8 new tests in `tests/test_profile_bootstrap_auto_trigger.py`:
  policy yes-on-first-run / skip-on-marker / skip-on-non-TTY /
  skip-on-opt-out, kick-off returns None vs Thread per policy,
  errors swallowed silently, marker path matches cli_profile's
  single-source-of-truth location.

### Changed (Honcho on by default — auto-start Docker daemon)

Pinned to this session's incident: a user installed Docker Desktop
but never opened the app; the wizard's `_optional_honcho()` saw the
docker binary, called `bootstrap.ensure_started`, which timed out at
120s trying to reach a dead socket, and the user got silently
downgraded to baseline memory with no clear path forward.

- `extensions/memory-honcho/bootstrap.py` — new `is_docker_daemon_running()`
  (cheap `docker info --format {{.ID}}` probe), `try_start_docker_daemon()`
  (`open -a Docker` on macOS; returns False elsewhere), and
  `wait_for_docker_daemon(timeout_s=60.0)` (polling helper). Bootstrap
  now distinguishes "binary missing" from "binary present, daemon down".
- `extensions/memory-honcho/bootstrap.py::_compose()` — `timeout` is
  now a kwarg (default 120s for cheap ops); `honcho_up()` passes
  `timeout=300` so first-pull on a cold Docker (postgres + redis +
  api ≈ 600 MB) doesn't trip the original 120s ceiling.
- `opencomputer/setup_wizard.py::_optional_honcho()` — when daemon is
  dead and platform is macOS, calls `try_start_docker_daemon()` then
  `wait_for_docker_daemon()` before proceeding to compose-up. On
  Linux/Termux (where we can't sudo systemctl on the user's behalf),
  prints a clear `sudo systemctl start docker` hint and downgrades
  cleanly. Wizard's compose timeout bumped 60s → 180s for the same
  first-pull tolerance reasoning.
- 7 new tests in `tests/test_honcho_default_on.py` cover: daemon-dead
  → start-attempt path; 180s timeout passed to ensure_started; graceful
  downgrade when daemon won't come up; Linux fallback message; macOS
  open-fails fallback; legacy bootstrap module without daemon helpers
  (defence-in-depth); and source-text assertion that 300s is wired
  into honcho_up.

### Added (Telegram token-conflict prevention — hermes parity)

Direct port of hermes' scoped-lock pattern at
`sources/hermes-agent-2026.4.23/gateway/status.py:464`. Pinned to the
v2026.4.26 Telegram E2E incident: Claude Code's Telegram channel
adapter (PID 45409) was already polling `@Terraform_368Bot`, OC's
gateway tried the same bot, Telegram serves long-poll updates to
whoever asked first, and OC silently saw zero traffic with no log
indication that anything was wrong.

- `opencomputer/security/scope_lock.py` (new) — machine-local scoped
  locks. `acquire_scoped_lock(scope, identity, metadata=None)` returns
  `(True, prior)` on success or `(False, holding_record)` on conflict
  so the caller can name the holding PID. Stale-lock detection covers
  three layers: corrupt JSON, dead PID (`os.kill(pid, 0)`), and
  PID-recycled (Linux `/proc/<pid>/stat` start_time mismatch). Lock
  files live at `~/.opencomputer/locks/<scope>-<sha256(identity)>.lock`
  (override via `OPENCOMPUTER_LOCK_DIR`); the identity is hashed so
  the bot token never appears on disk in plaintext.
- `extensions/telegram/adapter.py` — `connect()` now acquires
  `("telegram-bot-token", token)` BEFORE opening any HTTP. On
  conflict: clear error log naming the holding PID and the lock-file
  path, then returns False. `disconnect()` releases. Failed-getMe
  releases too so a retry isn't refused by our own stale lock.
- `extensions/telegram/adapter.py` — `_poll_forever` now detects HTTP
  409 Conflict from `getUpdates` (cross-machine duplicate that the
  local lock can't catch) with explicit logging + exponential
  back-off (2s → 4s → 8s → 16s → 30s cap). Mirrors openclaw CHANGELOG
  #69873 ("rebuild polling HTTP transport after getUpdates 409
  conflicts"). Reset to 0 on first successful poll.

13 new tests across `tests/test_scope_lock.py` (9: stale-detection,
re-acquire, plaintext-leak guard, idempotent release) and
`tests/test_telegram_token_lock.py` (4: connect happy path, refusal
with holding PID in log, disconnect-releases, failed-getMe-releases).

### Changed (release CI hardening)

- `.github/workflows/release.yml` — added a wheel-smoke step that runs
  AFTER build but BEFORE publish. Installs the freshly built wheel
  into a clean venv, runs `opencomputer --version` (asserts version ==
  tag), then walks `discover(standard_search_paths())` and asserts the
  required bundled plugins (`anthropic`, `openai`, `telegram`) are
  discoverable. Pinned to the v2026.4.26 incident, where the wheel
  imported fine but had no `extensions/` tree and was unusable in
  practice. The previous `import opencomputer` check could not catch
  this; the new step does, and fails the release before publish.

## [2026.4.26.post1] — hotfix: bundle `extensions/` in the wheel

Critical hotfix on the same-day v2026.4.26 release. The wheel only
shipped `opencomputer/` and `plugin_sdk/`, omitting the `extensions/`
plugin tree — so `pip install opencomputer==2026.4.26` produced an
unusable agent that errored with "Provider 'anthropic' is not
available" on first chat. Caught by the post-release E2E install test.

Fix: added `[tool.hatch.build.targets.wheel.force-include]` mapping
`"extensions" = "extensions"` to `pyproject.toml`. The wheel now ships
all 21 bundled plugins (anthropic-provider, openai-provider,
aws-bedrock-provider, telegram, discord, slack, matrix, mattermost,
imessage, signal, whatsapp, webhook, homeassistant, email,
api-server, coding-harness, dev-tools, memory-honcho, oi-capability,
opencli-scraper, weather-example).

Verified end-to-end: clean venv + `pip install opencomputer==
2026.4.26.post1` → `opencomputer chat` round-trip through Claude
Router returns a real LLM response.

`v2026.4.26` should be yanked on PyPI.

## [2026.4.26] — first date-versioned release; v1.0-quality UX + hermes parity

This is OpenComputer's first ship under the new date-versioned cadence.
Headline: complete hermes-agent parity for first-run UX, deployment
modes, and CLI quality-of-life — plus the entire Round 2A/2B import
batch (security hardening, MCP OAuth PKCE, centralized logging,
forked-context delegation, OSV malware scanning, episodic-memory
dreaming, multi-channel onboarding).

### Changed (release plumbing)

- Switched release cadence from semver (`0.x.y`) to date-stamped tags
  (`YYYY.M.D`). Ship when ready; no minor-bump theatre. The
  `plugin_sdk/` surface is still the only stability commitment — any
  breaking change there will be called out explicitly in the
  changelog regardless of date.
- `opencomputer.__version__` now derives from `importlib.metadata` so
  it cannot drift from `pyproject.toml`.
- New helper module `opencomputer.release.version` exposes
  `current_version()`, `parse_date_version()`, `today_version()` for
  tooling that needs to reason about the version string.


### Added (Layered Awareness V2.B — Background Deepening, 2026-04-27)

Layer 3 of the Layered Awareness design ships as a separate orchestrator
that progressively ingests historical data over expanding time windows.

- **Ollama LLM extractor** (`profile_bootstrap/llm_extractor.py`) —
  was deferred from V1 MVP. Subprocess wrapper around `ollama run`
  that turns artifacts into structured `ArtifactExtraction` records
  (topic, people, intent, sentiment, timestamp). Falls back gracefully
  when Ollama isn't installed.
- **Content-addressed raw artifact store**
  (`profile_bootstrap/raw_store.py`) — SHA256 hashing, two-level fanout
  (`<aa>/<bb>/<full-sha>.json`), idempotent writes.
- **BGE-small embedding helper** (`profile_bootstrap/embedding.py`) —
  via sentence-transformers (optional `[deepening]` dep). Module-level
  model cache so first call is slow but subsequent calls are fast.
- **Chroma vector store wrapper** (`profile_bootstrap/vector_store.py`) —
  PersistentClient in sqlite mode, single collection per profile.
  Narrow API: upsert + query → list[VectorMatch].
- **Spotlight FTS via mdfind** (`profile_bootstrap/spotlight.py`) —
  zero-cost FTS surface on macOS; queries the same index Spotlight
  already maintains. Returns SpotlightHit records.
- **psutil-based idle detection** (`profile_bootstrap/idle.py`) —
  CPU<20% AND plugged-in (or no battery sensor) → idle. Fail-safe to
  not-idle when psutil unavailable.
- **Deepening loop** (`profile_bootstrap/deepening.py`) — window
  progression (7d → 30d → 90d → 365d → all-time), cursor persistence
  at `<profile_home>/profile_bootstrap/deepening_cursor.json`,
  per-call advance. Idle-gated unless `--force`.
- **`extract_and_emit_motif` helper** in orchestrator —
  feeds `layered_awareness.artifact_extraction` SignalEvents onto the
  F2 bus for downstream graph importers.
- **`opencomputer profile deepen [--force --max-artifacts N]`** CLI.
- **Doctor checks** for Ollama, sentence-transformers, chromadb.
- **`pyproject.toml [deepening]` extras**: `psutil>=5.9`,
  `chromadb>=0.5`, `sentence-transformers>=3.0`.

V2.C (life-event detector + plural personas) ships separately.

Spec: `docs/superpowers/specs/2026-04-26-layered-awareness-design.md`
Plan: `docs/superpowers/plans/2026-04-27-layered-awareness-v2b-deepening.md`


### Added (Layered Awareness V2.A — V1 follow-ups, 2026-04-26)

Six follow-up fixes from the PR #143 V1 review backlog. Each shipped
as its own commit during V2.A iteration.

- **F1 consent enforcement on Layer 2 readers** (T1). The 6 ingestion.*
  capability claims registered in V1 are now actually enforced —
  revoking via `opencomputer consent revoke ingestion.calendar` skips
  the next bootstrap's calendar read instead of being audit-only.
  Adds `_get_consent_gate()` lazy gate accessor + `_consent_allows()`
  fail-closed wrapper in the orchestrator. Open-by-default fallback
  when no gate is configured (first-run profiles).
- **Active calendar permission request** (T2). When EventKit auth
  status is `NotDetermined` (0), `read_upcoming_events` now calls
  `requestAccessToEntityType_completion_(0, ...)` to actively trigger
  the macOS Privacy & Security dialog. Blocks via `threading.Event`
  with 60s timeout; subsequent runs don't re-prompt (macOS persists).
- **Dotted-directory pruning in `scan_recent_files`** (T3). Rewrote
  the file walker from `rglob("*")` to `os.walk` with in-place
  `dirnames[:]` mutation so `.git/`, `.cache/`, `.npm/`, `.idea/`
  subtrees are pruned at the source rather than walked-then-skipped.
  Major performance + privacy win on real `~/Documents` trees.
- **`bridge.json` chmod 0o600** (T4). Browser-bridge auth token file
  is now owner-readable only — no more world-readable token leak via
  `~/.opencomputer/<profile>/profile_bootstrap/bridge.json`.
  Applies on both fresh creation and `--rotate`.
- **Calendar + browser visit counters in `BootstrapResult`** (T5).
  Added `calendar_events_scanned` + `browser_visits_scanned` fields;
  CLI displays both. Reads that previously discarded their results
  now surface their effort to the user.
- **Multi-Chromium-family browser history** (T6). `read_chrome_history`
  is now `read_all_browser_history` under the hood, walking
  `_CHROMIUM_FAMILY_ROOTS` for Chrome, Brave, Edge, Vivaldi, Arc,
  Chromium across all `Default` + `Profile N` directories. Original
  `read_chrome_history` preserved as a backward-compat alias.

Test count delta: +21 tests (3075 → 3096 passing).

### Added (background PyPI update check — hermes parity)

- `opencomputer/cli_update_check.py` — non-blocking PyPI update check.
  Mirrors hermes' `prefetch_update_check` / `check_for_updates` pair
  at `sources/hermes-agent-2026.4.23/hermes_cli/banner.py:126,267`,
  but adapted for OC's pip-distributed model — hermes does
  `git fetch origin/main` because hermes ships via git clone; OC
  hits `https://pypi.org/pypi/opencomputer/json` and compares
  `info.version` against the running `__version__`. Daemon thread
  fires at chat start; result printed at chat exit so it never
  disturbs the prompt. Cached at `~/.opencomputer/.update_check.json`
  (atomic .tmp + os.replace) for 24h (cache misses + offline are
  silent). Response capped at 1 MB to defend against hostile mirrors.
  Opt out via `OPENCOMPUTER_NO_UPDATE_CHECK=1`. Treats the
  `0.0.0+unknown` placeholder (broken install) as "don't nag" —
  those users have bigger problems than version drift.

### Added (CLI quality-of-life — hermes parity)

Two paper-cut commands ported from hermes-agent's CLI surface.

- `opencomputer config edit` — opens the active profile's config.yaml
  in `$VISUAL` / `$EDITOR` (POSIX convention; falls back to `vi`).
  Uses `shlex.split` so multi-arg values like `code -w` work.
  Refuses with a pointer to `opencomputer setup` when no config exists
  yet — better than dropping the user into an empty buffer they have
  to populate by hand. Mirrors hermes' `hermes config edit`,
  referenced from `sources/hermes-agent-2026.4.23/hermes_cli/setup.py:2207`.
- `opencomputer auth` — focused provider-credential view. Read-only
  summary of every provider env var declared by the active plugin
  manifests, plus the proxy hint (`ANTHROPIC_BASE_URL`). Echoes only
  the last 4 chars for secrets ≥8 chars; shorter values print as
  "(set)". URL values are stripped to `scheme://host` so any
  token-bearing path / query string can't leak. Mirrors hermes'
  `hermes auth status`. Cleaner focused view than `opencomputer
  doctor` when you just want to answer "did I export the right key?".

### Changed (onboarding UX — hermes parity)

Brings OC's first-run experience up to hermes-agent parity for the
gaps that materially affect new users. Hermes patterns ported directly
from `sources/hermes-agent-2026.4.23/hermes_cli/{main,setup}.py`.

- `opencomputer chat` now detects a missing config or unset provider
  env var on first run and offers an inline `Run \`opencomputer setup\`
  now? [Y/n]`, exactly like hermes' `_has_any_provider_configured` /
  first-run prompt at `main.py:1082-1112`. On `Y`/Enter the wizard
  runs and we exit cleanly so the user re-runs `opencomputer` with
  fresh env. Non-TTY stdin short-circuits to a static hint so CI
  pipelines don't hang.
- `opencomputer setup` channel step now shows every entry in the new
  `_CHANNEL_PLATFORMS` registry (Telegram, Discord, Slack, Matrix,
  Mattermost, Signal, iMessage, WhatsApp, Webhook, HomeAssistant,
  Email) with `[configured]` next to the ones whose primary env var
  is already set. Mirrors hermes' `_GATEWAY_PLATFORMS` registry at
  `setup.py:2210-2256`. Input is space-separated channel ids; unknown
  ids are silently dropped so typos don't crash the wizard.
- `opencomputer setup` against an existing config now offers a
  Welcome Back menu (Quick / Full / Exit) — `quick` only re-prompts
  for items that are still missing, `full` reconfigures everything,
  `exit` aborts cleanly. Replaces the destructive `Overwrite? [y/N]`
  Y/N prompt. Mirrors hermes' returning-user menu at
  `setup.py:2982-3018`.
- `opencomputer setup` refuses to run in non-TTY contexts (CI,
  redirected stdin) with a clear stderr error instead of hanging on
  the first prompt. New shared helper `cli._require_tty(command)`
  ported from hermes' `main.py::_require_tty`.

### Added (one-line installer — hermes parity)

- `scripts/install.sh` — POSIX-bash one-line installer. Mirrors hermes'
  `scripts/install.sh` shape but tighter: detects pipx → pip --user →
  managed venv (PEP 668 fallback) automatically, refuses Python <3.13
  with clear per-OS install hints, supports `--dev` (editable mode from
  local clone), `--dry-run` (preview-only), `--no-user` (system-wide),
  `--use-pipx` (force pipx). Usage in README:
  `curl -fsSL https://raw.githubusercontent.com/sakshamzip2-sys/opencomputer/main/scripts/install.sh | bash`.
  Closes the "no one-liner install" deployment-mode gap; `pip install
  opencomputer`, Docker / docker-compose, and PyPI install were
  already covered.

### Added (Round 2B P-16 — security hardening)

Two surfaces tightened against the most common credential-leak +
agent-confusion failure modes.

- `opencomputer/mcp/client.py` — MCP tools that carry `_meta.owner =
  "system"` or `_meta.internal = true` (or the same fields on
  `annotations`) are now filtered out of the agent-visible tool list.
  Default behaviour unchanged: only servers that explicitly opt in
  hide tools. Lets server authors stash admin / introspection tools
  on the same MCP endpoint as the public surface without exposing
  them to the LLM. Helper `_tool_is_internal(tool)` is the single
  filter point so future schemas (extension carriers beyond `_meta`)
  can register here without touching the connect path.
- `opencomputer/security/env_loader.py` — new module. Reads
  `KEY=value` dotenv files with two safety properties enforced by
  default: the leading UTF-8 BOM (a common Windows-editor accident)
  is silently stripped before parsing, and any group/other
  permission bit on the file (`os.stat().st_mode & 0o077`) is
  fail-closed via the typed `LoosePermissionError`. The override
  path runs through a process-wide flag set by the new CLI option
  `--allow-loose-env-perms`, which logs a WARNING with the offending
  mode + a `chmod 600` hint at every load. The CLI handler strips
  the flag from `sys.argv` before Typer dispatch (mirroring the
  existing `-p`/`--profile` early-intercept pattern) so individual
  subcommands need not declare the option.
- `opencomputer/evolution/redaction.py` — five new patterns added
  (`slack_token`, `telegram_token`, `anthropic_key`, `openai_key`,
  `aws_akid`) plus a widened `Bearer` alphabet that catches dotted
  JWT-shaped tokens. `PATTERN_NAMES` grew from five to ten entries;
  the originals stay in their original slots so existing trajectory
  bundles continue to round-trip cleanly. Pattern application order
  was changed: the more-specific shapes (Anthropic / OpenAI / Slack
  / Telegram / AWS) run BEFORE the legacy generic `api_key` rule so
  the precise replacement label wins. Same regex shapes are also
  applied at log-format time by the P-4 logging redactor — now
  factored consistently across both surfaces.

Tests: `tests/test_security_p16.py` adds 24 cases covering BOM strip,
loose-perm refusal (mode 0644 + 0640), happy-path 0600 load,
explicit + process-wide override warnings, missing-file probe, the
mini parser surface (export / quotes / comments / empty values), CLI
flag interception, six MCP-tool filter scenarios (meta-internal,
meta-owner-system, annotations-extras, plain public, explicit
non-system, missing carriers, end-to-end registry filter), and seven
new redaction patterns (Anthropic-vs-OpenAI label precedence, Slack
bot/personal, Telegram, AWS AKID, dotted JWT bearer, false-positive
prose). Suite: 2931 → 2955 (+24).

### Added (Round 2B P-8 — bg auto-notifications)

Background processes started via `StartProcess` now fire a typed
`Notification` hook the moment they exit, and the coding-harness
registers a default subscriber that surfaces the completion to the
agent on its next turn. Long-running work (`npm run dev`, watchers,
test runners) no longer require the agent to remember to poll
`CheckOutput` — it sees the exit reflected in its context.

- `opencomputer/agent/bg_notify.py` — new module. Defines the
  `BgProcessExit` payload (tool_call_id, exit_code, tail_stdout,
  tail_stderr, duration_seconds) plus the per-session pending-message
  store the agent loop drains between turns. `make_hook_context` /
  `decode_payload` encode the payload onto the otherwise-unused
  `HookContext.message` slot, with a `BG_PROCESS_EXIT_MARKER` so other
  Notification subscribers (Telegram mirroring, audit) can disambiguate
  bg-exit notifications from user-facing alerts and ignore on mismatch.
- `extensions/coding-harness/tools/background.py` — `StartProcessTool`
  now stamps each `_BgEntry` with the originating call id, the active
  session id (via `bg_notify.current_session_id`), and the start
  timestamp, then spawns a `_watch_and_notify` watcher task. The
  watcher awaits `proc.wait()` plus the read-drain tasks, builds the
  payload (last 200 chars of each stream), and fires the Notification
  hook. `_cleanup_all` (SessionEnd) cancels in-flight watchers so a
  dead session never delivers a stale system message.
- `extensions/coding-harness/plugin.py` — registers the bg-notify
  default subscriber alongside the other harness hooks. Defensive
  import — failure disables auto-notifications without breaking
  activation.
- `opencomputer/agent/loop.py` — wires `set_session_id_provider` so the
  `StartProcess` tool can read the active session id. The between-turn
  drain runs on EVERY iteration (not just `_iter > 0`) so a process
  that finishes during the user's typing window is visible to the very
  first model turn.

Tests: `tests/test_bg_auto_notify.py` adds 15 cases covering
(a) clean-exit Notification firing with the expected payload,
(b) error-exit firing with the non-zero exit_code,
(c) the default subscriber appending the formatted system message,
(d) the fire-and-forget contract (a raising peer subscriber must not
prevent other subscribers from firing or the watcher from completing),
plus pending-store mechanics, decode-payload defenses, and the
`tail_chars` helper.
### Added (Round 2B P-4 — centralized rotated logging)

- New `opencomputer/observability/logging_config.py` wires three rotating
  file handlers under `<HOME>/logs/` — `agent.log` (full
  `opencomputer` tree), `gateway.log` (`opencomputer.gateway.*`),
  `errors.log` (`opencomputer.errors`, ERROR-and-above only). 10 MB ×
  5 backups per channel.
- Per-coroutine session context via `contextvars.ContextVar` —
  `set_session_id(...)` is wired from `SessionDB.create_session` and
  the CLI `chat` startup so every log record carries the active
  session id (or `-` when none is bound). Deliberately *not*
  `threading.local`: asyncio coroutines share the loop thread, so a
  thread-local would leak ids across concurrent sessions.
- Secret-redaction at format time covering Bearer tokens, Slack
  (`xoxb-` / `xoxp-`) tokens, Telegram bot tokens, Anthropic
  (`sk-ant-…`) keys, generic OpenAI-style `sk-…` keys, AWS access
  keys (`AKIA…`), and any path under `<home>/.opencomputer/secrets/`.
- CLI `chat` / `wire` / `gateway` subcommands invoke
  `_configure_logging_once()` at startup so the handlers attach exactly
  once per process.
- 14 new tests in `tests/test_logging_config.py` cover ContextVar
  isolation under `asyncio.gather`, every redaction pattern, real
  rotation, and per-channel routing.

### Added (Round 2B P-6 — MCP OAuth 2.1 PKCE flow)

Adds an OAuth 2.1 Authorization-Code-with-PKCE flow for MCP servers, with
every defense in depth turned on. Builds on the G.13 token storage layer
shipped earlier (`OAuthTokenStore.put` / `get`) — the new flow simply
hands its token response to that store.

- **New module `opencomputer/mcp/oauth_pkce.py`** — `run_pkce_flow(...)`
  drives the full dance end-to-end. Designed so callers don't have to
  think about the security primitives; they're encoded into the function:
  - `code_verifier` is `secrets.token_urlsafe(64)` — 256-bit entropy,
    well above RFC 7636's 43-char minimum.
  - `code_challenge` is the S256 derivation
    (`base64.urlsafe_b64encode(sha256(verifier)).rstrip(b"=")`).
  - CSRF `state` is `secrets.token_urlsafe(32)` and validated with
    `secrets.compare_digest` (constant-time, never `==`).
  - Callback server binds **`127.0.0.1` only** — never `0.0.0.0`,
    never `localhost` (which can resolve to IPv6 on some hosts and
    silently break the redirect).
  - Bound to ephemeral port `("127.0.0.1", 0)` — kernel picks; the
    redirect URI is constructed from `server.server_address[1]`.
  - 5-minute default callback timeout (`OAuthFlowTimeout`); shorter
    values honoured for tests.
  - `try/finally` shuts the listening socket down even on exception
    (`server.shutdown() + server.server_close()`).
  - `webbrowser.open()` returning False does NOT crash — the URL is
    printed to stdout for manual paste (works in headless / SSH).
- **New CLI: `opencomputer mcp oauth-login <provider>`** — orchestrates
  the flow, then persists the response via the existing
  `OAuthTokenStore.put(...)`. `--authorization-url`, `--token-url`, and
  `--client-id` are required CLI options because the MCP server-config
  schema does not yet carry OAuth manifest fields; users paste the
  three URLs directly from their provider's docs.
- **New tests `tests/test_mcp_oauth_pkce.py`** — 19 cases. None launch
  a real browser: `webbrowser.open` is monkeypatched and the callback
  is fired by a side thread that GETs the redirect URL. Coverage:
  PKCE primitive correctness (incl. RFC 7636 Appendix B test vector),
  bind to `127.0.0.1`, ephemeral port, happy path, state mismatch
  raises (and the token endpoint is NOT called), timeout raises,
  browser-open failure prints the URL, malformed callback (missing
  `code`) raises, scope omission, extra authorize params, input
  validation, and CLI-level integration with the token store.

### Added (Round 2B P-9 — forked-context subagent delegation)

Optional context fork for the `delegate` tool. When the parent calls
`delegate(..., forked_context=true)` the spawned child loop is seeded
with a snapshot of the parent's recent message history (last 5 by
default) instead of starting from an empty conversation. Lets a
subagent answer follow-on questions that depend on the parent's
context without re-fetching everything via tools.

- `plugin_sdk/runtime_context.py` — new `RuntimeContext.parent_messages`
  field (immutable tuple, defaults to `()`). Carries the parent's
  message snapshot through to `DelegateTool` without bumping any
  existing SDK call site.
- `opencomputer/agent/loop.py` — `AgentLoop` now snapshots its
  in-progress `messages` list onto the runtime immediately before
  dispatching tool calls (i.e. before the assistant message containing
  the `delegate` tool_use is appended, so the snapshot ends at a clean
  turn boundary). Adds a new `initial_messages` kwarg on
  `run_conversation` so a fresh-session child loop can be pre-seeded;
  seeded messages are persisted via `SessionDB.append_messages_batch`
  so resume-from-disk reproduces the same starting state.
- `opencomputer/tools/delegate.py` — schema gains `forked_context:
  boolean` (default `false`). When true, `DelegateTool.execute` reads
  `runtime.parent_messages`, asks
  `CompactionEngine._safe_split_index(messages, 5)` for a safe
  boundary that does NOT split a `tool_use` from its `tool_result`
  (Anthropic 400s otherwise), filters out `system` messages (the
  child has its own), and threads the result through as
  `initial_messages`. The child runtime is also rewritten to
  `parent_messages=()` so a grandchild's own forked-context call sees
  ITS parent's snapshot, not the original grandparent's.

Tests: `tests/test_delegate_forked_context.py` adds 11 cases
covering default-false unchanged, explicit-false unchanged, last-5
non-system seeded happy path, orphan-tool_use boundary safety
(6-message corpus where the naive `messages[-5:]` slice would split
a pair), empty parent history no-op, schema declaration, child
runtime snapshot clear, undersized parent history, and the new
`RuntimeContext.parent_messages` field defaults / accepts. Total
suite: 2906 passing (+11).

### Added (Round 2B P-7 — OSV malware scanning)

Pre-flight vulnerability scan against `OSV.dev` for every stdio MCP
server launched via `npx` or `uvx`. Hits emit a typed
`mcp_security.osv_hit` event on the F2 bus so audit / trajectory
subscribers see every advisory the agent encounters during startup.

- `opencomputer/mcp/osv_check.py` — `check_package(name, ecosystem)`
  with a 24h on-disk cache at `~/.opencomputer/cache/osv.json`
  (cache directory pinned to mode 0700). Network failures are
  fail-open: empty `vulns` + warning, so an OSV outage cannot
  break MCP startup.
- `opencomputer/mcp/client.py` — `MCPConnection.connect()` now runs
  the pre-flight gate before spawning a stdio process. HIGH /
  CRITICAL severity advisories trigger the bus event in both
  warn-and-allow (default) and fail-closed modes; `fail_closed`
  additionally refuses the launch and surfaces the advisory IDs in
  `last_error`.
- `opencomputer/agent/config.py` — `MCPConfig.osv_check_enabled`
  (default `True`) and `MCPConfig.osv_check_fail_closed` (default
  `False`). The chat / gateway / `mcp status` CLIs plumb both flags
  through to `MCPManager.connect_all`.
- New `MCPLaunchBlockedError` exception type (exported from
  `opencomputer.mcp.client`) for downstream callers that wrap the
  connect path and want a typed handle on the block reason.
- New typed event `_OSVSecurityEvent` (discriminator
  `mcp_security.osv_hit`) carries package coords, severity flag,
  advisory IDs, and a `blocked` boolean. Subscribers can `subscribe_pattern("mcp_security.*", ...)`
  for forward-compat with future security signals.

Tests: `tests/test_osv_check.py` adds 27 cases covering clean
package, fail-open + fail-closed vuln paths, network-error fail-open,
TTL fresh + stale cache hits, package extraction across npx/uvx
shapes, severity-detection helpers, and the `osv_check_enabled` short-
circuit. Total suite: 2846 passing.
### Added (Round 2B P-3 — inactivity-based loop timeout)

- New `LoopConfig.inactivity_timeout_s` (default 300s) — wall-clock
  guard that resets on every LLM round-trip and tool dispatch
  (success or failure). Catches hung providers / silent stalls
  without false-positive on long-running tools that periodically
  report progress. Uses `time.monotonic()` so NTP slews can't trip it.
- `LoopConfig.iteration_timeout_s` is now actually enforced (it was
  declared but never checked). Default raised from 600s to 1800s
  to act as an absolute upper bound rather than a routine cap.
- New typed exceptions in `opencomputer.agent.loop`:
  `InactivityTimeout`, `IterationTimeout`, both subclassing
  `LoopTimeout` so callers can catch one base class.
### Added (Round 2A P-18 — episodic-memory dreaming, EXPERIMENTAL)

- **Background dreaming turn** that consolidates recent episodic-memory
  entries into per-cluster summary rows so FTS5 cross-session search
  stays useful as the corpus grows. OFF by default; enabled per-profile
  via `MemoryConfig.dreaming_enabled` (settable through `opencomputer
  memory dream-on`). Uses the cheap auxiliary model when configured;
  cluster heuristic is a no-embedding KISS path (ISO-week bucket + ≥1
  shared file basename or ≥2 shared topic tokens). Idempotent + retries
  once on LLM failure without losing originals.

### Added (Round 2B P-12 — session-list filters)

- **Three new options on `opencomputer session list`**, building on
  G.33's title + preview support without touching display/preview
  rendering:
  - `--label <text>` — case-insensitive substring match against the
    session title. Combine with `--agent` / `--search`.
  - `--agent <profile-name>` — read sessions from a named profile's
    `sessions.db` instead of the active profile. OpenComputer
    profiles are per-directory so "filter by agent" maps to "open
    that profile's DB" via `get_profile_dir(name) / "sessions.db"`.
    Profile-name validation is delegated to `validate_profile_name`
    so the same rules apply that `profiles.py` enforces elsewhere.
  - `--search <text>` — FTS5 query against message text; returns
    sessions whose messages contained matches (deduped: a session
    with N matching messages still only appears once). Reuses the
    existing `SessionDB.search()` path — no parallel SQL builder.
- **FTS5 escaping** lives in `opencomputer/cli_session.py` as
  `_escape_fts5(query)` and is mirrored by a new opt-in
  `SessionDB.search(..., phrase=True)` parameter. User input is
  wrapped as a single FTS5 phrase (`"…"`) so reserved chars (`:`,
  `*`, `(`, `)`, `AND`/`OR`/`NOT`) stay literal instead of being
  parsed as operators. The legacy `SessionDB.search()` default
  (`phrase=False`) is unchanged so existing callers (`mcp/server.py`
  documents the param as "FTS5 syntax", `tools/recall.py`) keep
  working.
- **16 new tests** in `tests/test_cli_session.py`
  (`TestListFilters` + `TestEscapeFts5`) covering: each filter in
  isolation, label case-insensitivity, agent profile-DB switching,
  agent invalid-name rejection, search dedupe, FTS5 special-char
  inputs (`a:b`, `a"b`, `a*b`), no-match returns the empty hint,
  label+search intersection, agent+search composition, and direct
  unit tests for the escape helper.

### Added (Round 2B P-10 — plugin auto-install in setup wizard)

The first-run setup wizard now offers to enable any plugin a user's
chosen channels need, instead of silently writing a config that
references a disabled plugin and then failing at gateway startup.

- New helper `opencomputer.setup_wizard._required_plugins_for_channels`
  maps user-facing channel names → bundled plugin ids via the new
  module-level `_CHANNEL_PLUGIN_MAP` constant. Covers the 11 bundled
  channel plugins (telegram, discord, slack, matrix, mattermost,
  imessage, signal, whatsapp, webhook, homeassistant, email) plus
  the `home-assistant` spelling alias.
- New helper `opencomputer.setup_wizard._auto_enable_plugins_for_channels`
  reads the active profile's `profile.yaml` `plugins.enabled` list,
  diffs it against the channels the user selected, and prompts once
  with a combined `Confirm.ask` to enable everything that's missing.
  On accept, delegates to `cli_plugin.plugin_enable` per id (which
  itself validates against discovered plugins + writes profile.yaml
  atomically). `typer.Exit` from one id (unknown plugin, etc.)
  doesn't short-circuit the rest.
- `_optional_channel` now collects the user's channel selections and
  invokes the auto-enable helper after the channel step. Pure no-op
  when the user skips channels.

**Hard constraint preserved:** the wizard NEVER downloads, fetches,
or pip-installs a plugin. Only plugins already present on disk
(bundled `extensions/` or `~/.opencomputer/plugins/`) can be
enabled — `cli_plugin.plugin_enable`'s discovery validation enforces
this, and a regression test (`TestNoNetworkInstall`) inspects the
helper source for forbidden tokens (`subprocess`, `pip `, `urllib`,
`requests`, `httpx`, `shutil`) so a future "auto-install"
temptation would fail loudly. Maintains the Phase 5.B "no unsigned
skill registry" identity.

Tests: `tests/test_setup_wizard_p10_auto_enable.py` (13 tests).
Cases (a)–(e) from the plan, plus a typer.Exit safety-net case, a
wildcard (`enabled: "*"`) no-op case, and the no-network regression
guard.
### Removed (OI Tier 1 trimmed from 8 to 5 tools, 2026-04-25)

User-directed cleanup. Three Tier 1 tools were redundant with
built-in OC tools and added no unique value:

- **`ReadFileRegionTool`** — duplicated built-in `Read` tool. The
  built-in is line-based (LLM-friendly with line numbers) vs OI's
  byte-based slice; agents almost never need byte-precise reads.
- **`SearchFilesTool`** — used aifs (semantic file search) but
  agents' typical "find file by content" task is covered by `Grep`
  (regex) + `Glob` (pattern). The aifs path requires extra infra
  (embedding model) that wasn't in active use.
- **`ReadGitLogTool`** — code comment confirmed the implementation
  is "INLINE — does NOT use OI subprocess" — literally
  `subprocess.run(['git', 'log', ...])`. `BashTool` does this
  identically.

What stays — 5 macOS-unique tools:
- `screenshot` — display capture
- `extract_screen_text` — OCR via Tesseract
- `read_clipboard_once` — single clipboard read
- `list_app_usage` — running-apps list
- `list_recent_files` — recently-modified files

These are the genuine value OI adds that OC's core lacks.

Cleanup details:
- `extensions/coding-harness/oi_bridge/tools/tier_1_introspection.py`
  — 3 classes removed (~270 LOC), `ALL_TOOLS` + `__all__` updated.
- `extensions/coding-harness/plugin.json` — `tool_names` cut from
  18 to 15 entries; version bumped to 0.4.0.
- `tests/test_coding_harness_oi_tools_tier_1_introspection.py` —
  3 test classes removed, `TestAllToolsList` updated to assert
  `len(ALL_TOOLS) == 5` + new sanity test guarding the macOS-unique
  set.
- `extensions/oi-capability/use_cases/{personal_knowledge_management,
  context_aware_code_suggestions}.py` + their tests — deleted.
  Both depended on the removed Tier 1 tools and were documentation/
  example modules per the original audit, not runtime code.
- `tests/conftest.py` — dropped the `use_cases` sub-package alias
  since the directory is gone; updated docstring to reflect the
  trim.

**Net:** ~600 LOC removed (270 tool code + 250 docs/example +
80 test + conftest). Test count 2727 → 2647 (-80, all expected
from removed surfaces). Zero regressions.

### Removed (OpenCLI scraper plugin removed entirely, 2026-04-25)

User-directed removal after honest re-evaluation of value vs cost.
The plugin's three tools were either redundant or off-target:

- **`ScrapeRawTool`** — bit-for-bit duplicate of the built-in
  `WebFetchTool` (10e, 2026-04-23). No unique value.
- **`FetchProfileTool`** — supports 12 platforms (github, reddit,
  linkedin, twitter, hackernews, stackoverflow, youtube, medium,
  bluesky, arxiv). None match the user's actual workflow (Indian
  stock sites — screener.in, marketsmojo.com, scanx.trade — and
  stock research via the investor-agent / stockflow MCP servers).
  The platform list is calibrated for OSINT-on-developers, not for
  the user's domain.
- **`MonitorPageTool`** — page-change detection. Reproducible with
  `cron` (G.1) + `WebFetch` + a manual diff. Marginal value.

Removed:
- `extensions/opencli-scraper/` — entire plugin directory (~1,580 LOC)
- 12 `tests/test_opencli_*.py` files
- `docs/f6/` — design + source-map docs for the plugin
- Cleaned up incidental references in `opencomputer/security/sanitize.py`,
  `opencomputer/mcp/server.py`, `extensions/coding-harness/plugin.py`,
  `extensions/coding-harness/oi_bridge/tools/__init__.py`,
  `tests/test_instruction_detector.py`, `docs/parallel-sessions.md`.

**Net:** ~2,000 LOC + 12 test files removed. Test count drops from
2727 → 2510 (-217 OpenCLI-specific tests). Zero regressions in
remaining tests.

The `WebFetchTool` (built-in) + `cron` (G.1) + MCP servers cover every
real use case the OpenCLI plugin was supposed to address. Saksham's
"build a profile of myself by scanning laptop + scraping web" vision
is **not implemented by either OI or OpenCLI** — that needs a separate
design built on top of the existing User Model system (F4); to be
discussed separately.
### Added (Sub-project G.34 — FastMCP authoring skill, Tier 4)

- **New bundled skill** at `opencomputer/skills/fastmcp-authoring/` —
  curated path for users who want to author their own MCP servers in
  Python without writing a full OC plugin. Covers the FastMCP
  decorator API (`@server.tool()` / `@server.resource()` /
  `@server.prompt()`), transport choice (stdio / sse / http), and the
  three-phase request lifecycle. Composes with G.30 (`opencomputer
  mcp scaffold`) — the skill points at the scaffolder as the fastest
  path; the scaffolder generates a working skeleton; the skill
  explains the parts.
- **`SKILL.md`** — full authoring guide: when to author an MCP vs an
  OC plugin (different surfaces), the three primitives, transports,
  registration with OC, and common gotchas (stdio servers can't
  print to stdout, tool-name namespace collisions, heavy module
  imports slowing cold-start).
- **`examples/minimal_server.py`** — runnable mini-MCP exposing a
  single `add(a: int, b: int) -> int` tool. Copy-paste starting
  point.
- **`references/transports.md`** — decision matrix (stdio vs http vs
  sse) with cold-start trade-offs + the dual-mode `if "--http" in
  sys.argv` pattern for servers that want to ship one file but run
  two ways.
- **`references/lifecycle.md`** — four-phase walkthrough
  (initialize → list_tools → call_tool → shutdown) with debugging
  tips ("opencomputer mcp test" + tail-stderr loop).
- **12 new tests** in `tests/test_fastmcp_skill_bundle.py` — folder
  layout, frontmatter (name + description + version + trigger
  phrases + scaffolder cross-link), example compiles + uses
  FastMCP + has main entry, references cover three transports +
  four lifecycle phases.

### Added (Sub-project G.33 — `opencomputer session` CLI, Tier 4)

- **New `opencomputer session` command group** — surfaces existing
  `SessionDB` storage with four subcommands:
  - `session list [--limit N]` — table of recent sessions (id,
    started, platform, model, msgs, title) for the active profile.
  - `session show <id> [--head N]` — print session metadata + the
    first N messages as a content preview. Default head=5; pass `0`
    for metadata-only.
  - `session fork <id> [--title T]` — clone a session's messages
    into a fresh UUID. Lets you branch a conversation without
    polluting the source. Default title gets a `(fork)` suffix.
  - `session resume <id>` — print the exact `opencomputer chat
    --resume <id>` command. Doesn't spawn `chat` itself because
    typer-inside-typer has rough edges; the user copy-pastes.
- **Implementation** in `opencomputer/cli_session.py`. Uses
  `SessionDB.create_session` + `append_messages_batch` for atomic
  fork; `_home()` drives the per-profile DB path so each profile
  sees its own sessions.
- **12 new tests** in `tests/test_cli_session.py` covering empty
  profile (shows hint, no error), seeded profile (id renders
  through Rich's table wrapping), limit clamp, show errors
  on unknown id, show with default head + head=0, fork errors on
  unknown id, fork clones session+messages, fork explicit title,
  fork default-title `(fork)` suffix, resume unknown returns error,
  resume prints expected chat-resume command.

### Added (Sub-project G.32 — Model metadata registry, Tier 4)

- **`opencomputer/agent/model_metadata.py`** — small in-memory registry
  answering two questions about any model id without hitting an
  external pricing API:
  - `context_length(model_id)` → max tokens.
  - `cost_per_million(model_id)` → `(input_usd, output_usd)` tuple.
- **`ModelMetadata`** frozen dataclass with `model_id`, optional
  `context_length`, optional `input_usd_per_million`, optional
  `output_usd_per_million`. All numeric fields are nullable so callers
  can distinguish "unknown" from "declared as 0".
- **Curated default catalog** for the models OC users actually run:
  Anthropic (`claude-opus-4-7`, `claude-sonnet-4-6`,
  `claude-haiku-4-5-20251001`) and OpenAI (`gpt-5.4`, `gpt-4o`, `o1`,
  `o3`, `o4-mini`). Numbers reflect the public pricing pages as of
  2026-04.
- **`register_model(meta, *, replace=False)`** lets third-party
  provider plugins teach core about their models from `register(api)`.
  Default `replace=False` preserves the curated catalog so a buggy
  plugin can't silently override known-good entries.
- **Why this lives in core, not the provider plugin:** the cost-guard
  module (G.8) and CompactionEngine want context-length / cost without
  instantiating the provider plugin. Putting the table here keeps
  those callers cheap.
- **13 new tests** in `tests/test_model_metadata.py` — curated entries
  present (Claude family + OpenAI family), unknown returns None, helper
  functions match, register adds new entries, collision without
  `replace` preserves curated, collision with `replace=True` overrides,
  partial cost entries (input only) return `(in, 0.0)`, no-cost entries
  return `None`, list_models sorted + snapshot-immutable, reset clears
  third-party entries.

### Added (Sub-project G.31 — Smart model fallback routing, Tier 4)

- **`ModelConfig.fallback_models: tuple[str, ...] = ()`** — new config
  field. Ordered list of model ids to try on transient errors when the
  primary model fails. Default-empty tuple keeps existing behavior
  (no fallback).
- **`opencomputer/agent/fallback.py`** — new module with two functions:
  - `is_transient_error(exc)` — string-based classifier for HTTP 429,
    5xx, connection refused, connection reset, timeouts. Conservative:
    when in doubt, returns False (don't waste quota retrying an
    unrecoverable error). Mirrors the auth-failure heuristic in
    ``extensions/anthropic-provider/provider.py``.
  - `call_with_fallback(call, primary, chain)` — runs the call against
    each model in turn, stops at the first success. Re-raises the LAST
    error (most recent diagnostic). Non-transient errors short-circuit
    so an auth failure doesn't burn three more attempts.
- **`AgentLoop._run_one_step` (non-streaming path)** wraps the
  `provider.complete()` call in `call_with_fallback`. The streaming
  path is intentionally NOT wrapped — once tokens have flowed to the
  user, the loop is committed to that model. Streaming-mode fallback
  needs a separate buffering design and lands later.
- **No cross-provider fallback** — all models in the chain use the
  same `provider`. Mixing providers mid-turn has subtle implications
  for tool schemas, streaming shape, and prompt-cache identity; we
  keep the failure mode predictable.
- **26 new tests** in `tests/test_model_fallback.py` — `is_transient_error`
  positives (11 markers including "rate_limit", "overloaded", "timed
  out") and negatives (auth errors, "code-429-special-day" false-
  positive guard); `call_with_fallback` happy path / partial failure
  / full chain exhaustion / non-transient short-circuit / empty-chain
  collapse; `ModelConfig` field defaults / tuple acceptance / hashability.

### Added (Sub-project G.30 — `opencomputer mcp scaffold` CLI, Tier 4)

- **New CLI command** `opencomputer mcp scaffold <name> [--dir DIR]
  [--transport stdio|sse|http] [--force]` — generates a runnable
  Python MCP server skeleton at ``<dir>/<name>/`` with three files:
  - ``<pkg>/__init__.py`` + ``<pkg>/server.py`` — FastMCP app with
    one demo tool (``echo``). The package name is the user's name
    lowercased + hyphens-to-underscores.
  - ``pyproject.toml`` — runnable via ``python -m <pkg>.server`` or
    a ``[project.scripts]`` entry. Single dependency: ``mcp>=1.0``.
  - ``README.md`` — quickstart + the exact ``opencomputer mcp add``
    command to register the new server with the active config.
- Validates inputs: name must yield a valid Python identifier (no
  path separators, lowercase letters/digits/hyphens/underscores);
  transport must be one of `stdio|sse|http`; existing directory
  rejected unless `--force`.
- The generated `server.py` is `compile()`-checked in tests so a
  future template edit can't ship broken Python.
- **11 new tests** in `tests/test_mcp_scaffolder.py` — layout (file
  presence, hyphen → underscore package naming), contents (FastMCP
  import, pyproject script entry, README register command,
  transport propagation), validation (bad transport, path-separator
  in name, existing-dir without `--force`, `--force` overwrites
  cleanly), and a compile-check on the generated `server.py`.

### Added (Sub-project G.29 — Home Assistant adapter, Tier 4.x)

- **`extensions/homeassistant/`** — new bundled channel plugin. Talks
  to Home Assistant's REST API at ``POST /api/services/<domain>/<service>``.
  Outbound = service calls (turn lights on, send notifications, run
  scripts/automations). Inbound: webhook adapter (G.3) wired to a Home
  Assistant automation that POSTs events.
- **Mapping note:** the ``send`` verb is overloaded for HA. ``chat_id``
  is parsed as ``<domain>.<service>`` (e.g. ``notify.mobile_app_pixel_8``,
  ``light.turn_on``, ``script.morning_routine``).
  - For ``notify.*`` services, ``text`` becomes the ``message`` field.
  - For other services, callers pass the full payload via
    ``service_data=...`` kwarg.
  - Zero-arg services (e.g. ``script.morning_routine``) send an empty
    body — HA accepts that for trigger-style invocations.
- **Capabilities = 0** — service calls aren't chat messages, so the
  chat-shape flags don't apply.
- **Connect probes `/api/`** to surface bad URL / bad token early with
  a clear log line; failure is non-fatal so a temporarily-down HA
  instance doesn't wedge plugin loading.
- **Setup metadata** (G.25 pattern): ``setup.channels[].id="homeassistant"``,
  env_vars ``["HOMEASSISTANT_URL", "HOMEASSISTANT_TOKEN"]``, signup_url
  pointing at HA's long-lived-token docs.
- **9 new tests** in `tests/test_homeassistant_adapter.py` —
  capability flag = 0, notify packs `message`, non-notify with
  `service_data`, zero-arg service call → empty body, chat_id without
  dot rejected, empty notify message rejected, long notify truncated
  to 4096, non-dict service_data rejected, HTTP 401 surface.

### Added (Sub-project G.28 — API Server adapter via REST endpoint, Tier 4.x)

- **`extensions/api-server/`** — new bundled channel plugin. Exposes
  the agent over plain JSON-over-HTTP for external systems (CI, cron,
  curl) that want to drive the agent without a chat UI.
  - **Endpoint:** ``POST /v1/chat`` with header
    ``Authorization: Bearer <token>`` and JSON body
    ``{session_id?, message}``. Response: ``{session_id, response}``.
  - **Auth:** static Bearer token from ``API_SERVER_TOKEN`` env var.
    REQUIRED — registration is a no-op without it.
  - **Default bind:** ``127.0.0.1:18791``. Set
    ``API_SERVER_HOST=0.0.0.0`` only after understanding the auth
    model + setting a strong token. Safe-by-default posture.
  - **Handler injection:** the adapter exposes ``set_handler(callable)``
    so the host (gateway / custom embed) wires up the per-request
    agent loop. Without a handler bound, requests return 503. Keeps
    the SDK boundary clean — adapter doesn't import from
    ``opencomputer.*``.
  - **Capability flag = `0`** (request/response, not push). The
    inherited ``send()`` method returns a clear "this is a REST
    endpoint, not a push channel" error so callers don't misuse it.
  - **payload limit** = 100 KB at framework level so a misbehaving
    caller can't OOM the process.
- **Setup metadata** (G.25 pattern): ``setup.channels[].id="api-server"``,
  env_vars ``["API_SERVER_HOST", "API_SERVER_PORT", "API_SERVER_TOKEN"]``.
- **9 new tests** in `tests/test_api_server_adapter.py` — capability
  flag = 0, authorized-chat happy path (handler captures session_id +
  message), missing/wrong auth → 401, invalid JSON → 400, empty
  message → 400, no handler bound → 503, handler exception → 500
  with type-name leak, ``send()`` returns clear not-applicable error.

### Added (Sub-project G.27 — Signal adapter via signal-cli, Tier 4.x)

- **`extensions/signal/`** — new bundled channel plugin. Signal outbound
  text + reactions via signal-cli's JSON-RPC HTTP daemon
  (``signal-cli daemon --http``). Mirrors G.26 (WhatsApp) pattern:
  outbound + reactions in this adapter; inbound via webhook adapter
  (G.3) wired to signal-cli's ``/receive`` endpoint or a custom poller.
- **Capabilities = REACTIONS only.** signal-cli supports edit + delete
  via newer JSON-RPC methods, but availability is inconsistent across
  versions; deferred until we add a version detection step.
- `chat_id` accepts both E.164 phone numbers (``+15551234567``) and
  Signal group ids (``group.<base64>``).
- `message_id` is the signal-cli timestamp — used as the
  ``targetTimestamp`` for reactions; non-numeric ids rejected with a
  clear error so callers don't pass arbitrary strings.
- **Setup metadata** (G.25 pattern): ``setup.channels[].id="signal"``,
  env_vars ``["SIGNAL_CLI_URL", "SIGNAL_PHONE_NUMBER"]``, signup_url
  pointing at AsamK's signal-cli repo.
- **9 new tests** in `tests/test_signal_adapter.py` — capability flag,
  basic send (JSON-RPC envelope shape), group-id send, truncation,
  empty-body rejection, signal-cli error surface, reaction payload
  (timestamp coercion to int), empty-emoji rejection, non-numeric
  message_id rejection.

### Added (Sub-project G.26 — WhatsApp adapter via Cloud API, Tier 4.x)

- **`extensions/whatsapp/`** — new bundled channel plugin. WhatsApp
  outbound text + reactions via Meta's Cloud API (Graph API
  `/v18.0/{phone_number_id}/messages`).
  - **No edit / delete.** WhatsApp Cloud API does not support those
    operations on outbound messages from a business account, so the
    adapter declines those flags. Capability flag = REACTIONS only.
  - **No inbound.** Cloud API delivers inbound by webhook POST — use
    the webhook adapter (G.3) wired to a Cloud API webhook callback
    URL. The pattern matches G.17 (Slack), G.18 (Mattermost), G.19
    (Matrix): outbound + reactions in this adapter, inbound via the
    generic webhook adapter.
  - `chat_id` is the recipient's E.164 phone number (e.g.
    `+919876543210`); the adapter strips the leading `+` per Cloud
    API expectation. `max_message_length=4096` per Meta's docs.
  - **Setup metadata** declared on the manifest (G.23/G.24/G.25
    pattern): `setup.channels[].id="whatsapp"`, env_vars
    `["WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"]`,
    signup_url pointing at Meta's Cloud API quickstart.
- **8 new tests** in `tests/test_whatsapp_adapter.py` — capability
  flag (reactions only, no edit/delete/voice), basic send (E.164
  stripping, payload shape), path target (phone_number_id in URL),
  truncation to 4096, empty-body rejection, HTTP error surface,
  reaction payload shape, empty-emoji rejection (Cloud API would
  CLEAR reactions on empty emoji; the adapter rejects to prevent
  accidental clears).

### Removed (OI tier-trim cleanup, 2026-04-25)

User-directed cleanup after audit of duplicate functionality between
Open Interpreter (OI) tiers and OC's native features:

- **OI Tier 2 — communication tools removed.** Email / SMS / Slack /
  Discord / browser-notification surfaces overlap with OC's channel
  adapter ecosystem (telegram, discord, slack, mattermost, matrix,
  email — all bundled) and the MCP server bridge. Native channels
  carry richer capability flags (reactions, edits, threads, voice)
  than OI's wrappers, so we delete the duplicates rather than maintain
  parallel surfaces.
- **OI Tier 3 — browser tools removed.** Navigate / extract content /
  browser screenshot overlap with `extensions/opencli-scraper/`'s
  3-tool web stack (`ScrapeRaw` / `FetchProfile` / `MonitorPage`),
  which now ships enabled-by-default (see below). OpenCLI's HTTP
  layer covers Saksham's stock-research workflow; full browser
  control via OI was speculative.
- **OI Tier 4 — system control removed.** Run / kill process / system
  command / restart app overlap with the built-in `BashTool`. Bash
  already supports any shell command, including process management.
- **OI Tier 5 — advanced removed.** Schedule task overlaps with the
  G.1 cron subsystem; custom-code execution overlaps with `BashTool`.
- **Dependent use_case modules + tests removed.** Six use_cases under
  `extensions/oi-capability/use_cases/` (`dev_flow_assistant`,
  `autonomous_refactor`, `life_admin`, `email_triage`,
  `proactive_security_monitoring`, `temporal_pattern_recognition`)
  imported the removed tier modules; deleted along with their test
  fixtures. The two tier-1-only use_cases stay:
  `context_aware_code_suggestions` + `personal_knowledge_management`.

**What stays:** OI Tier 1 (introspection) — read file regions, list
apps, clipboard, screenshot, screen text, recent files, search, git
log. Eight tools that OC's core does NOT have natively. These remain
wired through F1 ConsentGate at dispatch.

`extensions/coding-harness/plugin.json` `tool_names` shrunk from 33
to 18 entries; description + version bumped to 0.3.0 to mark the
breaking surface change.

### Changed (OpenCLI scraper enabled by default, 2026-04-25)

- **`extensions/opencli-scraper/plugin.json`** flipped
  `enabled_by_default` from `false` → `true` per user instruction
  after legal review. Safety stack (rate limiting + robots.txt cache +
  field whitelist + F1 ConsentGate capability claims + F2 bus audit)
  remains intact — the scraper is still tightly bounded; it just no
  longer needs an explicit per-install opt-in.
- **Latent manifest bug fixed.** `kind: "tools"` (invalid — schema
  rejects it) corrected to `kind: "tool"`. The manifest had been
  silently failing pydantic validation since landing, so the plugin
  was never actually loading. The flip-to-default + the bug fix land
  together; otherwise the toggle would have had no effect.
- Updated `tests/test_opencli_consent_integration.py::
  test_plugin_manifest_still_disabled_by_default` →
  `test_plugin_manifest_enabled_by_default` to reflect the new
  posture.

### Added (Sub-project G.25 — Channel setup metadata, Tier 4 OpenClaw port follow-up)

- **`plugin_sdk.SetupChannel`** — frozen dataclass symmetric to
  `SetupProvider` (G.23/G.24) but for channel plugins (Telegram,
  Discord, iMessage, etc.). Fields: `id`, `env_vars`, `label`,
  `signup_url`, `requires_user_id` (Telegram-style allowlist hint).
- **`PluginSetup.channels: tuple[SetupChannel, ...] = ()`** — new field
  on the existing `PluginSetup` dataclass. Default-empty tuple keeps
  every existing manifest backwards-compatible.
- **Bundled channel manifests updated** — telegram declares
  `id: "telegram"`, `env_vars: ["TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID"]`,
  `signup_url: "https://t.me/BotFather"`, `requires_user_id: true`;
  discord declares `id: "discord"`, `env_vars: ["DISCORD_BOT_TOKEN"]`,
  `signup_url: "https://discord.com/developers/applications"`.
- **Manifest validator schema** — `SetupChannelSchema` with
  `extra="forbid"` (typo detection) and the empty-string drop pattern
  shared with G.21–G.24.
- **10 new tests** in `tests/test_channel_setup_metadata.py` —
  schema parse (minimal / full / drops empties / typo rejection /
  omitted-channels default), `_parse_manifest` flattening,
  bundled-manifest regression guard for telegram + discord,
  backwards-compat (no setup → no channels; providers-only → empty
  channels tuple).

### Added (Sub-project G.24 — Setup wizard reads manifest display fields, Tier 4 OpenClaw port follow-up)

- **`SetupProvider` extended with display fields** — `label: str`,
  `default_model: str`, `signup_url: str`. All default to empty string
  (no value), keeping every existing manifest backwards-compatible.
- **`opencomputer setup` wizard now manifest-driven.** The hard-coded
  `_SUPPORTED_PROVIDERS` dict is renamed to `_BUILTIN_PROVIDER_FALLBACK`
  and only fires when discovery yields nothing or a manifest doesn't
  declare the field. New `_discover_supported_providers()` walks plugin
  candidates, reads each `setup.providers[]` entry, and merges
  manifest-declared values over the fallback dict.
- **Third-party provider plugins now self-describe** in the wizard —
  add a `setup.providers[]` block to your plugin.json and the wizard
  shows your provider in the menu without core changes.
- **Bundled provider manifests updated** — anthropic-provider declares
  `label: "Anthropic (Claude)"`, `default_model: "claude-opus-4-7"`,
  `signup_url: "https://console.anthropic.com/settings/keys"`;
  openai-provider declares `label: "OpenAI (GPT)"`,
  `default_model: "gpt-5.4"`, `signup_url: "https://platform.openai.com/api-keys"`.
- **9 new tests** in `tests/test_setup_wizard_manifest_driven.py` —
  schema parses display fields (with empty-default), bundled-manifest
  regression guard, manifest-over-fallback merge, empty-string preserves
  fallback, third-party provider added, discovery failure falls back
  silently, exported helper symbol shape.
- **Existing test updated** — `test_setup_wizard_provider_catalog_includes_anthropic_and_openai`
  in `test_phase5.py` now reads via `_get_supported_providers()`.

### Added (Sub-project G.23 — Plugin setup metadata, Tier 4 OpenClaw port)

- **`plugin_sdk.PluginSetup` + `plugin_sdk.SetupProvider`** — frozen
  dataclasses declaring cheap setup metadata before plugin runtime
  loads. `PluginManifest.setup: PluginSetup | None` is the new manifest
  field; default `None` keeps every existing manifest backwards-
  compatible. Mirrors OpenClaw's `PluginManifestSetup` /
  `PluginManifestSetupProvider` at
  `sources/openclaw-2026.4.23/src/plugins/manifest.ts:76-97`.
- **`SetupProvider`** declares one provider id with `auth_methods` (e.g.
  `("api_key", "bearer")`) and `env_vars` (e.g. `("ANTHROPIC_API_KEY",)`).
  Order matters in `env_vars`: the first entry is canonical for setup
  tools.
- **`opencomputer.plugins.discovery.find_setup_env_vars_for_provider`**
  — pure helper, no I/O. Resolves a provider id (e.g. `"anthropic"`) to
  its declared env-var tuple by walking candidates' `setup.providers`.
  Returns `()` when nothing matches so callers can fall back gracefully.
- **`cli._check_provider_key` refactor** — reads env-var requirements
  from manifests first, then falls back to a legacy hard-coded dict
  (`{anthropic, openai}`) only when discovery yields nothing. Push of
  knowledge from core back into plugin manifests; third-party providers
  can now self-describe.
- **Bundled provider manifests updated** — anthropic-provider declares
  `setup.providers[0]: {id: "anthropic", auth_methods: ["api_key",
  "bearer"], env_vars: ["ANTHROPIC_API_KEY"]}`; openai-provider declares
  `{id: "openai", auth_methods: ["api_key"], env_vars: ["OPENAI_API_KEY"]}`.
- **Manifest validator schemas** — `SetupProviderSchema` +
  `PluginSetupSchema` mirror the dataclasses with `extra="forbid"` (typo
  detection) and the empty-string drop pattern shared with G.21/G.22.
- **15 new tests** in `tests/test_plugin_setup_metadata.py` —
  schema parse (omitted / minimal / drops empties / typo rejection /
  requires_runtime default), `_parse_manifest` flattening,
  `find_setup_env_vars_for_provider` (declared / unknown / no-metadata
  / first-wins), bundled-manifest regression guard, and
  `cli._check_provider_key` reading manifest first vs. fallback.

### Added (Sub-project G.22 — Legacy plugin id normalization, Tier 4 OpenClaw port)

- **`PluginManifest.legacy_plugin_ids: tuple[str, ...] = ()`** — new
  optional field for plugins to declare ids they used to be known by.
  When OpenComputer renames `anthropic-provider` → `claude-provider`,
  the new manifest declares `legacy_plugin_ids: ["anthropic-provider"]`
  and existing user `profile.yaml` references silently map to the new
  id. Mirrors OpenClaw's `legacyPluginIds` at
  `sources/openclaw-2026.4.23/src/plugins/manifest-registry.ts:100`.
- **`opencomputer.plugins.discovery.build_legacy_id_lookup(candidates)`**
  — pure helper, no I/O. Returns `{legacy_id: current_id}` after applying
  three conflict policies: self-aliases dropped silently (a typo),
  legacy ids that collide with another current id skipped + warned,
  duplicate claims by multiple plugins last-write-wins + warned.
- **`opencomputer.plugins.discovery.normalize_plugin_id(plugin_id,
  candidates)`** — single-id wrapper around the lookup, returns
  unchanged ids untouched. Mirrors OpenClaw's `normalizePluginId` at
  `sources/openclaw-2026.4.23/src/plugins/config-state.ts:83-91`.
- **`PluginRegistry.load_all` Layer B′ — legacy-id normalization.**
  Runs before Layer C (G.21 model-prefix) so a renamed provider plugin's
  current id is what model-prefix matching adds to (avoids double-adding
  legacy + current ids). Each entry in `enabled_ids` is rewritten through
  the legacy lookup before the activation check.
- **Manifest validator schema** — `legacy_plugin_ids: list[str]` field
  with the same empty-string-drop tolerance as `model_support` (G.21).
- **16 new tests** in `tests/test_legacy_plugin_ids.py` — schema parse
  (omitted / list / drops empties / dataclass flattening),
  `build_legacy_id_lookup` (simple / multiple / no-legacy / self-alias /
  alias-collides / duplicate-claim with warning), `normalize_plugin_id`
  (unknown / legacy / current-id passthrough), and end-to-end
  `PluginRegistry.load_all` activation via legacy ids in `enabled_ids`.

### Added (Sub-project G.21 — Model-prefix auto-activation, Tier 4 OpenClaw port)

- **`plugin_sdk.ModelSupport`** — frozen dataclass declaring which model ids
  a provider plugin can serve. Two fields, both tuples: `model_prefixes`
  (`str.startswith`) and `model_patterns` (`re.search` regex). Mirrors
  OpenClaw's `modelSupport` field at `sources/openclaw-2026.4.23/src/plugins/
  providers.ts:316-337`.
- **`PluginManifest.model_support: ModelSupport | None = None`** — new
  optional manifest field. Default `None` keeps every existing plugin
  backwards-compatible.
- **`opencomputer.plugins.discovery.find_plugin_ids_for_model(model_id,
  candidates)`** — pure helper, no I/O. Patterns checked first
  (`re.search`); prefixes second (`str.startswith`). Bad regex silently
  skipped so one malformed manifest can't break the registry. Result
  sorted alphabetically for prompt-cache determinism.
- **`PluginRegistry.load_all` Layer C — model-prefix auto-activation.**
  When a filter is active (`enabled_ids` is a frozenset, not `"*"`),
  plugins whose `model_support` matches `cfg.model.model` are silently
  added to the set. Solves "I switched to gpt-4o, why is openai-provider
  disabled?" — picking the model implicitly enables the matching plugin.
- **Bundled provider manifests updated.** `extensions/anthropic-provider/
  plugin.json` declares `model_support.model_prefixes: ["claude-"]`;
  `extensions/openai-provider/plugin.json` declares `["gpt-", "o1", "o3",
  "o4"]`.
- **Manifest validator schema** — `ModelSupportSchema` mirrors the
  dataclass with pydantic. Empty / whitespace-only entries silently
  dropped (OpenClaw tolerance pattern from `manifest.json5-tolerance.test
  .ts`); typo'd field names rejected loudly via `extra="forbid"`.
- **15 new tests** in `tests/test_model_prefix_activation.py` —
  manifest schema parse (omitted / prefixes-only / drops empties /
  rejects typos), `_parse_manifest` flattening, `find_plugin_ids_for_model`
  (prefix / pattern / invalid regex / no-support / empty-id / sorted),
  bundled-manifest regression guard, and end-to-end Layer C activation
  through `PluginRegistry.load_all`.

### Added (Sub-project G.19 — Matrix adapter (Client-Server API), Tier 3.x)

- **`extensions/matrix/`** — Matrix channel adapter via the Client-Server API.
  - `MatrixAdapter` outbound: `m.room.message` text via PUT `/_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}`. Reactions via `m.reaction` events (`m.relates_to.rel_type=m.annotation`). Edits via `m.replace` events with `m.new_content` (the standard Matrix convention). Deletes via `/redact/` endpoint with optional reason.
  - **No end-to-end encryption** in v1 — works only in unencrypted rooms. E2E
    needs `matrix-nio` + olm/megolm; deferred until demand.
  - Inbound NOT in this adapter — use webhook adapter (G.3) wired to a Matrix
    bridge / appservice / hookshot.
  - Capability flag: REACTIONS + EDIT_MESSAGE + DELETE_MESSAGE + THREADS.
- **Plugin config** via env vars: `MATRIX_HOMESERVER` + `MATRIX_ACCESS_TOKEN`.
  Disabled by default.
- **14 new tests** in `tests/test_matrix_adapter.py` — capability flag, send
  (basic / thread root / URL-encoded room id / truncate / HTTP error),
  reactions (unicode emoji passes through directly per Matrix spec /
  empty-emoji rejection), edit (m.replace with m.new_content), delete
  (redact endpoint, with/without reason), connect caches user_id.

### Added (Sub-project G.18 — Mattermost adapter (Web API outbound), Tier 3.x)

- **`extensions/mattermost/`** — new bundled channel plugin. Mattermost
  (self-hosted Slack alternative) outbound + reactions / edit / delete via
  Web API at `/api/v4/...`. Mirrors G.17 Slack pattern: no WebSocket
  runtime; inbound via Mattermost Outgoing Webhooks → OC webhook adapter
  (G.3).
  - `adapter.py::MattermostAdapter` — `connect` verifies token via
    `users/me` and caches the bot user id (needed for `reactions`). `send`
    POSTs to `/api/v4/posts` with optional `root_id` for threaded replies.
    `send_reaction` POSTs to `/api/v4/reactions` with `user_id + post_id +
    emoji_name`. `edit_message` uses PUT, `delete_message` uses DELETE on
    `/api/v4/posts/{id}`.
  - Capability flag = REACTIONS + EDIT_MESSAGE + DELETE_MESSAGE + THREADS.
  - **Emoji-to-name map duplicated from Slack** (cross-plugin imports are
    forbidden by `tests/test_cross_plugin_isolation.py`). Same 16 unicode
    emoji → name mappings as Slack.
- **Plugin config** via env vars: `MATTERMOST_URL` and `MATTERMOST_TOKEN`
  (Personal Access Token with `post:write`). Disabled by default.
- **11 new tests** in `tests/test_mattermost_adapter.py` — capability flag,
  connect-caches-user-id, invalid-token-rejection, send (basic / threaded /
  truncate / HTTP error), reactions (emoji mapped + posted to API), edit
  (PUT) + delete (DELETE).

### Added (Sub-project G.17 — Slack adapter (Web API outbound), Tier 2.12)

- **`extensions/slack/`** — new bundled channel plugin. Outbound + reactions /
  edit / delete via raw httpx calls to the Slack Web API. **No Socket Mode runtime**
  — keeps the dep footprint small (no `slack_sdk`). Inbound: users configure Slack
  Outgoing Webhooks pointing at an OC webhook token (G.3) — covers the most common
  case "agent posts to a Slack channel" without needing a public URL.
  - `adapter.py::SlackAdapter` — `connect` verifies the bot token via `auth.test`.
    `send` posts to `chat.postMessage` (with optional `thread_ts` + `broadcast`
    for threaded replies). `send_reaction` maps unicode emoji → Slack reaction
    names (👍 → `thumbsup`, ❤️ → `heart`, etc.) and treats `already_reacted` as
    success (idempotent). `edit_message` / `delete_message` via `chat.update` /
    `chat.delete`. Capability flag = REACTIONS + EDIT_MESSAGE + DELETE_MESSAGE
    + THREADS.
- **Plugin config** via env var: `SLACK_BOT_TOKEN` (must start `xoxb-`).
  Plugin warns at register time if the token doesn't have the expected prefix.
  Required scopes: `chat:write`, `reactions:write`, `chat:write.public`.
- **21 new tests** in `tests/test_slack_adapter.py` — capability flag advertises
  G.17 set + skips voice/typing, send + thread + broadcast + slack-error,
  reactions (unicode mapping, bare-name pass-through, already_reacted idempotence),
  edit + delete, full emoji-to-name mapping (9 parametrised cases incl bare
  name, mixed-case, empty), connect handles `invalid_auth`.

Use case: agent's daily briefing also gets posted to a #stocks Slack channel for a
team / community. Slack Outgoing Webhooks → OC webhook adapter handles the
inbound side without Socket Mode complexity.

### Added (Sub-project G.16 — iMessage adapter (BlueBubbles bridge), Tier 2.11)

- **`extensions/imessage/`** — new bundled channel plugin. iMessage via the
  BlueBubbles self-hosted Mac bridge (https://bluebubbles.app). Mac-tied —
  doesn't work in the Linux Docker image; users running on a Mac can enable it.
  - `adapter.py::IMessageAdapter` — polls BlueBubbles `GET /api/v1/message/query`
    every 10 s by default. Tracks the highest ROWID seen so polling is idempotent
    (no replay of old messages on restart). Skips `isFromMe` echoes. Emits
    MessageEvent with chat GUID as `chat_id`, sender phone/email as `user_id`.
  - Outbound: `send` POSTs to `/api/v1/message/text`. `send_reaction` maps emoji
    to BlueBubbles tapback names (love / like / dislike / laugh / emphasize / question);
    unmappable emoji return a clear local error without hitting the network.
  - Capability flag: `REACTIONS` only. Edit / voice / file attachments deferred
    to G.16.x follow-ups.
- **Plugin config** via env vars: `BLUEBUBBLES_URL` + `BLUEBUBBLES_PASSWORD` required;
  `BLUEBUBBLES_POLL_INTERVAL` (default 10 s) optional. Disabled by default.
- **22 new tests** in `tests/test_imessage_adapter.py` — capability flag, send text
  with chat GUID, length truncation, HTTP error handling, reactions (supported
  emoji posts to react endpoint, ❤️ → love, unmappable emoji errors locally),
  polling (filters echoes, skips seen ROWIDs, chronological order, empty/no-chat
  message rejection), full tapback emoji map (9 cases).

Use case: Saksham chats with OC over iMessage from his iPhone while away from his
laptop. The BlueBubbles bridge runs on his always-on Mac. Hybrid deployment:
gateway on Mac for iMessage + on VPS for cron/webhook (different profiles, both
sharing the agent loop).

### Added (Sub-project G.15 — Doctor checks for G subsystems, Tier 2.15)

- **`opencomputer doctor`** now reports the state of every Sub-project G subsystem
  alongside the existing core checks. Read-only — no state mutation. Surfaces:
  - **cron storage** — pass with job count, skip when no jobs file, warn on
    corrupted JSON.
  - **webhook tokens** — pass with active/total counts, skip when no tokens file.
  - **cost-guard limits** — pass when caps set, **warn when usage tracked but no
    caps configured** (voice / paid MCPs unguarded — actionable signal).
  - **voice TTS/STT key** — pass when `OPENAI_API_KEY` set, skip otherwise.
  - **oauth store** — pass with token count, warn on permission drift (dir mode
    not 0700).
- **10 new tests** in `tests/test_doctor_g_subsystems.py` — empty-profile all-skip,
  each subsystem's pass / skip / warn paths.

Setup wizard (Tier 2.14) intentionally not extended — OC's existing 320-LOC wizard
covers the new G subsystems via env-var prompts that the user can address as needed;
adding per-subsystem sections would create sprawl. Doctor surfacing the gaps is
sufficient onboarding signal.

### Added (Sub-project G.14 — Email channel adapter (IMAP+SMTP), Tier 2.7)

- **`extensions/email/`** — new bundled channel plugin. IMAP polling for inbound +
  SMTP for outbound. Stdlib only — no new deps. `enabled_by_default: false`.
  - `adapter.py::EmailAdapter` — connects via `imaplib.IMAP4_SSL` / `smtplib.SMTP_SSL`
    wrapped in `asyncio.to_thread` so they don't block the gateway loop.
    Polling every 60 s by default; fetches `UNSEEN`, marks `\Seen` after parse,
    parses subject + body (multipart-aware, HTML fallback with stdlib stripping),
    emits `MessageEvent` with the sender's address as `chat_id`. `allowed_senders`
    config (case-insensitive) blocks random spam from triggering the agent.
  - Outbound `send(chat_id, text, subject=, in_reply_to=)` constructs an `EmailMessage`
    with proper threading headers (`In-Reply-To` + `References`) and ships via SMTP_SSL.
- **Plugin config** via env vars: `EMAIL_IMAP_HOST` / `EMAIL_USERNAME` / `EMAIL_PASSWORD`
  required; `EMAIL_SMTP_HOST` / `EMAIL_FROM_ADDRESS` / `EMAIL_POLL_INTERVAL` /
  `EMAIL_MAILBOX` / `EMAIL_ALLOWED_SENDERS` optional.
- **17 new tests** in `tests/test_email_adapter.py` — capability flag is NONE, plaintext
  parsing, subject-only, HTML fallback, no-From rejection, allowed-senders filter
  (block / allow / case-insensitive / no-filter), SMTP send with threading headers,
  invalid recipient, SMTP failure wrapping, IMAP login on connect, IMAP poll fetches
  UNSEEN messages, HTML stripping (3 cases).

Use case: Saksham forwards earnings emails / news articles to his configured address;
OC analyzes and replies to the original sender. Gmail App Password supported.

### Added (Sub-project G.13 — OAuth/PAT token store for MCP providers, Tier 2.5 v1)

- **`opencomputer/mcp/oauth.py`** — secure token storage at
  `<profile_home>/mcp_oauth/<provider>.json` (mode 0600, dir 0700, atomic writes).
  - `OAuthToken` frozen dataclass — `access_token`, `token_type`, `expires_at`,
    `scope`, `refresh_token`, `created_at`, `provider`.
  - `OAuthTokenStore` — `put / get / list / revoke`. Lowercase-normalises provider
    names. Skips expired tokens automatically. Corrupted files return `None`
    rather than raise.
  - `paste_token(...)` convenience for the most common case (PAT pasted from a
    provider's settings page).
  - `get_token_for_env_lookup(provider, env_var)` — fallback chain used by MCP
    server-config rendering: env-var first, then OAuth store, then `None`.
- **`opencomputer mcp oauth-paste / oauth-list / oauth-revoke`** CLI subcommands.
  `oauth-paste` prompts for the token securely on stdin (hidden input) when
  `--token` isn't passed. `oauth-list` never prints token values.
- **26 new tests** in `tests/test_mcp_oauth.py` — round-trip, normalisation,
  overwrite, revoke, list, expiry filtering, file-mode 0600 / dir-mode 0700,
  corrupted-file handling, paste validation + stripping, env-fallback chain
  (4 cases), CLI smoke (5 cases incl token redaction in listing).

What's NOT here yet (deferred to G.13.x follow-ups): browser-based OAuth dance
with callback server + provider-specific flows for github/google/notion.
The storage layer is forward-compatible: those flows will call
`OAuthTokenStore.put(...)` and everything downstream works.

Use case: `opencomputer mcp install github` (G.7) declares the github MCP needs
`GITHUB_PERSONAL_ACCESS_TOKEN`. Saksham can either set the env var OR paste
the PAT once with `opencomputer mcp oauth-paste github` and the MCP launch
falls back to the stored value.

### Added (Sub-project G.12 — Discord reactions + edit + delete, Tier 2.8 / 2.9)

- **`extensions/discord/adapter.py`** — DiscordAdapter now declares
  `ChannelCapabilities.{TYPING, REACTIONS, EDIT_MESSAGE, DELETE_MESSAGE, THREADS}` and implements:
  - `send_reaction(chat_id, message_id, emoji)` — uses `message.add_reaction`. Accepts unicode
    emoji (`👍`) or custom guild emoji (`<:name:id>`). Surfaces Discord's `Forbidden` cleanly.
  - `edit_message(chat_id, message_id, text)` — `message.edit`. Bots can only edit their own
    messages; clear error message on `Forbidden`. No 48 h time window (unlike Telegram). Truncates
    to `max_message_length` (2000).
  - `delete_message(chat_id, message_id)` — `message.delete`. Own messages free; others' need
    `MANAGE_MESSAGES`.
  - Internal `_resolve_channel(chat_id)` helper — cache-aware fetch with graceful failure.
- **11 new tests** in `tests/test_discord_capabilities.py` — capability flag advertises
  G.12 set + does not advertise unimplemented (voice / photo / document), reactions add via
  discord.py + handles NotFound, edit truncates + handles Forbidden, delete handles missing
  message, channel resolution caches + falls back to fetch.

Mirrors the G.2 pattern from Telegram but with Discord's quirks (own-message-only edit, no time
window, fetch-message-then-method approach).

### Added (Sub-project G.11 — MCP catalog binding via plugin manifest, Tier 2.13)

- **`PluginManifest.mcp_servers: tuple[str, ...]`** — new optional manifest field. List of MCP
  preset slugs (from G.7's `PRESETS`) the plugin needs. Validator + parser threaded through
  `manifest_validator.PluginManifestSchema` + `discovery._parse_manifest`.
- **`opencomputer/plugins/loader.py::_install_mcp_servers_from_manifest`** — runs after the
  plugin's `register()` succeeds. Resolves each slug → `MCPServerConfig` → appends to
  `config.yaml`. Idempotent (skips servers with names already in config — respects user
  customisation), logs WARNING on unknown slug but never blocks load.
- **9 new tests** in `tests/test_mcp_catalog_binding.py` — validator accepts + defaults to empty,
  parser threads field, install round-trip, idempotence, unknown-slug warns, multiple presets,
  empty list no-op, user customisation respected.

Use case: a plugin that depends on `filesystem` MCP can declare `"mcp_servers": ["filesystem"]`
in its `plugin.json`, and the user gets the MCP added automatically when the plugin activates —
no separate `opencomputer mcp install filesystem` step.

### Added (Sub-project G.10 — Adapter scaffolder + capabilities-aware template, Tier 2.16)

- **`opencomputer/templates/plugin/channel/adapter.py.j2`** — channel adapter template upgraded
  for G.2's `ChannelCapabilities`. Now imports the flag enum, declares
  `capabilities = ChannelCapabilities.NONE` by default, and includes commented-out method stubs
  for every optional capability (`send_typing` / `send_reaction` / `send_photo` / `send_document` /
  `send_voice` / `edit_message` / `delete_message` / `download_attachment`) with the matching
  flag-to-uncomment hint. Authors copy what they need rather than guessing the API surface.
- **`opencomputer/cli_adapter.py`** — new CLI subgroup providing discoverable channel-adapter
  surface:
  - `opencomputer adapter new <name>` — alias for `plugin new <name> --kind channel` (more
    discoverable since channel adapters are the most common third-party plugin type).
  - `opencomputer adapter capabilities` — Rich table listing all `ChannelCapabilities` flags with
    the method to override + a one-line description. Reduces "what does VOICE_IN do?" trips to
    grep.
- **7 new tests** in `tests/test_adapter_scaffolder.py` — `capabilities` lists all 11 flags +
  method names; `new` creates plugin dir; template content includes `ChannelCapabilities`,
  defaults to NONE, has all 8 optional method stubs, PascalCases class names correctly.

This is the force multiplier from the integration plan's self-audit (R2): every future channel
adapter (Slack / Matrix / WhatsApp / Signal / iMessage) drops from "build from scratch" to
"uncomment the stubs for the platform's capabilities + fill in the API calls."

### Added (Sub-project G.9 — Voice (TTS + STT), Tier 2.10)

- **`opencomputer/voice/`** — new subpackage. Cost-guarded text-to-speech and speech-to-text via
  OpenAI APIs (`tts-1` / `tts-1-hd` / `whisper-1`):
  - `synthesize_speech(text, *, cfg, dest_dir)` — TTS to file. Default `opus` format = Telegram
    voice. Supports `mp3 / aac / flac / wav / pcm` for other channels. 4096 char hard limit
    enforced locally before API call.
  - `transcribe_audio(audio_path, *, model, language)` — Whisper STT. WAV duration parsed from
    header for accurate cost projection; other formats fall back to a 30 s assumption.
  - `VoiceConfig` dataclass — `model / voice / format / speed`. Voice limited to OpenAI's 6
    canonical voices (alloy / echo / fable / onyx / nova / shimmer).
  - Cost helpers: `tts_cost_usd(text, model)` and `stt_cost_usd(duration_s, model)` for budget
    projection. Pricing constants tagged with `PRICING_VERSION`.
- **Cost-guard integration** — every synthesize / transcribe call pre-flights via
  `CostGuard.check_budget("openai", projected_cost_usd=…)` and records actual usage on success
  with an operation label (`tts:tts-1` / `stt:whisper-1`). `BudgetExceeded` propagates so callers
  can fall back gracefully (e.g. text-only when voice budget is hit).
- **`opencomputer voice {synthesize, transcribe, cost-estimate}`** CLI subgroup. `cost-estimate`
  runs without making an API call so users can preview spend before committing.
- **22 new tests** in `tests/test_voice.py` — pricing helpers, full TTS path with mocked OpenAI
  client (request kwargs verification, file output, empty/oversized rejection, voice/format
  validation, BudgetExceeded blocks the call entirely, API errors wrapped as RuntimeError),
  STT path (mock client, language hint, missing/oversized file, budget block, WAV duration
  parsing), CLI smoke for cost-estimate.

This unlocks Saksham's voice-briefing use case: cron job at 8:30 AM runs the
`stock-market-analysis` skill, agent's text response gets routed through `synthesize_speech` →
`adapter.send_voice` → Telegram voice message. The cost-guard cap (e.g. `--daily 0.50`) ensures a
runaway loop can't drain the wallet.

### Added (Sub-project G.8 — Cost-guard module, Tier 2.17)

- **`opencomputer/cost_guard/`** — new subpackage tracking per-provider USD spend with
  daily + monthly caps. Prevents runaway costs from a misconfigured cron / voice loop / agent
  retry storm. Storage at `<profile_home>/cost_guard.json` (mode 0600, atomic writes,
  90-day retention).
  - `CostGuard.record_usage(provider, cost_usd, operation)` — log a paid API call.
  - `CostGuard.check_budget(provider, projected_cost_usd)` → `BudgetDecision` with
    `allowed`, `reason`, daily/monthly used + limit. Caller-driven (not enforced via interceptor)
    so providers can decide their own fallback strategy when budget hits.
  - `CostGuard.set_limit(provider, daily, monthly)` — `None` clears, float sets cap.
  - `CostGuard.current_usage(provider=None)` — `ProviderUsage` summary with per-operation breakdown.
  - `CostGuard.reset(provider=None)` — clear recorded usage (limits stay).
  - `BudgetExceeded` exception for callers that prefer exception flow.
  - `get_default_guard()` — process-wide singleton rooted at the active profile.
- **`opencomputer cost {show,set-limit,reset}`** CLI subgroup. `show` renders a Rich table with
  daily/monthly used vs. limit + per-operation breakdown for the current day.
- **27 new tests** in `tests/test_cost_guard.py` — record/check round-trips, negative-cost
  rejection, lowercase normalisation, operation-label surfacing, daily/monthly caps blocking,
  no-limits-always-allowed, set/clear-limits, retention pruning (90-day cutoff), 0600 file mode,
  profile isolation, singleton, frozen dataclasses, full CLI smoke (set-limit + show + reset).

This unblocks Tier 2.10 voice (TTS @ $0.015/1k chars + Whisper @ $0.006/min) — the cost-guard
will pre-flight check budget on every voice op so a runaway can't drain a wallet.

### Added (Sub-project G.7 — MCP presets bundle, Tier 2.4)

- **`opencomputer/mcp/presets.py`** — registry of 5 vetted MCP presets:
  - `filesystem` — local file ops in CWD root (npx, no creds).
  - `github` — repos / issues / PRs (npx, needs `GITHUB_PERSONAL_ACCESS_TOKEN`).
  - `fetch` — URL → markdown for the agent (uvx, no creds).
  - `postgres` — read-only Postgres queries (npx, needs `POSTGRES_URL`).
  - `brave-search` — web search via Brave API (npx, needs `BRAVE_API_KEY`).
  Each preset declares `required_env` so the install path can warn when prerequisites are unset.
- **`opencomputer mcp presets`** — list bundled presets with description + required env vars.
- **`opencomputer mcp install <slug> [--name N] [--disabled]`** — adds the preset's
  `MCPServerConfig` to `config.yaml`. Refuses if the server name already exists. After install,
  prints a checkmark/cross status icon for each `required_env` var so missing creds are surfaced
  immediately. Includes the preset's homepage URL for further docs.
- **16 new tests** in `tests/test_mcp_presets.py` — registry shape (5 presets, all stdio,
  descriptions + homepage), config immutability, install CLI (success / unknown preset / custom
  name / `--disabled` / duplicate-name error / env-var warning).

Use case unlocked: `opencomputer mcp install fetch` or `opencomputer mcp install github` instead
of hunting for the right `npx` invocation + manually editing config.yaml.

### Added (Sub-project G.6 — MCP server mode, Tier 2.2)

- **`opencomputer/mcp/server.py`** — new MCP server using `mcp.server.fastmcp.FastMCP` over stdio.
  Exposes 5 tools so external MCP clients (Claude Code, Cursor) can query OC's session history:
  - `sessions_list(limit=20)` — recent sessions across all platforms.
  - `session_get(session_id)` — single session metadata.
  - `messages_read(session_id, limit=100)` — message log including tool_calls.
  - `recall_search(query, limit=20)` — FTS5 search across all sessions.
  - `consent_history(capability=None, limit=50)` — F1 audit-log entries
    (gracefully returns `[]` for pre-F1 / fresh profiles).
  Builds the server fresh per CLI invocation so `opencomputer -p <profile> mcp serve` resolves
  the correct profile via `_home()`.
- **`opencomputer mcp serve`** — new CLI subcommand. Runs the MCP server until stdin/stdout closes.
- **12 new tests** in `tests/test_mcp_server.py` — server construction, tool count + names,
  description and inputSchema invariants, empty-DB returns for each of the 5 tools, CLI wiring.

Use case unlocked: while coding in Claude Code, Saksham can ask "what did we discuss about
GUJALKALI yesterday?" and Claude Code calls `recall_search` against OC's session DB to surface
the Telegram conversation. Bridges OC ↔ Claude Code without any manual export step.

### Added (Sub-project G.5 — Pending-task drain on shutdown, Tier 2.6)

- **`opencomputer/hooks/runner.py::drain_pending(timeout=5.0)`** — async helper that awaits all
  in-flight `fire_and_forget` tasks (e.g. F1 audit-log writers) on graceful shutdown, with bounded
  timeout. Returns `(completed, cancelled)`. Tasks exceeding the timeout are cancelled so a stuck
  handler doesn't hang exit. Closes the F1 audit-chain integrity gap that occurred when the process
  was terminated mid-write.
- **`opencomputer/hooks/runner.py::pending_count()`** — sync introspection helper for status / tests.
- **`opencomputer/cli.py::_memory_shutdown_atexit`** — now drains pending hooks BEFORE memory
  provider shutdown so audit writes triggered from hooks land before connections close. Single
  `asyncio.run` covers both phases.
- **9 new tests** in `tests/test_hooks_drain.py` — quick-task completion, stuck-task cancellation,
  mixed quick+stuck, exception swallowing, concurrent-fire integrity (50 simultaneous), pending_count
  semantics, empty-drain idempotence.

Source pattern: Kimi CLI's `_pending_fire_and_forget` set + drain. The drain timing was tuned for
OC's specific mix of audit-log (sub-millisecond) + Telegram-notify (1-3 s) hooks.

### Added (Sub-project G.4 — Docker support, Tier 2.3 of `~/.claude/plans/toasty-wiggling-eclipse.md`)

- **`Dockerfile`** — multi-stage build (`python:3.13-slim` builder → runtime), non-root `oc` user
  (uid 1000), `tini` as PID 1 so `docker stop` delivers SIGTERM cleanly. Builder installs the
  package + deps into `/opt/venv`; runtime stage copies just the venv + source. Webhook port
  18790 exposed. `OPENCOMPUTER_HOME=/home/oc/.opencomputer` so a single named-volume mount captures
  config + sessions + cron + consent audit chain. Layer order optimised so dep changes don't
  invalidate the source layer.
- **`docker-compose.yml`** — two profiles:
  - `default` (`gateway` service) — Telegram + Discord + cron + webhook in one container, with
    webhook port mapped + provider/channel env vars wired.
  - `cron-only` — light scheduler-only container (no channel adapters).
  Both use `restart: unless-stopped` and the named volume `opencomputer-data` for persistence.
- **`.dockerignore`** — excludes `.venv`, `__pycache__`, `.git`, `tests/`, `docs/`, sources tree,
  IDE files, build artefacts. Keeps images lean.
- **20 new tests** in `tests/test_docker.py` — structure validations that run without a Docker
  daemon: multi-stage build, non-root user, webhook port exposed, tini init, persistent home env,
  compose profiles + named volume + restart policy + provider env vars, dockerignore covers the
  expected exclude list. Lets us catch Dockerfile drift in CI even though CI doesn't build the image.

Use case unlocked: `docker compose up -d` on a $5/mo VPS → cron jobs and webhook listener run
24/7 without Saksham's laptop being awake. `docker compose --profile=cron-only up -d` for the
minimal scheduler-only deployment.

### Added (Sub-project G.3 — Webhook channel adapter, Tier 1.3 of `~/.claude/plans/toasty-wiggling-eclipse.md`)

- **`extensions/webhook/`** — new bundled channel plugin. HTTP listener for inbound triggers from
  TradingView, Zapier, n8n, GitHub Actions, custom services. Per-token HMAC-SHA256 auth via
  ``X-Webhook-Signature`` header. Plugin is `enabled_by_default: false` because it opens an inbound
  network port — must be explicitly enabled per profile.
  - `adapter.py::WebhookAdapter` — aiohttp-based HTTP server on configurable host/port (default
    `127.0.0.1:18790`). Routes: `POST /webhook/<token_id>` (signed) and `GET /webhook/health`.
    Per-token sliding-window rate limit (60 req/min default). 1 MB body cap. Signature verification
    via constant-time `hmac.compare_digest`. Capabilities: `ChannelCapabilities.NONE` (inbound-only,
    no typing / reactions / outbound — `send()` returns clear error). Payload coercion accepts
    `text` / `alert` / `message` / `body` / `content` keys (TradingView ships `alert`).
  - `tokens.py` — token registry at `<profile_home>/webhook_tokens.json` (mode 0600). Atomic writes
    via tmp + os.replace. CRUD: `create_token`, `get_token`, `list_tokens` (strips `secret`),
    `revoke_token`, `remove_token`, `mark_used`. HMAC verify helper.
  - `plugin.py` — registers WebhookAdapter when env vars `WEBHOOK_HOST` / `WEBHOOK_PORT` are
    set or defaults to `127.0.0.1:18790`.
- **`opencomputer/cli_webhook.py`** — new `opencomputer webhook {list,create,revoke,remove,info}`
  subcommand group. `create` prints the secret ONCE with copy-paste curl example.
- **`aiohttp>=3.9`** added to `pyproject.toml::dependencies` for the webhook HTTP listener.
- **28 new tests** in `tests/test_webhook_{tokens,adapter}.py`:
  - tokens: create returns id+secret of correct length, list excludes revoked, list strips secret,
    revoke marks flag, remove deletes entry, mark_used updates timestamp, HMAC verify accepts
    valid + rejects wrong/empty/unprefixed signatures, file mode 0600, profile-isolated path.
  - adapter: real aiohttp server on ephemeral port via `TestServer`. Health endpoint no-auth.
    Auth: unknown token 401, invalid signature 403, revoked token 401. Dispatch: valid signature
    fires MessageEvent with text + metadata + platform=web. Plain-text body accepted. Rate limit
    blocks burst after threshold. `send()` returns inbound-only error. Capabilities flag is NONE.
    Payload coercion: text > alert > flatten.

Use case unlocked: TradingView alert → POST → OC dispatches to agent (with the token's
`scopes` + `notify` channel hint in event metadata) → agent runs the configured skill → notifies
back via Telegram. Or: GitHub Actions "build failed" → OC investigates and pings Saksham.

### Added (Sub-project G.2 — Telegram file/voice/reaction/edit/delete capabilities, Tier 1.2 + 2.0)

- **`plugin_sdk/channel_contract.py`** — added `ChannelCapabilities` flag enum (TYPING, REACTIONS,
  PHOTO_IN/OUT, DOCUMENT_IN/OUT, VOICE_IN/OUT, EDIT_MESSAGE, DELETE_MESSAGE, THREADS). `BaseChannelAdapter`
  gains 7 new optional methods — `send_photo`, `send_document`, `send_voice`, `send_reaction`,
  `edit_message`, `delete_message`, `download_attachment` — each raising `NotImplementedError` by
  default so adapters only override what their `capabilities` flag advertises. Self-audit R1 from
  the integration plan: prevents ~50 method duplications when 10+ adapters land.
- **`plugin_sdk/__init__.py`** — re-exports `ChannelCapabilities` (now a public type).
- **`extensions/telegram/adapter.py`** — Telegram now advertises 10 capability flags and implements
  all 7 optional methods + inbound photo/document/voice attachment parsing into
  `MessageEvent.attachments`. Uses raw Bot API multipart upload (no python-telegram-bot dep).
  Bot-API limits enforced locally before request: 10 MB photo, 50 MB document, 20 MB getFile
  download. `download_attachment` accepts both raw `file_id` and `"telegram:<id>"` reference form.
- **`docs/sdk-reference.md`** — new section documenting `ChannelCapabilities` + sample adapter.
- **29 new tests** in `tests/test_channel_capabilities.py` (14 — flag enum + base defaults) and
  `tests/test_telegram_attachments.py` (15 — capability flag check, send_photo/document/voice
  request shape, oversized-file local rejection, missing-file error, reaction/edit/delete
  endpoints, download_attachment round-trip with httpx MockTransport, inbound photo/document/voice
  parsing into MessageEvent.attachments, metadata-only update skipped). Full suite: **2307 passing**.

Use case unlocked: Saksham forwards a stock chart screenshot to OC via Telegram → adapter
parses photo file_id into `MessageEvent.attachments` → agent calls `download_attachment(file_id)` →
analyzes via vision-capable provider → replies with annotated chart via `send_photo()`.

### Added (Sub-project G.1 — Hermes cron jobs port, Tier 1.1 of `~/.claude/plans/toasty-wiggling-eclipse.md`)

- **`opencomputer/cron/`** — new subpackage porting Hermes's cron infrastructure. Adapted from
  `sources/hermes-agent-2026.4.23/cron/{jobs,scheduler}.py` and `tools/cronjob_tools.py`. Profile-isolated;
  integrates with F1 ConsentGate via capability claims.
  - `cron/jobs.py` — JSON-backed CRUD: `create_job`, `list_jobs`, `update_job`, `pause_job`,
    `resume_job`, `trigger_job`, `remove_job`, `mark_job_run`, `advance_next_run`, `get_due_jobs`.
    Schedule kinds: `once` (`30m`/`2h`/`1d`/timestamp), `interval` (`every 30m`),
    `cron` expression (`0 9 * * *`). Stale-run detection fast-forwards recurring jobs past their
    grace window instead of replaying a backlog after downtime.
  - `cron/scheduler.py` — asyncio-native `tick()` (single-shot) and `run_scheduler_loop()`
    (60s default tick interval). Cross-process file lock at `<cron_dir>/.tick.lock` so the gateway's
    in-process ticker, a standalone `opencomputer cron daemon`, and manual `cron tick` never
    overlap. Recurring jobs have `next_run_at` advanced under the lock BEFORE execution
    (at-most-once on crash). Bounded parallel execution via asyncio Semaphore (default 3).
    `[SILENT]` marker in agent response suppresses delivery (output still saved).
  - `cron/threats.py` — prompt-injection scanner ported verbatim from Hermes: 10 critical regex
    patterns (prompt injection, deception, exfil, secrets read, SSH backdoor, sudoers mod, root rm)
    + 10 invisible-character classes (zero-width, BOM, bidi overrides). `scan_cron_prompt()` returns
    string, `assert_cron_prompt_safe()` raises `CronThreatBlocked`. Defence-in-depth: scan at create + at every tick.
- **`opencomputer/tools/cron_tool.py::CronTool`** — single agent-callable tool with `action`
  parameter (create/list/get/pause/resume/trigger/remove). Declares 4 F1 capability claims:
  `cron.create` / `cron.modify` / `cron.delete` (EXPLICIT tier), `cron.list` (IMPLICIT).
  Mirrors Hermes's compressed-action design to avoid schema bloat.
- **`opencomputer/cli_cron.py`** — new `opencomputer cron {list,create,get,pause,resume,run,remove,tick,daemon,status}`
  subcommand group. `--skill` is the preferred entry path; `--prompt` triggers the threat scan.
  `--yolo` disables `plan_mode` (use with caution). `cron daemon` is a standalone scheduler that
  runs even when the gateway isn't up.
- **`croniter>=2.0`** added to `pyproject.toml::dependencies`.
- **93 new tests** across `tests/test_cron_{threats,jobs,scheduler,tool}.py` — schedule parsing,
  threat patterns (12 pattern types + 6 invisible chars), CRUD, profile isolation, secure
  permissions (0700 dirs / 0600 files), file-lock semantics, tick integration with mocked
  AgentLoop, runtime threat re-scan, `[SILENT]` marker handling, capability-claim shapes.

### Refactored (Phase A4 — F7 OI interweaving, PR-3 of 2026-04-25 Hermes parity plan)

- **`extensions/coding-harness/oi_bridge/`** — 23 OI tools (5 tiers) moved from the standalone
  `extensions/oi-capability/` plugin into the coding-harness as a bridge layer, per
  `docs/f7/interweaving-plan.md`. Tools are registered via `extensions/coding-harness/plugin.py`
  with a try/except guard (registration failure skips silently).
- **ConsentGate wiring** — All 23 tool classes now declare `capability_claims` (F1 pattern); the
  gate enforces at dispatch. `# CONSENT_HOOK` / `# AUDIT_HOOK` markers replaced. `# SANDBOX_HOOK`
  markers retained as pending-3.E-API-match comments in Tier 4-5 tools (7 tools).
- **Tests renamed** — 10 `tests/test_oi_*.py` → `tests/test_coding_harness_oi_*.py`. Imports updated
  to `extensions.coding_harness.oi_bridge.*`. AGPL CI guard path updated to new location.
- **conftest.py** — Added `extensions.coding_harness` alias (mirrors oi_capability pattern).
- **Compat shim** — `extensions/oi-capability/` is now a deprecated stub with DeprecationWarning;
  `plugin.json` marked deprecated; `plugin.py` is a no-op register stub.
- `docs/f7/README.md`, `docs/f7/design.md` (§16 added), `docs/parallel-sessions.md` updated.

### Added (Phase A3 — F6 OpenCLI Phase 4 wiring, PR-2 of 2026-04-25 Hermes parity plan)

- **`CapabilityClaim` on each C2 tool** — `ScrapeRawTool`, `FetchProfileTool`, and `MonitorPageTool` each declare a `capability_claims: ClassVar[tuple[CapabilityClaim, ...]]` with a `ConsentTier.EXPLICIT` claim namespaced under `opencli_scraper.*`. The agent loop's F1 ConsentGate enforces these claims at dispatch time before any tool executes — plugins do NOT call `ConsentGate.require()` themselves; the gate is invoked automatically by AgentLoop (see §F1 architecture in `opencomputer/agent/consent/`).
- **F2 bus publish in `_execute_scrape`** — every successful scrape now publishes a `WebObservationEvent` to `default_bus` (metadata-only: `url`, `domain`, `content_kind`, `payload_size_bytes`, `source="opencli-scraper"`, adapter name in `metadata`). Publish is best-effort: bus failure is caught, logged at WARNING, and never breaks the tool's `ToolResult` return.
- **`plugin.py::register()` wired** — the "awaiting Phase 4" early-return stub is replaced with real registration: constructs one shared `OpenCLIWrapper`, `RateLimiter`, `RobotsCache` and calls `api.register_tool()` for all 3 tools. Tool classes are loaded under a qualified `extensions.opencli_scraper.tools` sys.modules key to prevent name shadowing against other plugins' `tools/` packages.
- **`plugin.json` unchanged** — `enabled_by_default: false` STAYS until the user completes legal review.
- **11 new tests** in `tests/test_opencli_consent_integration.py` — capability claim shape, bus publish on success (both `FetchProfileTool` and `ScrapeRawTool`), bus failure isolation, manifest still-disabled check, register() call count + tool name verification.
### Added (Phase 3.D — Temporal Decay + Drift Detection, F5 layer)

- **`plugin_sdk/decay.py`** — public `DecayConfig` + `DriftConfig` + `DriftReport` dataclasses.
- **`opencomputer/user_model/decay.py::DecayEngine`** — exponential decay with per-edge-kind half-life (asserts 30d, contradicts 14d, supersedes 60d, derives_from 21d). `compute_recency_weight` applies `0.5^(age/half_life)` floored at `min_recency_weight`. `apply_decay` walks the edge table and persists via 3.C's `UserModelStore.update_edge_recency_weight`.
- **`opencomputer/user_model/drift.py::DriftDetector`** — symmetrized KL divergence between recent (default 7d) and lifetime motif distributions (from 3.B `MotifStore`), with Laplace smoothing. Returns `DriftReport` with `per_kind_drift`, `top_changes`, and a `significant` flag.
- **`opencomputer/user_model/drift_store.py::DriftStore`** — SQLite-backed report archive at `<profile_home>/user_model/drift_reports.sqlite` with retention helper.
- **`opencomputer/user_model/scheduler.py::DecayDriftScheduler`** — bus-attached background runner; throttles decay + drift to `decay_interval_seconds` / `drift_interval_seconds` (default daily). Heavy work in daemon thread; never blocks the bus.
- **`opencomputer user-model {decay run, drift detect, drift list, drift show}` CLI** — manual triggers + visibility.
- **Phase 3 complete**: 3.A bus + 3.B inference + 3.C graph + 3.D decay/drift form the F2/F4/F5 user-intelligence stack.

### Added (Phase 3.C — User-model graph + context weighting, F4 layer)

- **`plugin_sdk/user_model.py`** — public `Node`, `Edge`, `UserModelQuery`, `UserModelSnapshot` dataclasses + `NodeKind` / `EdgeKind` literals.
- **`opencomputer/user_model/store.py::UserModelStore`** — SQLite at `<profile_home>/user_model/graph.sqlite` with `nodes` + `edges` + `nodes_fts` (FTS5 with porter+unicode61 tokenizer), idempotent migrations, WAL+retry-jitter.
- **`opencomputer/user_model/importer.py::MotifImporter`** — converts 3.B `Motif` records into nodes+edges. Temporal → attribute+preference; transition → two attributes + derives_from; implicit_goal → goal + per-top-tool attribute.
- **`opencomputer/user_model/context.py::ContextRanker`** — scores candidate nodes via `salience × confidence × recency × source_reliability`; top-K cap with optional token budget; returns `UserModelSnapshot`.
- **`opencomputer user-model {nodes,edges,search,import-motifs,context}` CLI** — visibility + manual import + ranked retrieval.
- **Phase 3.D dependency**: `UserModelStore.update_edge_recency_weight` is the write API decay/drift will use.

### Added (Phase 3.B — Behavioral Inference engine, F2 continued)

- **`plugin_sdk/inference.py`** — public `Motif` dataclass + `MotifExtractor` protocol.
- **`opencomputer/inference/extractors/`** — three extractors:
  - `TemporalMotifExtractor` — bucket-by-(hour,weekday) recurring usage detector
  - `TransitionChainExtractor` — 5-minute window adjacent-event transition counter
  - `ImplicitGoalExtractor` — top-N tool sequence summarizer per session (heuristic; future LLM-judge swap-in)
- **`opencomputer/inference/storage.py::MotifStore`** — SQLite-backed motif CRUD at `<profile_home>/inference/motifs.sqlite`. WAL + retry-jitter pattern.
- **`opencomputer/inference/engine.py::BehavioralInferenceEngine`** — attaches to F2 default_bus; buffers events; runs extractors when batch_size or batch_seconds threshold reached; persists motifs.
- **`opencomputer inference motifs {list,stats,prune,run}` CLI** — visibility + manual flush + retention.
- **Phase 3.C dependency**: `MotifStore.list(kind=...)` is the read API the user-model graph will consume.

### Added (Phase 3.F — OS feature flag + invisible-by-default UI)

- **`FullSystemControlConfig`** in `opencomputer/agent/config.py` — typed knob (`enabled`, `log_path`, `menu_bar_indicator`, `json_log_max_size_bytes`); composed into top-level `Config` as `system_control` field. Defaults to disabled — invisible until the user opts in.
- **`opencomputer/system_control/logger.py::StructuredAgentLogger`** — one-JSON-line-per-call append-only log at `~/.opencomputer/<profile>/home/agent.log`. Includes pid + timestamp; rotates to `.log.old` past `max_size_bytes`; OSError-tolerant (never breaks the agent).
- **`opencomputer/system_control/bus_listener.py::attach_to_bus`** — subscribes the structured logger to `default_bus` for ALL events when system-control is on. Detachable via the returned `Subscription`.
- **`opencomputer system-control {enable,disable,status}` CLI** — visible state toggle. `enable --menu-bar` activates a macOS rumps indicator (best-effort; soft-deps on the optional `rumps` extra).
- **`pyproject.toml`** — new optional extra `[project.optional-dependencies] menubar = ["rumps>=0.4.0; platform_system == 'Darwin'"]`.
- **Hard-decoupled from F1 consent**: F1 gates individual capabilities; 3.F gates the autonomous-mode personality. Both are required for autonomous tool execution.

### Added (Phase 3.E — Pluggable Sandbox Strategy)

- **`plugin_sdk/sandbox.py`** — `SandboxStrategy` ABC + `SandboxConfig` + `SandboxResult` + `SandboxUnavailable` public types.
- **`opencomputer/sandbox/`** — concrete `MacOSSandboxExecStrategy` (sandbox-exec), `LinuxBwrapStrategy` (bwrap), `DockerStrategy` (docker run), `NoneSandboxStrategy` (opt-out), `auto_strategy()` picks the best available for the host.
- **`opencomputer/sandbox/runner.py::run_sandboxed`** — one-call async helper used by tools that need containment.
- **`opencomputer sandbox status / run / explain` CLI** — visibility + dry-run + invocation.
- **Future F7 wiring**: Session C's OI bridge will route OI's bash + arbitrary-shell tools through `run_sandboxed` per `docs/f7/design.md`. Phase 3.E ships only the primitive — wiring lands in Phase 5 OI integration.

### Added (Phase B3 — Evolution trajectory auto-collection via TypedEvent bus, parallel Session B)

- **`opencomputer/evolution/trajectory.py::register_with_bus`** — subscribes to Session A's F2 TypedEvent bus (`opencomputer.ingestion.bus.default_bus`, landed in 3.A) for `"tool_call"` events. Each `ToolCallEvent` is converted to a `TrajectoryEvent` and accumulated into an in-memory open trajectory keyed by `session_id`. **Exception-isolated** — any handler exception is logged but never propagates to the bus's other subscribers (defense in depth on top of bus's own per-subscriber try/except).
- **Privacy-preserving event conversion** — only tool_name + outcome + a small subset of metadata (with the design doc §4.1 200-char filter applied) are stored. Raw prompt text from `event.metadata` is dropped if it would violate the trajectory privacy rule. `session_id=None` events are dropped silently (cannot bucket anonymously).
- **`_on_session_end(session_id)`** — persists the open trajectory to SQLite via `insert_record`, computes reward via the B1 `RuleBasedRewardFunction`, and updates `reward_score`. Returns the inserted row id. Also exception-isolated.
- **Auto-collection flag** — `<_home() / "evolution" / "enabled">` file marker. `is_collection_enabled()` reads it; `set_collection_enabled(bool)` toggles it; `bootstrap_if_enabled()` is the startup-time helper that auto-registers the subscriber when the flag is set. (Wiring `bootstrap_if_enabled()` into AgentLoop startup is Session A's call — it lives in their reserved `agent/loop.py` territory; for now users invoke it manually or via the new `enable` CLI.)
- **CLI extensions** in `opencomputer/evolution/cli.py`: new `trajectories` subapp with `show [--limit 50]`; top-level `enable` (creates flag + registers subscriber in current process) and `disable` (removes flag; existing trajectories preserved).
- **16 new tests** across 2 files (`tests/test_evolution_b3_{subscriber,cli}.py`). Full suite: **1860 passing** (was 1844 entering B3). Ruff clean.
- **Plan reference**: `~/.claude/plans/hermes-self-evolution-plan.md` §B3 — completes the Session B plan (B1-B4 all merged + B3 now ships).

### Added (Phase C4 — F6 OpenCLI use-case libraries, parallel Session C)

- **`extensions/oi-capability/use_cases/` library** — 8 domain-specific function libraries that compose the C3 OI tools (23 across 5 tiers) into higher-level patterns. **NOT registered as tools** — these are helper APIs callable from tests, Session A's eventual Phase 5 wiring (interweaving plan), or user code:
  - `autonomous_refactor.py` — `plan_refactor` (uses `search_files` Tier 1 to find candidates), `execute_refactor_dry_run` (uses `read_file_region` + simulates edits), `execute_refactor` (REQUIRES `confirm=True` else raises ValueError; calls `edit_file` Tier 4 for each planned change). **Module docstring marks integration with `extensions/coding-harness/*` as Session A's Phase 5 scope** per `docs/f7/interweaving-plan.md`.
  - `life_admin.py` — `upcoming_events`, `todays_schedule`, `find_free_slots` (09:00–18:00 working window, merges overlapping busy blocks via `list_calendar_events` Tier 2)
  - `personal_knowledge_management.py` — `index_recent_notes` (filters .md/.txt/.org via `list_recent_files` Tier 1), `search_notes` (uses `search_files` Tier 1), `extract_action_items` (regex for unchecked checkboxes + inline TODOs)
  - `proactive_security_monitoring.py` — `SUSPICIOUS_PROCESSES` + `SUSPICIOUS_DOMAINS` frozensets; `scan_processes` (uses `list_running_processes` Tier 5); `check_recent_browser_history` (uses `read_browser_history` Tier 3); `sweep` (combined report)
  - `dev_flow_assistant.py` — `morning_standup` (composes 3 calls: `read_git_log` + `list_recent_files` + `read_email_metadata`), `eod_summary`, `detect_focus_distractions` (`list_app_usage` count threshold)
  - `email_triage.py` — `classify_emails` (5 buckets: urgent/newsletters/personal/work/other based on sender + subject heuristics); `generate_draft_response` (template-based stub, NEVER calls send_email — drafts only)
  - `context_aware_code_suggestions.py` — `gather_code_context` (target + N neighbor files), `git_blame_context` (inline `git blame` subprocess, porcelain parse). **Module docstring notes Phase 5 coding-harness integration scope.**
  - `temporal_pattern_recognition.py` — `daily_activity_heatmap` (7-day × 24-hour dict), `commit_cadence` (daily/weekday/weekend avg + longest streak), `meeting_density` (per-week avg + longest meeting-free block hours)
- **`tests/conftest.py`** — single-line addition: `"use_cases"` added to the sub-package alias loop so `extensions.oi_capability.use_cases.X` resolves correctly.
- **85 new tests** across 8 files (`tests/test_oi_use_cases_*.py`). Full suite: **1819 passing** (was 1734 entering C5). Ruff clean.
- **AGPL boundary holds** — these use-cases never `import interpreter`; they only compose tool wrappers (which themselves only call into the subprocess via JSON-RPC). C3's CI guard verifies.

### Added (Phase 3.A — Signal Normalizer + TypedEvent bus, F2 foundation)

- **`plugin_sdk/ingestion.py`** — public typed-event hierarchy for the shared pub/sub bus. `SignalEvent` base (frozen+slots, `event_id` UUID4 / `event_type` discriminator / `timestamp` / `session_id` / `source` / `metadata`) plus 5 concrete subclasses: `ToolCallEvent`, `WebObservationEvent`, `FileObservationEvent`, `MessageSignalEvent`, `HookSignalEvent`. Plus `SignalNormalizer` ABC, `IdentityNormalizer` pass-through, and a module-level normalizer registry (`register_normalizer` / `get_normalizer` / `clear_normalizers`). The two `*SignalEvent` names avoid shadowing the unrelated `MessageEvent` / `HookEvent` symbols already in `plugin_sdk.core` / `plugin_sdk.hooks` — discriminator strings (`"message"`, `"hook"`) are unaffected.
- **`opencomputer/ingestion/bus.py`** — `TypedEventBus` with sync `publish` + async `apublish`, type-discriminator + glob-pattern subscribers, exception-isolated fanout (one bad subscriber cannot poison others — logs WARNING + continues), bounded queue + drop-oldest backpressure (default `maxlen=10000`, throttled WARN + `dropped_count` counter), thread-safe subscriber list (snapshot-on-publish), per-subscription `BackpressurePolicy.{block,drop,log_and_drop}`, plus a module-level `default_bus` singleton + `get_default_bus()` / `reset_default_bus()` helpers. In-memory only at this stage — Phase 3.D may add SQLite persistence.
- **`AgentLoop._dispatch_tool_calls`** publishes a `ToolCallEvent` after each tool invocation (via the new `_emit_tool_call_event` helper). Outcomes mapped: `success` (clean ToolResult) / `failure` (`is_error=True` or raised exception) / `blocked` (consent gate, PreToolUse hook block, or allowlist refusal) / `cancelled` (asyncio cancellation). Sync, exception-isolated; a broken bus never breaks the loop.
- **Documentation** — `docs/sdk-reference.md` extended with a new "Ingestion / Signal bus" section covering every new export. `docs/parallel-sessions.md` "Bus API change log" entry announcing initial bus shipping.
- **Session B unblocked**: B3 (the trajectory subscriber, parked since Session B's worktree shipped because the bus didn't exist on `main`) can now subscribe directly to `default_bus.subscribe("tool_call", ...)`.
- **Tests**: 35 new across 3 files (`test_typed_event_bus.py` 22 / `test_signal_normalizer.py` 8 / `test_loop_emits_bus_events.py` 5). Full suite at 1734 passing (was 1699 entering 3.A). Ruff clean.

### Added (Phase C3 — F7 Open Interpreter capability plugin skeleton, parallel Session C)

- **`extensions/oi-capability/` plugin scaffold** — wraps upstream Open Interpreter (AGPL v3) via strict subprocess isolation. Per `docs/f7/design.md`. **Tools NOT registered yet** — plugin.py stub returns early; Session A wires consent + sandbox + AuditLog and **refactors the entire plugin into `extensions/coding-harness/oi_bridge/`** in Phase 5 per `docs/f7/interweaving-plan.md`.
- **AGPL boundary discipline (load-bearing)** — `import interpreter` appears in exactly ONE file: `extensions/oi-capability/subprocess/server.py` (the in-venv server script). New CI test `tests/test_oi_agpl_boundary.py` greps the entire codebase outside that allowed path and fails the build on any match. 3 tests; passes with zero forbidden imports.
- **Telemetry kill-switch** (`subprocess/telemetry_disable.py`) — patches `sys.modules["interpreter.core.utils.telemetry"]` with a `_NoopModule` BEFORE any OI import. Plus `disable_litellm_telemetry()` toggles `litellm.telemetry = False` + calls `litellm._turn_off_message_logging()`. Verified by `tests/test_oi_telemetry_disable.py` which patches `requests.post` with a fail-loudly assertion.
- **JSON-RPC subprocess protocol** (`subprocess/{protocol,wrapper,server}.py`) — frozen+slots dataclasses for request/response/error; standard JSON-RPC error codes (-32700 parse, -32600 invalid request, -32601 method not found, -32602 invalid params, -32603 internal) plus app codes (-32000 consent_denied, -32001 sandbox_violation, -32002 timeout, -32003 tool_not_found). Wrapper reads `\n`-delimited JSON from subprocess stdout; correlation-id matched; per-call timeout with kill-on-timeout; auto-respawn on dead subprocess; resource limit (4 GB RAM cap on Unix); stderr → `<_home() / "oi_capability" / "subprocess.log">`.
- **Lazy venv bootstrap** (`subprocess/venv_bootstrap.py`) — creates `<_home() / "oi_capability" / "venv">` on first use with minimal `requirements.txt` (pinned `OI_VERSION = "0.4.3"`; NO torch / opencv / sentence-transformers — saves ~500 MB on Apple Silicon). Idempotent; `OPENCOMPUTER_OI_VERSION` env override.
- **23 tools across 5 risk tiers** with constructor-injection consent / sandbox / audit hooks (pre-declared `# CONSENT_HOOK` / `# SANDBOX_HOOK` / `# AUDIT_HOOK` markers per `docs/f7/interweaving-plan.md` so Phase 5 refactor is mechanical):
  - **Tier 1 introspection** (8 tools, read-only): read_file_region, list_app_usage, read_clipboard_once, screenshot, extract_screen_text, list_recent_files, search_files, read_git_log
  - **Tier 2 communication** (5 tools, drafts-only writes): read_email_metadata, read_email_bodies, list_calendar_events, read_contacts, send_email
  - **Tier 3 browser** (3 tools): read_browser_history, read_browser_bookmarks, read_browser_dom
  - **Tier 4 system control** (4 mutating tools, per-action consent in Phase 5): edit_file, run_shell, run_applescript, inject_keyboard
  - **Tier 5 advanced** (3 tools): extract_selected_text, list_running_processes, read_sms_messages
- **`read_git_log` carve-out** — implemented INLINE via `git log` shell call, NOT routed through OI subprocess (per F7 design §11.4 refinement — zero AGPL exposure for a trivially-implementable tool).
- **Drafts-only `send_email` enforcement** — wrapper raises `ValueError` on `send_now=True`. Test verifies. Email goes to draft folder only; user sends from their email client.
- **`tests/conftest.py` (new)** — handles hyphenated extension directory names (`extensions/oi-capability/` → importable as `extensions.oi_capability`) by registering module aliases in `sys.modules` before test collection. Affects all tests but is purely additive (no existing test affected).
- **162 new tests** across 10 files. Full suite: **1604 passing** (was 1442 entering C3). Ruff clean. AGPL boundary test passes with 0 forbidden imports detected.
- **`extensions/oi-capability/LICENSE`** is MIT (matches OpenComputer); the OI subprocess venv contains AGPL-licensed open-interpreter, isolated by the boundary. **`NOTICE`** explains the AGPL isolation strategy.

> **Note**: Session A's Sub-project F1 (consent layer + audit log) shipped its own `test_sub_f1_license_boundary.py` AGPL-grep test independently of our `test_oi_agpl_boundary.py`. Both check `import interpreter` outside allowed paths; ours scopes to `extensions/oi-capability/subprocess/server.py`, theirs scopes to `opencomputer/` + `plugin_sdk/`. They are complementary — keep both for now; consolidate in a follow-up if Session A prefers.

### Added (Sub-project F1 — 2.B extensions: progressive promotions, per-resource prompts, expiry regression, audit viewer)

- **`opencomputer consent suggest-promotions`** (2.B.1) — reads `consent_counters` and lists every `(capability_id, scope_filter)` where `clean_run_count >= 10` AND the active grant is still EXPLICIT (Tier 2). Renders a Rich table (capability_id / scope / clean_run_count / current tier / suggested tier) plus a one-line hint pointing at `opencomputer consent grant ... --tier 1`. Adds a `--auto-accept` flag that upgrades each candidate to IMPLICIT in place and writes a `promote` audit row with `actor=progressive_auto_promoter`, `reason=clean_run_count>=10`. Promoted grants are stored with `granted_by="promoted"` (matches the `Literal` in `plugin_sdk/consent.py`). 3 new tests in `tests/test_sub_f1_suggest_promotions.py`.
- **Per-resource consent prompts** (2.B.2) — `ConsentGate.render_prompt` + module-level `render_prompt_message(claim, scope)` helper. When a scope has been extracted from the tool call (path / file / url / etc., via the existing `_extract_scope` heuristic in `agent/loop.py`), the prompt names the resource: `"Allow read_files.metadata on /Users/saksham/Projects/foo.py? [y/N/always]"`. Falls back to the generic `"Allow <cap>? [y/N/always]"` when no scope is available. The scope-aware string is also folded into `ConsentDecision.reason` on deny so wire/TUI clients surface the specific resource without having to re-render the prompt. Two new tests appended to `tests/test_sub_f1_consent_gate.py`.
- **Consent-expiry mid-turn regression** (2.B.3) — added `test_grant_expiry_is_rechecked_per_call` to `tests/test_sub_f1_consent_gate.py` to lock in `ConsentStore.get`'s read-time expiry filter (verified working). Seeds a 1s-TTL grant, calls the gate, sleeps past expiry, calls again — second call must deny with "no grant for capability". No production code change; the regression test prevents a future refactor from silently breaking expiry enforcement.
- **`opencomputer audit show / verify`** (2.B.4) — new Typer subapp at `opencomputer/cli_audit.py` registered next to `consent` in `opencomputer/cli.py`. `audit show` filters by `--tool` (regex over capability_id), `--since` (ISO-8601 OR relative `7d`/`24h`/`30m`), `--decision`, `--session`, `--limit`, with `--json` for machine-readable output. Backed by new `AuditLogger.query(...)` method (returns dict rows). `audit verify` is a thin wrapper around new `AuditLogger.verify_chain_detailed()` that returns `(ok, n)` and prints `"Chain intact (N rows verified)"` on success or `"Chain broken at row K"` + non-zero exit on failure — same underlying check as `consent verify-chain`, lives under `audit` because users intuit it belongs there. 7 new tests in `tests/test_sub_f1_cli_audit.py`.

### Added (Sub-project F1 — Consent layer + audit log)

- **Core consent layer** (`opencomputer.agent.consent`) — non-bypassable. Lives in core (NOT in `extensions/`) because plugins can be disabled; a disable-able consent plugin would silently bypass the security boundary. The gate is invoked by `AgentLoop._dispatch_tool_calls` BEFORE any `PreToolUse` hook fires — plugin-authored hooks cannot pre-empt it.
- **Four-tier consent model** — `ConsentTier.IMPLICIT / EXPLICIT / PER_ACTION / DELEGATED` (`plugin_sdk/consent.py`). Plus `CapabilityClaim`, `ConsentGrant`, `ConsentDecision` frozen dataclasses, re-exported from `plugin_sdk.__init__`.
- **BaseTool.capability_claims** — new `ClassVar[tuple[CapabilityClaim, ...]]` attribute. Tools declare what they need; default empty (no gate check). F1 ships the infrastructure; F2+ attaches claims to real tools (read_files.metadata etc.).
- **Schema migration framework** — `apply_migrations()` in `opencomputer.agent.state`. Ordered migrations `(0,1) → (1,2) → (2,3)`; v1→v2 adds II.6 `reasoning_details` + `codex_reasoning_items` columns on `messages`; v2→v3 adds `consent_grants`, `consent_counters`, `audit_log` tables. Bumps `SCHEMA_VERSION = 3`. Idempotent. Existing DBs upgrade without data loss.
- **Append-only `audit_log` table** — SQLite triggers block `UPDATE`/`DELETE` at the engine level (tamper-evident, not tamper-proof). HMAC-SHA256 chain over `(prev_hmac ‖ canonicalized row)` catches FS-level tampering via `AuditLogger.verify_chain()`.
- **`ConsentStore`** — SQLite-backed grant CRUD. Uses delete-then-insert (not `INSERT OR REPLACE`) because SQLite allows multiple NULLs in a PK column. Expiry enforced at read time.
- **`AuditLogger`** — HMAC-SHA256 chain + `export_chain_head()` / `import_chain_head()` for user-side backup + `restart_chain()` for post-keyring-wipe recovery.
- **`ProgressivePromoter`** — tracks clean vs dirty runs per `(capability, scope)`. N=10 default (high trust, per user preference). Offers Tier-2 → Tier-1 promotion at threshold; dirty run resets counter.
- **`BypassManager`** — `OPENCOMPUTER_CONSENT_BYPASS=1` env flag for unbricking a broken gate. Banner rendered on every prompt while active.
- **`KeyringAdapter`** — wraps `keyring` with graceful file-based fallback for environments without D-Bus/Keychain (CI, headless SSH, minimal Docker). Warns on fallback.
- **`opencomputer consent` CLI** — `list / grant / revoke / history / verify-chain / export-chain-head / import-chain-head / bypass`. Default grant expiry: 30 days. `--expires never|session|<N>d|<N>h` overrides. Tier default: 1 (`EXPLICIT`).
- **License boundary test** (`test_sub_f1_license_boundary.py`) — grep-based check that no `interpreter` or `openinterpreter` import appears in `opencomputer/` or `plugin_sdk/`. Guards against F7's Open Interpreter subprocess wrapper regressing into a direct AGPL import.
- **~50 new tests** covering the above.

### Added (Phase C2 — F6 OpenCLI plugin skeleton, parallel Session C)

- **`extensions/opencli-scraper/` plugin scaffold** — wraps upstream OpenCLI (Apache-2.0) for safe, consented web scraping. Per `docs/f6/design.md`. **Tools NOT registered yet** — plugin.py stub returns early; Session A wires `ConsentGate.require()` + `SignalNormalizer.publish()` and flips `enabled_by_default: true` in Phase 4 of the master plan.
- **`OpenCLIWrapper`** (`wrapper.py`) — async subprocess orchestration via `asyncio.create_subprocess_exec`. **Free-port scan** in 19825-19899 with `OPENCLI_DAEMON_PORT` env override; **version check** against `MIN_OPENCLI_VERSION = "1.7.0"` (raises if too old); **encoding-safe stdout** (`errors='replace'`); **per-call timeout** with kill-on-timeout via `asyncio.wait_for`; **exit-code mapping** to typed exceptions (`OpenCLIError`, `OpenCLINetworkError`, `OpenCLIAuthError`, `OpenCLIRateLimitError`, `OpenCLITimeoutError`); **global concurrent-scrape semaphore** (cap 8 — design doc §13.4 refinement).
- **`RateLimiter`** (`rate_limiter.py`) — per-domain token bucket with `asyncio.Lock`. Conservative defaults: GitHub 60/hr, Reddit 60/min, LinkedIn 30/min, Twitter 30/min, etc., per design §7. `*` wildcard fallback at 30/60s.
- **`RobotsCache`** (`robots_cache.py`) — 24h TTL cache for robots.txt using stdlib `urllib.robotparser`. **404 → allow** (per RFC); **5xx / network error → deny** (could be deliberate block). Async fetch via `httpx.AsyncClient`. Per-domain locks prevent thundering herd.
- **`FIELD_WHITELISTS`** (`field_whitelist.py`) — per-adapter dict for all 15 curated adapters (github/reddit/linkedin/twitter/hackernews/stackoverflow/youtube/medium/bluesky/arxiv/wikipedia/producthunt + 3 reddit subcommands). `filter_output()` handles dict + list-of-dicts; **unknown adapter returns empty** (fail-closed).
- **`subprocess_bootstrap`** — `detect_opencli()` (with `npx --no-install` fallback per F6 design §13.2), `detect_chrome()` (platform-specific paths + PATH search). `BootstrapError` raised with platform-specific install instructions; **never auto-installs**.
- **3 tool classes** in `tools.py`: `ScrapeRawTool`, `FetchProfileTool`, `MonitorPageTool` — all inherit `BaseTool`, take `wrapper + rate_limiter + robots_cache` via constructor injection (mockable in tests). Shared `_execute_scrape()` enforces `rate_limit → robots_check → wrapper.run → field_whitelist.filter` order.
- **`LICENSE`** (Apache-2.0) + **`NOTICE`** for upstream OpenCLI attribution.
- **85 new tests** across 6 files (`tests/test_opencli_{wrapper,rate_limiter,robots_cache,field_whitelist,subprocess_bootstrap,tools}.py`). Full suite: **1442 passing** (was 1357 entering C2). All external deps mocked — no live network, no live `opencli` binary in CI.
- **Discrepancy flagged**: `PluginManifest.kind` is `"tool"` (singular) per `plugin_sdk/__init__.py`; this plugin's `plugin.json` uses `"tools"` (plural). Both coexist because the loader reads raw JSON without validating. Worth picking one in a follow-up — for now we stay on `"tools"` to match recent Session A plugins.

### Added (Phase C1 — F6 OpenCLI + F7 Open Interpreter deep-scans + design docs, parallel Session C)

- **F6 deep-scan** `docs/f6/opencli-source-map.md` (491 lines) — complete architecture map of the upstream OpenCLI repo (`sources/OpenCLI/`, Apache-2.0). Confirms port 19825 hardcoded with `OPENCLI_DAEMON_PORT` env override; global registry pattern via `cli({...})`; daemon + Manifest V3 Chrome extension architecture; 6 strategies (`PUBLIC`/`LOCAL`/`COOKIE`/`HEADER`/`INTERCEPT`/`UI`); 624 commands across 103+ sites with all 15 of our shortlist verified present. License analysis confirms safe for closed-source wrapper.
- **F6 design doc** `docs/f6/design.md` — wrapper architecture (subprocess invocation; rate-limiter + robots.txt cache + per-adapter field whitelist; 3 typed tools); 15-adapter shortlist with strategy classification (12 PUBLIC, 3 COOKIE — gets stricter consent); strategy → consent-prompt mapping; port-collision mitigation via free-port scan; full self-audit (5 flawed assumptions, 6 edge cases, 6 missing considerations + refinements applied) + adversarial review (3 alternatives compared, 4 hidden assumptions surfaced, 5 worst-case edges).
- **F6 user README** `docs/f6/README.md` — privacy posture, safety guarantees (9 enumerated), phase status, setup flow (post-Phase-4), FAQ.
- **F7 deep-scan** `docs/f7/oi-source-map.md` (578 lines) — complete capability map of upstream Open Interpreter (`sources/open-interpreter/`, **AGPL v3 confirmed**). 15 capability modules under `interpreter/core/computer/` + modern `computer_use/` Anthropic-style tools; PostHog telemetry hardcoded at `interpreter/core/utils/telemetry.py:52` with API key exposed; tier-by-tier risk classification of all 23 curated tools; subprocess concerns documented.
- **F7 design doc** `docs/f7/design.md` — AGPL boundary discipline (subprocess-only + CI lint test); subprocess + JSON-RPC architecture; telemetry kill-switch via `sys.modules` patch BEFORE any OI import + network egress block as belt-and-suspenders; 23-tool surface across 5 risk tiers; per-tier consent surface design; venv bootstrap with version-pinned minimal deps; full self-audit (6 flawed assumptions, 8 edge cases, 6 missing considerations + refinements applied) + adversarial review (4 alternatives compared, 5 hidden assumptions surfaced, 5 worst-case edges).
- **F7 user README** `docs/f7/README.md` — 5-tier model with per-tier consent surface; AGPL boundary explanation; safety guarantees (10 enumerated); phase status; setup; FAQ.
- **F7 interweaving plan** `docs/f7/interweaving-plan.md` — explicit Phase 5 refactor contract for Session A: how `extensions/oi-capability/` (standalone in C3) becomes `extensions/coding-harness/oi_bridge/` mechanically (move files; replace `# CONSENT_HOOK` / `# SANDBOX_HOOK` / `# AUDIT_HOOK` markers with real calls; register through coding-harness plugin.py). Pre-declared extension points + class-based constructor injection make the refactor trivial. Three load-bearing C3 design choices justified.
- **`docs/parallel-sessions.md`** — added Session C reserved-files block + Session C "active working" entry. Reserved: `extensions/opencli-scraper/*`, `extensions/oi-capability/*`, `tests/test_opencli_*.py`, `tests/test_oi_*.py`, `docs/f6/*`, `docs/f7/*`.

C1 is **docs only** — no code, no tests, no plugin scaffolding (those are C2/C3). All design choices include explicit self-audit + adversarial-review sections per the project's planning convention.

### Added (Phase B4 — Prompt evolution + monitoring dashboard + atrophy detection, parallel Session B)

- **Migration `002_evolution_b4_tables.sql`** — adds three new tables to the evolution DB: `reflections` (track each `reflect()` invocation: timestamp, window_size, records_count, insights_count, records_hash, cache_hit), `skill_invocations` (atrophy data: slug + invoked_at + source ∈ {`manual` | `agent_loop` | `cli_promote`}), `prompt_proposals` (id + proposed_at + target ∈ {`system` | `tool_spec`} + diff_hint + insight_json + status ∈ {`pending` | `applied` | `rejected`} + decided_at + decided_reason). All with appropriate indexes. Migration is idempotent + automatic via the existing `apply_pending()` runner.
- **`PromptEvolver`** (`opencomputer/evolution/prompt_evolution.py`) — takes `Insight` with `action_type=="edit_prompt"` and persists it as a **diff-only proposal**. **Never auto-mutates a prompt file.** Writes a row to `prompt_proposals` table + atomic sidecar `<evolution_home>/prompt_proposals/<id>.diff` (via `tmp + .replace`). Validates `target` ∈ {`system`, `tool_spec`} and that `diff_hint` is non-empty. CLI: `prompts list/apply/reject` — `apply` records the user decision but does NOT edit prompt files (caller's responsibility — by design). `PromptProposal` is a frozen+slots dataclass mirroring DB rows.
- **`MonitorDashboard`** (`opencomputer/evolution/monitor.py`) — aggregates: total reflections + last-reflection timestamp, list of synthesized skills with invocation counts + atrophy flags, average reward score over last 30 days vs lifetime. Atrophy threshold default: 60 days no-invocation. `_iter_reward_rows()` queries `trajectory_records.reward_score` directly (option-b: keeps `TrajectoryRecord` dataclass shape stable; no breaking change for downstream consumers). CLI: `dashboard` renders two Rich tables (summary + per-skill).
- **Storage helpers** added to `opencomputer/evolution/storage.py`: `record_reflection`, `list_reflections`, `record_skill_invocation`, `list_skill_invocations`, `record_prompt_proposal`, `list_prompt_proposals`, `update_prompt_proposal_status`. All follow the existing `conn=None` lazy-open pattern.
- **CLI extensions** in `opencomputer/evolution/cli.py`: new `prompts` subapp (`list/apply/reject`), top-level `dashboard`, `skills retire` (moves to `<evolution_home>/retired/<slug>/` for audit trail; collision-safe with `-2..-N` suffixes), `skills record-invocation` (manual analog of B5+ auto-recording from agent loop). The existing `reflect` command now records a `reflections` row after each call; `skills promote` records an initial `cli_promote` invocation so promoted skills don't appear atrophied immediately.
- **Tests** — 58 new across 4 files (`tests/test_evolution_{storage_b4,prompt_evolution,monitor,cli_b4}.py`). Full suite: **1326 passing** (was 1268 entering B4). Zero edits to existing tests; zero changes to Session-A-reserved files.

**B4 design philosophy:** prompt evolution NEVER auto-applies. Atrophy detection is informational only — `skills retire` is a user-invoked move, not automatic. Together with B1+B2's quarantine-namespace design, evolution remains entirely opt-in and reversible at every step.

### Added (Phase B2 — Evolution reflection + skill synthesis + CLI, parallel Session B)

- **GEPA-style reflection engine** (`opencomputer/evolution/reflect.py`) — `ReflectionEngine.reflect(records)` renders the Jinja2 prompt (`prompts/reflect.j2`), calls the configured `BaseProvider` (via OpenComputer's plugin registry — never direct Anthropic SDK), parses JSON output, and returns a list of `Insight` objects. Defensive JSON parser strips markdown fences, skips malformed entries, filters `evidence_refs` against actual record ids (catches LLM hallucinations). Per-call cache keyed by sha256 of the record-id sequence, so dry-runs and retries don't re-bill the LLM.
- **Skill synthesizer** (`opencomputer/evolution/synthesize.py`) — `SkillSynthesizer.synthesize(insight)` writes a III.4-hierarchical skill (`SKILL.md` + optional `references/` + `examples/`) into the evolution quarantine namespace at `<profile_home>/evolution/skills/<slug>/`. **Atomic write** via `tempfile.mkdtemp` + `os.replace` — half-written skills are impossible. **Path-traversal guard** rejects reference/example names containing `/`, `\`, or leading `.` (defense against LLM payloads that try to write outside the skill dir). **Slug collision** handling: appends `-2`, `-3`, …, `-99` suffixes; never overwrites.
- **`opencomputer evolution …` CLI subapp** (`opencomputer/evolution/{entrypoint,cli}.py`) — Typer subapp wired through `entrypoint.py::evolution_app` so Session A folds it into `cli.py` in a single line (`app.add_typer(evolution_app, name="evolution")`). Until then, invoke directly via `python -m opencomputer.evolution.entrypoint <subcommand>`. Commands:
  - `reflect [--window 30] [--dry-run] [--model claude-opus-4-7]` — manual reflection trigger; `--dry-run` shows the trajectory table without an LLM call.
  - `skills list` — Rich table of synthesized skills + their description.
  - `skills promote <slug> [--force]` — copy from quarantine to user's main skills dir; refuses overwrite without `--force`.
  - `reset [--yes]` — delete the entire evolution dir (DB + quarantine + future prompt-proposals); confirms before wiping unless `--yes`. **Session DB and main skills are untouched.**
- **Jinja2 prompt templates** (`opencomputer/evolution/prompts/{reflect,synthesize}.j2`) — `reflect.j2` renders trajectory batches into a single LLM prompt asking for high-confidence Insight extraction (system framing emphasizes conservatism; output schema is JSON-only with payload contracts documented inline). `synthesize.j2` renders SKILL.md with YAML frontmatter, the `<!-- generated-by: opencomputer-evolution -->` quarantine marker, and traceability comments (slug, confidence, evidence-refs).
- **Tests** — 36 new (`tests/test_evolution_{reflect_template,reflect_engine,synthesize_skill,cli}.py`); 1 obsolete stub-behavior test removed; full suite at 1070 passing across 60 test files (was 1058 entering B2). **Zero edits to existing test files**; no Session-A-reserved file touched.

### Added (Phase B1 — Evolution subpackage skeleton, parallel Session B)

- **`opencomputer/evolution/` subpackage** — self-contained scaffold for GEPA-style self-improvement (trajectory collection → reflection → skill synthesis). **Opt-in** by design (`config.evolution.enabled` defaults to `False`); nothing runs unless invoked. See `docs/evolution/README.md` (user-facing) and `docs/evolution/design.md` (architecture).
- **Trajectory dataclasses** (`evolution/trajectory.py`) — `TrajectoryEvent` and `TrajectoryRecord` (frozen+slots). Privacy-first: `metadata` string values >200 chars are rejected at construction time, so raw prompt text can never leak into the evolution store. Helpers `new_event` / `new_record` / `with_event` for ergonomic immutable-append flow.
- **SQLite storage with self-contained migration runner** (`evolution/storage.py` + `evolution/migrations/001_evolution_initial.sql`) — separate DB at `<profile_home>/evolution/trajectory.sqlite` (no contention with `sessions.db`). WAL mode + retry-with-jitter, matching `agent/state.py` pattern. Migration runner tracked via `schema_version` table; documented as a temporary self-contained shim that will refactor onto Sub-project F1's framework once that lands (`# TODO(F1)` marker at top of file).
- **Rule-based reward function** (`evolution/reward.py`) — `RewardFunction` runtime-checkable Protocol + `RuleBasedRewardFunction` default. Three weighted signals (tool success rate 0.5, user-confirmed cue 0.3, completion flag 0.2). Conservative — no length component (verbose responses NOT rewarded), no latency component. LLM-judge reward explicitly post-v1.1.
- **Reflection + synthesis stubs** (`evolution/reflect.py`, `evolution/synthesize.py`) — `Insight` frozen dataclass (observation + evidence_refs + action_type + payload + confidence) + `ReflectionEngine` and `SkillSynthesizer` classes whose constructors accept the parameters B2 will need (provider, window, dest_dir) but whose work-doing methods raise `NotImplementedError("...lands in B2...")`. Public API surface locked at B1 so consumers can be wired against a stable contract today.
- **Hermes deep-scan + design doc** — `docs/evolution/source-map.md` (474-line architecture summary of the Nous Research Hermes Self-Evolution reference, MIT-licensed) + `docs/evolution/design.md` (architectural decisions, divergences from Hermes, self-audit, refactor paths).
- **Parallel-session coordination protocol** — `docs/parallel-sessions.md`: shared state file documenting reserved files (Session A vs Session B), bus-API change log, PR-review responsibilities, rollback procedure. Both sessions read at startup, update after each commit.

73 new tests (`tests/test_evolution_{trajectory,storage,reward,reflect,synthesize}.py`); zero changes to existing files (Session-A-reserved territory respected).

### Changed (pre-v1.0 stabilization — drift-preventer cleanup)

- **Consolidated plugin search-path construction.** New single source of truth: `opencomputer.plugins.discovery.standard_search_paths()`. Four call sites that previously duplicated the `profile-local → global → bundled` walk now import it: `cli._discover_plugins`, `cli.plugins` (listing command), `cli_plugin.plugin_enable`, `AgentLoop._default_search_paths`. No behavior change except for one fix — see next bullet.
- **Fix: `opencomputer plugins` listing command now honors profile-local plugins.** Previously it built its own search path that skipped the profile-local dir and ordered bundled before user-installed (wrong priority for dedup). It now matches every other plugin-walking code path. Run `opencomputer -p <name> plugins` to see a named profile's locally-installed set.

### Changed — BREAKING (pre-v1.0 tool-name renames)

Three tool-name changes landed in the pre-v1.0 window. Any existing user transcript or external integration that invoked these tools by their old names will fail at load. Post-v1.0 these would require a semver-major bump; doing them now is the right window.

- **`Diff` → `GitDiff` and `CheckpointDiff`** — two different plugins previously registered a tool named `Diff` with different semantics (`extensions/dev-tools` = git diff wrapper; `extensions/coding-harness` = unified diff vs rewind checkpoint). The collision triggered `ToolRegistry` `ValueError` when both plugins loaded in the same profile, and when they didn't, it was a latent LLM-selection bug (the model would pick the anonymous "default" Diff unpredictably). Both are now semantically precise: dev-tools ships `GitDiff`, coding-harness ships `CheckpointDiff`.
- **`start_process`, `check_output`, `kill_process` → `StartProcess`, `CheckOutput`, `KillProcess`** — the last snake_case tool names in the codebase, now aligned with the PascalCase convention every other tool uses (Edit, MultiEdit, Read, TodoWrite, Rewind, GitDiff, CheckpointDiff, RunTests, ExitPlanMode, ...). Class names (`StartProcessTool`, etc.) were already PascalCase — only the `ToolSchema.name` the LLM sees was inconsistent.

All 809 tests green across the four atomic commits.

### Added (Phase 12b1 — Honcho as default memory overlay)

- **Honcho is the default memory provider when Docker is available.** Setup wizard auto-starts the 3-container stack (api + postgres+pgvector + redis + deriver) via `bootstrap.ensure_started()` — no prompt, no opt-in. On machines without Docker, the wizard prints the install URL and persists `provider=""` so the next run doesn't retry. Baseline memory (MEMORY.md + USER.md + SQLite FTS5) stays on unconditionally.
- **`RuntimeContext.agent_context`** — typed `Literal["chat","cron","flush","review"]` = `"chat"`. `"cron"`/`"flush"` short-circuit both `MemoryBridge.prefetch` AND `sync_turn` so batch jobs don't spin the external stack. Mirrors Hermes' `sources/hermes-agent/plugins/memory/honcho/__init__.py:279-286`.
- **`HonchoSelfHostedProvider.mode`** — `Literal["context","tools","hybrid"]` = `"context"`. Validates at construction. `context` injects recall automatically; `tools` exposes Honcho as agent-facing tools; `hybrid` does both. Consumed by A5 wizard / A7 loop-wiring.
- **`bootstrap.ensure_started(timeout_s=60)`** — idempotent bring-up helper. Pre-flight Docker detection, port-collision check (only port 8000 is host-exposed), `docker compose pull --quiet`, `docker compose up -d`, health-poll every 2s until timeout. Returns `(ok, msg)`. Replaces direct `honcho_up()` in the wizard.
- **`PluginManifest.enabled_by_default: bool = False`** — new manifest field. `memory-honcho/plugin.json` sets it to `true`; other plugins preserve existing behavior. Schema + dataclass + `_parse_manifest` updated atomically per `opencomputer/plugins/CLAUDE.md`.
- **`opencomputer memory doctor`** — 5-row Rich table reporting the state of every memory layer (baseline / episodic / docker / honcho / provider). Diagnostic, always exits 0. Complements `memory setup` / `status` / `reset`.
- **AgentLoop wires MemoryBridge at last** — `run_conversation` now calls `memory_bridge.prefetch(user_message, turn_start_index, runtime)` after appending the user message + before the tool loop, and `memory_bridge.sync_turn(user, assistant, turn_index, runtime)` on END_TURN (same site as the Phase 12a reviewer spawn). Prefetch output is appended to the per-turn `system` variable as `"## Relevant memory"`; the frozen `_prompt_snapshots[sid]` is NOT modified — preserves the prefix-cache invariant. The cron/flush guard from A1 now operates end-to-end in production.

### Added (Phase 14 — multi-profile support)

- **Per-profile directories + `-p` flag routing** (14.A). `_apply_profile_override()` in `opencomputer/cli.py` intercepts `-p` / `--profile=<name>` / `--profile <name>` from `sys.argv` BEFORE any `opencomputer.*` import, sets `OPENCOMPUTER_HOME`, and all downstream `_home()` consumers resolve to the active profile's directory automatically. 14.M/14.N code becomes profile-aware with zero changes.
- **Sticky active profile** at `~/.opencomputer/active_profile` (one-line file). `opencomputer profile use <name>` writes it; `opencomputer profile use default` unlinks.
- **Pre-import explicit-flag wins over parent env** — a `-p coder` always overrides `OPENCOMPUTER_HOME` even if a parent shell exported it. Guard on sticky-file read only, not on the explicit-flag write.
- **`opencomputer profile` CLI** (14.B) — `list`, `create`, `use`, `delete`, `rename`, `path`. Create supports `--clone-from <other>` (config-only) and `--clone-all` (full recursive state copy). Rename warns about Honcho continuity loss. Delete clears sticky if the deleted profile was active.
- **Plugin manifest scoping** (14.C) — `PluginManifest` gains `profiles: tuple[str, ...] | None = None` (omit or `["*"]` = any profile; concrete list = restricted) and `single_instance: bool = False`. Manifest validator accepts both plus `schema_version`. `opencomputer/plugins/discovery.py` populates the new fields from `plugin.json`.
- **Manifest-layer enforcement in loader** (14.D) — Layer A: `_manifest_allows_profile()` in `opencomputer/plugins/registry.py` gates loading by the plugin's declared compatibility. Composes with the existing Layer B enabled-ids filter (both must pass). Skips log at INFO with profile + reason for diagnostics.
- **Profile-local plugin directory** (14.E) — `~/.opencomputer/profiles/<name>/plugins/`. Discovery scans in priority order: profile-local → global (`~/.opencomputer/plugins/`) → bundled (`extensions/`). Profile-local shadows global shadows bundled on id collision.
- **`opencomputer plugin` subcommand** (14.E) — `install`, `uninstall`, `where`. `install <path>` defaults to the active profile's local dir; `--global` targets the shared dir; `--profile <name>` targets a specific profile. `--force` to overwrite. `where <id>` prints the first match across the priority-ordered roots.
- **Reserved profile names** — `default`, `presets`, `wrappers`, `plugins`, `profiles`, `skills` rejected by `validate_profile_name` (prevent subdir collisions with the root layout).
- **README Profiles + Presets + Workspace overlays + Plugin install sections** (14.L) — user-facing docs for everything above.

### Tests

- `tests/test_phase14a.py` (23 tests): validation + directory resolution + flag routing (short/long/equals forms) + sticky fallback + flag-beats-sticky + argv stripping + invalid-name fallback + parent-env override.
- `tests/test_phase14b.py` (19 tests): all seven profile CLI subcommands including clone-from/clone-all, default-name refusal, confirmation prompts, sticky-file side effects, Honcho rename warning.
- `tests/test_phase14c.py` (10 tests): dataclass defaults, manifest validator accepts profiles/single_instance/schema_version, discovery propagates fields, bundled plugins declare profiles.
- `tests/test_phase14d.py` (8 tests): manifest helper unit tests (None/wildcard/specific/empty list) + loader integration (wildcard loads anywhere, restricted skips mismatched profile, specific-match loads, Layer A + B compose correctly).
- `tests/test_phase14e.py` (11 tests): install defaults to profile-local, --global flag, --profile flag, --force overwrite, refuses existing without --force, rejects source-without-manifest; uninstall, where lookup; discovery priority (profile-local shadows global).

All 488 tests green on this branch.

### Added (Phase 10f — memory baseline completion)
- **`Memory` tool** (`opencomputer/tools/memory_tool.py`) — agent-facing
  curation of MEMORY.md + USER.md. Actions: `add`/`replace`/`remove`/`read`.
  Targets: `memory` (agent observations) / `user` (user preferences).
- **`SessionSearch` tool** (`opencomputer/tools/session_search_tool.py`) —
  agent-facing FTS5 search across all past messages. Default limit 10,
  max 50. Wraps new `SessionDB.search_messages()` returning full content.
- **USER.md support** in `MemoryManager` — separate from MEMORY.md so
  agent observations don't commingle with user preferences.
- **Atomic write pipeline** — `_write_atomic()` + `_file_lock()` (fcntl /
  msvcrt). Every mutation: acquire lock → backup to `<path>.bak` →
  write temp → `os.replace()`. Never leaves partial files.
- **Character limits** on both files, configurable via `MemoryConfig`.
  Over-limit writes raise `MemoryTooLargeError` (returned as tool error).
- **Declarative memory injected into base system prompt** (frozen per
  session) — preserves Anthropic prefix cache across turns.
  `PromptBuilder.build()` gained `declarative_memory`, `user_profile`,
  `memory_char_limit`, `user_char_limit` params.
- **`MemoryProvider` ABC** (`plugin_sdk/memory.py`) — public contract for
  external memory plugins (Honcho, Mem0, Cognee). 5 required methods,
  2 optional lifecycle hooks, cadence-aware via `turn_index`.
- **`InjectionContext.turn_index`** field (default 0, backward compatible).
- **`PluginAPI.register_memory_provider()`** with one-at-a-time guard +
  isinstance check.
- **`MemoryContext` + `MemoryBridge`** — shared deps bag + exception-safe
  orchestrator wired into `AgentLoop`. A broken provider never crashes
  the loop.
- **`opencomputer memory` CLI subcommand group** —
  `show / edit / search / stats / prune / restore` with `--user` flag.

### Changed
- `MemoryConfig` gained: `user_path`, `memory_char_limit=4000`,
  `user_char_limit=2000`, `provider=""`, `enabled=True`,
  `fallback_to_builtin=True`. Backward compatible.

### Tests
- +62 tests in `tests/test_phase10f.py`, all green.
- Full suite: 336 passing.

### Added (Layered Awareness MVP, 2026-04-26)

First-pass implementation of "agent already knows the user" via four
overlapping layers running at different cadences. Implements the MVP
of the Sub-project F vision (see `docs/superpowers/specs/2026-04-26-layered-awareness-design.md`).

- **Layer 0 — Identity Reflex.** Reads `$USER`, git config, macOS
  Contacts.app `me` card, system locale. <1s, no consent prompts.
- **Layer 1 — Quick Interview.** Five install-time questions
  (current focus, concerns, tone preference, do-not-do, free-form).
  Persisted as user-explicit user-model edges with confidence 1.0.
- **Layer 2 — Recent Context Scan.** 7-day window over files in
  `~/Documents` / `~/Desktop` / `~/Downloads`, git log across
  detected repos in `~/Vscode` / `~/Projects` / etc., calendar
  events (FDA-gated PyObjC EventKit), Chrome browser history (read
  via tempfile copy to bypass SQLite lock).
- **Layer 4 minimal — Browser Bridge.** Chrome MV3 extension +
  Python aiohttp listener at `127.0.0.1:18791`. Forwards every tab
  navigation as a `browser_visit` SignalEvent into the F2 bus.

CLI:
- `opencomputer profile bootstrap` runs Layers 0-2 sequentially.
  `--skip-interview` runs Layer 0 only. `--force` re-runs after
  the marker has been written.
- `opencomputer profile bridge token [--rotate]` prints the auth
  token used by the browser extension.
- `opencomputer profile bridge status` checks listener reachability.

Prompt builder gains a `user_facts` slot pulling top-20 nodes from
the F4 user-model graph (Identity > Goal > Preference > Attribute,
ranked by confidence, truncated to 80 chars per fact). Block omitted
if graph empty. The slot is also wired through `build_with_memory()`
so the production agent loop sees it.

F1 capability claims added: `ingestion.recent_files` (IMPLICIT,
metadata-only — scope-locked by docstring), `ingestion.git_log`
(IMPLICIT), `ingestion.calendar` (EXPLICIT), `ingestion.browser_history`
(EXPLICIT), `ingestion.messages` (EXPLICIT), `ingestion.browser_extension`
(EXPLICIT).

V2/V3/V4 of Layered Awareness (background deepening, life-event
detector, plural personas, curious companion) ship in subsequent
plans after MVP dogfood.

Spec: `docs/superpowers/specs/2026-04-26-layered-awareness-design.md`
Plan: `docs/superpowers/plans/2026-04-26-layered-awareness-mvp.md`

## [0.1.0] — 2026-04-21 (pre-alpha)

### Added
- Initial public release.
- Core agent loop with tool dispatch (`opencomputer/agent/loop.py`).
- Three-pillar memory: declarative (MEMORY.md), procedural (skills/), episodic (SQLite + FTS5 full-text search).
- 7 built-in tools: Read, Write, Bash, Grep, Glob, skill_manage, delegate.
- Strict plugin SDK boundary (`plugin_sdk/`) with manifest-first two-phase discovery.
- Bundled plugins:
  - `anthropic-provider` — Anthropic Claude models with Bearer-auth proxy support.
  - `openai-provider` — OpenAI Chat Completions + any OpenAI-compatible endpoint.
  - `telegram` — Telegram Bot API channel with typing indicators.
  - `discord` — Discord channel via discord.py.
  - `coding-harness` — Edit, MultiEdit, TodoWrite, background-process tools + plan mode.
- MCP integration — connects to Model Context Protocol servers (stdio), tools namespaced.
- Gateway for multi-channel daemons.
- Wire server — JSON over WebSocket RPC for TUI / IDE / web clients (`opencomputer wire`).
- Streaming responses (Anthropic + OpenAI) with per-turn typing indicators on Telegram.
- Dynamic injection engine — cross-cutting modes as providers (plan mode).
- Hardened context compaction — real token counts, tool-pair preservation, aux-fail fallback.
- Runtime context threading — plan_mode / yolo_mode / custom flags flow loop → hooks → delegate → subagents.
- CLI: `chat`, `gateway`, `wire`, `search`, `sessions`, `skills`, `plugins`, `setup`, `doctor`, `config`.
- Interactive setup wizard (`opencomputer setup`).
- Health check (`opencomputer doctor`).
- Typed YAML config with dotted-key get/set.
- GitHub Actions CI — pytest on Python 3.12 + 3.13, ruff lint.
- 114 tests.

### Credits
Architectural ideas synthesized from [Claude Code](https://github.com/anthropics/claude-code),
[Hermes Agent](https://github.com/NousResearch/hermes-agent),
[OpenClaw](https://github.com/openclaw/openclaw),
[Kimi CLI](https://github.com/MoonshotAI/kimi-cli).

[Unreleased]: https://github.com/sakshamzip2-sys/opencomputer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sakshamzip2-sys/opencomputer/releases/tag/v0.1.0
