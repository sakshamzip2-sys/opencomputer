# Hermes Doc-Parity Snapshot — 2026-05-08

**Source docs compared:**
1. *Hermes Agent — Quickstart & Install Reference* (install script, provider setup, first run, slash commands, layering features, update/maintenance, diagnostics, uninstall)
2. *Hermes Agent — CLI, TUI, WSL2 & Configuration Reference* (launch flags, status bar, keybindings, slash commands, busy modes, quick commands, background sessions, session management, context compression, TUI specifics, WSL2 setup, terminal backends, full `config.yaml` schema)

**OpenComputer state walked:** main tip `429c5b8f` (2026-05-08).
**Companion priors:** [`inventory.md`](inventory.md), [`2026-04-28-major-gaps.md`](2026-04-28-major-gaps.md), the un-pushed `2026-05-06-deep-comparison.md` working draft.

---

## Why this comparison was done

The user supplied both reference docs verbatim with the instruction *"implement this as well as this"*. A naive read suggests a multi-week parity port. Two facts make that read wrong:

1. OpenComputer has been a Hermes-extraction project for months. ~95% of the load-bearing surface area in these two docs is already shipped (verified by code-walk against `opencomputer/`, `extensions/`, `plugin_sdk/`, and cross-referenced to the 2026-05-06 deep-comparison draft).
2. The user explicitly course-corrected during discovery:
   > *"Only integrate something that actually makes sense. If you already have it, don't do it. If you're missing it, it doesn't mean that we should just fill it just because we're missing it. We will fill it because it makes sense. … If there's something redundant, you can probably remove it. Only and if only, you do an end-to-end check and make sure it's completely waste and unrequired, then only do that."*

This document is the result of applying that filter. It exists so the analysis doesn't have to be redone the next time someone asks "are we caught up to Hermes?"

---

## 1. Already shipped — parity ✓

Each row maps a Hermes surface from one of the two docs to the OpenComputer module that ships the equivalent.

### Quickstart / Install doc

| Hermes surface | OpenComputer equivalent |
|---|---|
| `curl … \| bash` install | `scripts/install.sh` (multi-strategy fallback: pipx → pip --user → venv at `~/.opencomputer/venv`; `--dry-run`, `--dev`, `--use-pipx`, `--no-user` flags) |
| Per-user vs root install | `install.sh` `--no-user` |
| `hermes model` interactive picker | `oc model` (`opencomputer/cli_model_picker.py`) — Hermes-exact two-space pad + arrow + literal marker UX |
| `hermes setup` wizard | `oc setup` (`opencomputer/setup_wizard.py` + `opencomputer/cli_setup/sections/`, `section_handlers/*`) — section-driven UX modeled on Hermes |
| `hermes gateway setup` | `oc gateway` (foreground) + `oc gateway --install-daemon` (cross-platform service install via `opencomputer/service/`) |
| `hermes update`, `--check`, `--backup` | `oc update` (`opencomputer/cli_update_check.py`) + `oc backup` (PR #474, full disaster-recovery CLI) |
| Post-update validation (`hermes doctor`) | `oc doctor` (`opencomputer/doctor.py`) — provider, deps, hooks, MCP health |
| `hermes sessions list` | `oc sessions` (Typer command, lists by recency with full metadata) |
| `hermes config set/get/edit/check/migrate` | `oc config` Typer group |
| `hermes login`, `hermes logout` | `oc login`, `oc logout` (`opencomputer/cli_login.py`) — Hermes-exact `getpass` flow |
| Voice mode (`pip install "hermes-agent[voice]"`) | `pip install "opencomputer[voice]"` extras + PR #199 voice mode + PR #485 wake-word |
| MCP via `mcp_servers:` config | `opencomputer/mcp/` + `cli_mcp.py` (`oc mcp` group) — deferred-load pattern |
| `hermes uninstall` | `pip uninstall opencomputer` (or `pipx uninstall`); `~/.opencomputer/` is removable independently. See §2 for why this is *not* wrapped. |
| Settings storage `~/.hermes/.env`, `config.yaml`, `auth.json`, `SOUL.md`, `memories/`, `skills/`, `cron/`, `sessions/`, `logs/` | `~/.opencomputer/<profile>/` mirror — `config.yaml`, `.env`, `auth.json`, `SOUL.md`, `memories/`, `skills/`, `cron/`, `sessions/`, `logs/` (Sub-project C) |

### CLI / TUI / Config doc

| Hermes surface | OpenComputer equivalent |
|---|---|
| `hermes` interactive | `oc` / `oc chat` |
| `hermes --tui` | `oc --tui` / `oc tui` (PRs #486 #487 — full Hermes-shape dashboard + TUI port, 12 pages, 60 routes) |
| `hermes chat -q "query"` non-interactive | `oc oneshot "query"` (same feature, different name) |
| `hermes --resume <id>` | `oc chat --resume <id>` + `oc resume` (modal picker) |
| `hermes -w` worktree mode | `oc -w` worktree mode |
| `hermes --verbose` | `oc --verbose`/`/verbose` slash |
| Status bar (model / context / cost / time, color thresholds) | Recent dashboard+TUI status bar (PR #486+#487 + thinking-card v6 PR #395) |
| `Alt+Enter` / `Ctrl+J` multiline | OC keybindings |
| `Ctrl+B` push-to-talk | Voice mode + wake-word port |
| `Ctrl+G` open in `$EDITOR` | Tier S external-editor port (PR #220+) |
| `Ctrl+C` interrupt | OC interrupt handler |
| `Tab` autocomplete slash | Slash command tab completion |
| `Alt+V` paste image | Clipboard support (`opencomputer/cli_ui/clipboard.py`) |
| `/help /tools /model /sessions /skin /voice /reasoning /title /verbose /usage /history /save /background-equivalents` | 24+ slash commands in `opencomputer/agent/slash_commands_impl/` (Tier 2.A bundle from major-gaps doc — closed) |
| `/queue` busy-mode (queue mid-run messages) | `agent/slash_commands_impl/queue_mode_cmd.py` |
| `/steer` mid-run nudge | `opencomputer/agent/steer.py` (Wave 5 T3 — Hermes commit `e27b0b765`) |
| Session management `--continue`, `--resume "title"`, `oc sessions list` | `oc sessions` + `oc resume` picker + `--resume <id-prefix>` direct |
| Context compression (`enabled`, `threshold`, `protect_last_n`, summary model) | `opencomputer/agent/compaction.py` + model-aware width dict (PR #343) + `auxiliary.compression` slot |
| TUI alternate-screen + modal overlays | TUI port |
| TUI LaTeX rendering | TUI port |
| TUI `/help /sessions /model` modal pickers | TUI port |
| `display.tool_progress: off\|new\|all\|verbose` | `agent/slash_commands_impl/display_toggles_cmd.py` runtime toggle |
| Section defaults (`thinking` expanded, `tools` expanded, etc.) | Recent thinking-card v6 + AI-Elements port (PRs #395, #406, #408) |
| Streaming (`enabled`, `transport`, `edit_interval`, `fresh_final_after_seconds`) | OC streaming + partial-stream recovery (PR #482) |
| Terminal backends — local, docker, ssh | `opencomputer/sandbox/{local,docker,ssh}.py` |
| Docker `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit 256` | `opencomputer/sandbox/docker.py` security config |
| Persistent docker container (single per process) | OC docker backend pattern |
| `auto_prune` session retention | `Config.session.auto_prune_days` / `auto_prune_untitled_days` / `auto_prune_min_messages` (Tier A4 from deep-comparison) |
| `compression.enabled`, `threshold`, `target_ratio`, `protect_last_n` | `Config.compaction` + `CompactionEngine` |
| `auxiliary.{vision, web_extract, compression, session_search, approval, triage_specifier}` slots | OC auxiliary model slots (`opencomputer/agent/aux_llm.py`) |
| `delegation.max_concurrent_children`, `max_spawn_depth`, `orchestrator_enabled` | `Config.delegation` + `tools/delegate.py` (MAX_DEPTH=2, BLOCKED_TOOLS list) |
| `web` backend (firecrawl/searxng/parallel/tavily/exa) | OC web search backend chain (PR #17) |
| `browser` config (inactivity_timeout, dialog_policy) | `extensions/browser/` |
| TTS providers (edge, elevenlabs, openai, minimax, gemini, xai) | OC voice extras (Edge default verified by `cli_setup/section_handlers/tts_provider.py`) |
| STT providers (local, groq, openai, mistral) | OC STT (groq STT in voice extras; Tier A2 from deep-comparison) |
| `display` settings (`streaming`, `show_reasoning`, `show_cost`, `bell_on_complete`, `runtime_metadata_footer`) | OC display config + `cli_ui/bell.py` (Tier 2.B XS port) |
| `streaming` (`enabled`, `transport: edit`) | OC streaming + heartbeat lane (PR #482) |
| `privacy.redact_pii` | `opencomputer/security/redact.py` (Tier 3.D port) |
| `group_sessions_per_user` | Per-user isolation in group chats (gateway) |
| `unauthorized_dm_behavior: pair\|ignore` | DM pairing (Messaging Gateway PR-1) |
| `security.redact_secrets`, `tirith_enabled`, `website_blocklist` | Tier-S security stack — `security/redact.py`, `security/tirith.py`, `security/url_safety.py` |
| `approvals.mode: manual\|smart\|off` | F1 consent layer (PR #64) — `manual` = always-prompt, `smart` = capability-claims-driven, `off` = bypass mode |
| `checkpoints.enabled`, `max_snapshots` | Coding-harness checkpoint manager — list/diff/restore (mirrors Hermes' `/rollback N` bare-integer compat) |
| Context files (`HERMES.md`, `AGENTS.md`, `CLAUDE.md`, `.cursorrules`) priority chain | `opencomputer/agent/subdirectory_hints.py` discovers `OPENCOMPUTER.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules` with same priority semantics; `SOUL.md` always-loaded slot independently |
| Standing Orders parsed from `AGENTS.md` `## Program: <name>` blocks | `opencomputer/agent/standing_orders.py` (rev-2 import) |
| `worktree: true` config | OC worktree mode |
| `memory.memory_enabled`, `user_profile_enabled`, char limits | OC memory config |
| Per-provider API timeouts (`request_timeout_seconds`, `stale_timeout_seconds`, per-model `timeout_seconds`) | OC provider config |
| `timezone: "..."` IANA | OC timezone handling |
| Native Windows runtime (PowerShell, Win32 input) | `opencomputer/tools/powershell_run.py`, `tools/_win32_input.py`, `tools/_gui_backends.py` (PR #267 cross-platform deployment parity) |
| `quick_commands:` zero-token shell shortcuts | **Not shipped** — see §2 for why |
| `display.busy_input_mode: interrupt\|queue\|steer` config knob | **Not shipped** — see §2 for why |
| `display.busy_indicator.style: kawaii\|minimal\|dots` | **Not shipped** — see §2 for why |
| `display.platforms.<channel>:` per-channel display overrides | **Parked** — see §3 |
| `/background <prompt>` slash | **Parked** — see §3 |
| `oc uninstall` wrapper | **Not shipped** — see §2 for why |
| `oc chat -q` alias for oneshot | **Not shipped** — see §2 for why |
| `oc -c` short flag for `--continue` | **Not shipped** — see §2 for why |
| `OPENCOMPUTER_TUI_RESUME` env var | **Not shipped** — see §2 for why |
| `oc chat --toolsets "web,terminal,skills"` | **Not shipped** — see §2 for why |
| `oc -s skill1,skill2` preload-skills launch flag | **Not shipped (as launch flag)** — see §2 for why |
| `session_reset.mode` policy (idle/daily) | **Not shipped** — see §2 for why |
| `hygiene_hard_message_limit: 400` | **Not shipped** — see §2 for why |
| WSL2 reference doc (full setup walkthrough) | **Single README line** (see §4) instead of full doc — see §2 for why |
| Modal / Daytona / Vercel-Sandbox / Singularity terminal backends | **Won't-do** (already deliberately scoped out per 2026-05-06 deep-comparison) |
| Asia-region channels (DingTalk, WeCom, Feishu, QQ, Zalo) | **Won't-do** (already deliberately scoped out — geographic / language mismatch) |

---

## 2. Missing AND deliberately not shipping (with rationale)

| Hermes item | Why we are not adding it |
|---|---|
| `oc uninstall` wrapper command | Standard `pip uninstall opencomputer` (or `pipx uninstall opencomputer`) plus `rm -rf ~/.opencomputer/` already covers every install path our own `install.sh` produces. A wrapper command adds a footgun (a single misclick could nuke years of profile data) for negligible convenience. |
| `oc chat -q "query"` non-interactive flag | We already ship `oc oneshot "query"` — the same feature, different name. Forking `chat -q` and `oneshot` into two paths to the same behaviour is API drift. |
| `oc -c` short flag for `--continue` | We have `oc resume` (modal picker), `oc chat --resume <id>`, `oc chat --resume last`, and `oc chat --resume pick`. A `-c` short flag is a sixth way to do the same thing. |
| `OPENCOMPUTER_TUI_RESUME=1` env var auto-resume | The picker + `--resume` flag already cover the workflow. An env-var coupling for "always auto-resume" adds session-state magic without a clear use case. |
| `oc chat --toolsets "web,terminal,skills"` toolset filter | Touches the tool-registry contract; risk of breaking cross-tool dependencies. No current pain signal. Defer to demand. |
| `oc -s skill1,skill2` preload-skills *launch* flag | Skills load on demand and via Skill-Hub taps. The internal kanban dispatcher already invokes `--skills` programmatically where it needs to. A user-facing launch flag invites pre-loading of skills that may not be needed for the session. |
| `oc gateway status` standalone subcommand | `oc service status` already reports daemon health for systemd/launchd-installed gateways. Foreground gateways report status on stdout. Adding a third surface duplicates without clarifying. |
| `display.busy_input_mode: interrupt\|queue\|steer` config knob | Per-turn `/queue` and `/steer` slash commands already give precise control. Promoting "what happens when I hit Enter while the agent is busy" to a default-policy config adds yaml schema for a behaviour users almost always want decided per-message. |
| `display.busy_indicator.style: kawaii\|minimal\|dots\|wings\|none` | Pure cosmetic. Saves no tokens. Adds no capability. |
| `quick_commands:` zero-token yaml shell shortcuts | A user can already define shell aliases (`alias status="systemctl status opencomputer"`) or write a single-action SKILL.md. `quick_commands:` adds a yaml schema for the same thing. |
| `session_reset.mode: idle\|daily\|both\|none` + `idle_minutes`, `at_hour` | `auto_prune_days` covers the load-bearing case (privacy audit RR-2 — sessions accumulating). The reset-on-idle and reset-at-hour policies are niche refinements with no concrete demand. |
| `hygiene_hard_message_limit: 400` gateway safety valve | Gateway already handles bloat via compaction + auto_prune. A hard message-count cap is a sledgehammer that risks dropping load-bearing context. |
| Modal / Daytona / Vercel-Sandbox / Singularity terminal backends | Already deliberately scoped out per 2026-05-06 deep-comparison. We ship local/docker/ssh + native introspection. |
| Asia-region channels (DingTalk, WeCom, Feishu, QQ, Zalo, Yuanbao-only ones) | Geographic / language mismatch — already deliberately scoped out. |
| WSL2 reference doc (full setup walkthrough — `wsl --install`, `wsl.conf` systemd, filesystem rules, networking, NAT/mirrored mode, port-proxy, Task Scheduler) | The user is a Mac developer; there is no WSL2 demand to anchor against. Native Windows runtime *already works* (see §1, PR #267). A one-line acknowledgement in the README is sufficient (see §4). If a Windows-via-WSL2 user appears with a real friction point, write the doc then. |

---

## 3. Parked — plausibly valuable, awaiting demand

These items pass a "it could be useful" sniff test but don't pass *"useful enough to add right now, given this user's workflow"*.

| Hermes item | Trigger to reopen |
|---|---|
| `/background <prompt>` slash command (spawn isolated daemon-thread session, inherits model/provider/toolsets, result as inline panel) | The user develops a workflow that needs concurrent agent threads from one foreground or Telegram session. Substantial implementation: spawn mechanism + result rendering + lifecycle. Not free. |
| `display.platforms.<channel>:` per-channel display overrides (e.g. Telegram=verbose, Discord=quiet) | The user runs a multi-channel gateway long enough to want per-channel display calibration. Low effort to add when the demand is concrete. |
| Auxiliary model slot expansion (additional `auto`/`main` sentinels for new aux paths beyond vision/web_extract/compression/session_search/approval/triage_specifier) | A new feature requires a new aux call. Slot expansion is cheap when motivated. |

---

## 4. Honest README touch — Windows acknowledgement

The current `README.md` install header reads:
> *"One-line install (macOS / Linux / Termux):"*

OpenComputer actually runs natively on Windows (Python 3.13+) — `PowerShellRun`, Win32 `SendInput` shim, Windows clipboard, `msvcrt` file-locking, all shipping since PR #267 (2026-04-29 cross-platform deployment parity). Saying we don't support Windows when we do is *less honest than the code*.

The companion PR for this findings doc updates the README install section to acknowledge native Windows + point WSL2-curious users at WSL2 install path. No code change.

---

## 5. Closing

The parity question for these two specific Hermes Agent reference docs is **closed** as of 2026-05-08. ~95% shipped. The remainder either fails the "makes sense for this user" filter (§2) or is parked for demand-driven reopen (§3). One honest README correction (§4) lands with this analysis.

Future Hermes deep-comparisons supersede this snapshot. Filename is date-stamped so the supersession is clean.
