# Hermes Doc-Parity Snapshot — 2026-05-08 (verified)

**Source docs compared:**
1. *Hermes Agent — Quickstart & Install Reference* (install script, provider setup, first run, slash commands, layering features, update/maintenance, diagnostics, uninstall)
2. *Hermes Agent — CLI, TUI, WSL2 & Configuration Reference* (launch flags, status bar, keybindings, slash commands, busy modes, quick commands, background sessions, session management, context compression, TUI specifics, WSL2 setup, terminal backends, full `config.yaml` schema)

**OpenComputer state walked:** main tip `429c5b8f` + the `parity/hermes-quickstart-cli-2026-05-08` branch (this PR adds 2 commits on top — chat aliases + /background).
**Verification basis:** Two parallel sub-agents read the current source and confirmed/refuted ~45 individual parity claims. This document reflects the **verified** reality, not just memory entries — many claims that initially looked "shipped" turned out to be shipped under different names, on a sibling branch, or as a subset of what Hermes documents.

---

## How this document was written

The user supplied both Hermes reference docs verbatim with the instruction *"implement this as well as this"*. A naive read suggests a multi-week parity port. Two facts narrow that:

1. OpenComputer has been a Hermes-extraction project for months. Most load-bearing surfaces are present.
2. Mid-discovery the user explicitly course-corrected: *"Only integrate something that actually makes sense. If you already have it, don't do it. If you're missing it, that doesn't mean we should fill it just because we're missing it."*

The first version of this document had ~50 "parity ✓" rows that turned out to be over-stated on closer verification — many were *partial* or *named-differently* matches. This rewrite corrects that.

The companion PR (this branch) ships **three** items the verification revealed *do* pass the "makes sense" filter:
- `oc chat -c` / `oc chat --continue` — alias for `--resume last`
- `oc chat -q "query"` — alias for `oc oneshot "query"` (refactored to share `_run_oneshot_turn` helper)
- `OPENCOMPUTER_TUI_RESUME=…` env var + `oc tui --continue` / `oc tui --resume <id>` flags + Ink/React TUI side that consumes `OC_TUI_RESUME` and seeds the session
- `/background <prompt>` slash (MVP — submit/list/show; no push-on-completion yet)

Everything else either passed verification as already-shipped, was honestly recategorised after verification, or was deliberately left for demand-driven reopen.

---

## 1. Confirmed shipped — parity ✓

These rows survived verification. File:line evidence on each.

### Quickstart / Install doc

| Hermes surface | OpenComputer evidence |
|---|---|
| `curl … \| bash` install with pipx > pip --user > venv fallback | `scripts/install.sh` |
| Per-user vs root install | `install.sh --no-user` flag |
| `hermes model` interactive picker | `oc model` (`opencomputer/cli_model_picker.py:74-79` two-space pad + `← <marker>` UX) |
| `hermes setup` wizard | `oc setup` (`opencomputer/setup_wizard.py` + `cli_setup/sections.py` + section_handlers/) |
| `hermes gateway --install-daemon` | `oc gateway --install-daemon` (`cli.py:2384,2539` + `opencomputer/service/{macos_launchd,linux_systemd,windows_schtasks}.py`) |
| `hermes update` | `oc update` (`cli_update_check.py`) |
| `hermes backup` (create/restore + verify) | `oc backup` (`cli_backup.py:49,133,217` — HMAC chain verify) |
| `hermes doctor` | `oc doctor` — provider-key, MCP, introspection deps, voice, browser checks |
| `hermes sessions list` | `oc sessions` (`cli_session.py:141-259`) |
| `hermes login`, `hermes logout` (getpass flow) | `oc login`, `oc logout` (`cli_login.py:140-181`, uses `getpass.getpass`) |
| Voice mode `pip install "[voice]"` extras | OC voice extras + `cli_voice.py:53-672` (synthesize/transcribe/cost/talk/wake/train-wake) |
| MCP via `mcp_servers:` config | `opencomputer/mcp/` + `cli_mcp.py` |
| Settings storage layout | `~/.opencomputer/<profile>/{config.yaml,.env,auth.json,SOUL.md,memories,skills,cron,sessions,logs}` |

### CLI / TUI doc

| Hermes surface | OpenComputer evidence |
|---|---|
| `hermes chat -q "query"` non-interactive | `oc oneshot "query"` (`cli.py:2034`) — and now `oc chat -q "query"` alias (this PR) |
| `hermes -w` / `--worktree` worktree mode | `oc chat -w` / `--worktree` (`cli.py:2163-2173`) — note: per-command flag, not a global pre-subcommand flag |
| `hermes --resume <id>` and modal picker | `oc chat --resume <id>` + `oc resume` (`cli.py:2227,2305`) |
| `hermes --continue` / `-c` | `oc chat --continue` / `-c` (this PR — alias for `--resume last`) |
| `hermes oneshot` non-interactive | `oc oneshot "query"` |
| Tab autocomplete slash commands | `cli_ui/input_loop.py:714` |
| Multiline input (`Alt+Enter` / `Ctrl+J`) | `cli_ui/input_loop.py:190-194,797-801` |
| Voice push-to-talk | Voice mode + wake-word (`cli_voice.py:471,672` train-wake; PR #199 + PR #485) — keybinding differs (Hermes Ctrl+B; OC binds via dedicated `oc voice talk` flow, not Ctrl+B) |
| `Ctrl+C` interrupt | `cli_ui/input_loop.py:788` |
| Open input in `$EDITOR` | `cli_ui/input_loop.py:206-218` — Ctrl+X+Ctrl+E (Emacs idiom), not Hermes's Ctrl+G |
| Paste image from clipboard | `cli_ui/input_loop.py:206` Ctrl+V (not Hermes's Alt+V) |
| `/queue` busy-mode | `agent/slash_commands_impl/queue_mode_cmd.py` |
| `/steer` mid-run nudge | `opencomputer/agent/steer.py` (Wave 5 T3 — Hermes commit `e27b0b765`) |
| `/help`, `/sessions`, `/reload`, `/agents` slash | `cli_ui/slash.py:78,117,173`; `slash_commands_impl/agents_cmd.py:24` |
| `/background <prompt>` | This PR — `slash_commands_impl/background_cmd.py` (MVP: start/list/show; no push-on-completion) |
| `display.tool_progress: off\|new\|all\|verbose` runtime toggle | `display_toggles_cmd.py:18,30,40` (`_VERBOSE_MODES = ("off","new","all","verbose")`); surfaced as `/verbose` slash, runtime-only (not a config field) |
| `oc tui` (Ink+React) — alternate-screen, modal pickers, status bar | `oc tui` subcommand (`cli_tui.py`) launches Node entry — full TUI features run in JS, *not* Python |
| `OPENCOMPUTER_TUI_RESUME` env var | This PR — env precedence + `oc tui --continue` / `--resume <id>` + Ink-side consumer in `ui-tui/src/{entry.tsx,app.tsx}` |
| Per-prompt elapsed time | `cli.py:1200`; `cli_ui/streaming.py:530-535` |
| Status bar (mode/profile/personality) | `cli_ui/input_loop.py:454-489` — note: shows mode/profile/personality, NOT model/context%/cost (those are footer-only when enabled) |
| `display.runtime_footer.enabled` (model/pct/cwd line) | `gateway/runtime_footer.py:24-44` (per-platform overrides supported) — opt-in, default off |
| Streaming + partial-stream recovery | PR #482 |
| Compression (auto-summarise old turns) | `agent/compaction.py` (`CompactionConfig.preserve_recent`/`threshold_ratio`/`summarize_max_tokens`) — note: field names differ from Hermes's `protect_last_n` / `target_ratio` / `enabled` |
| `auto_prune_days` / `_untitled_days` / `_min_messages` session retention | `agent/config.py:185-188` + `loop.py:409-419` (Tier A4 from deep-comparison) |
| Terminal backends — local, docker, ssh | `opencomputer/sandbox/{linux.py,macos.py,docker.py,ssh.py,auto.py}` |
| Context files priority chain (`OPENCOMPUTER.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules`) | `agent/subdirectory_hints.py:49-52` |
| `SOUL.md` always-loaded slot | `MemoryConfig.soul_path` (`config.py:208-211`) — frozen base prompt; doctor check `doctor.py:328-329` |
| Standing Orders parsed from `AGENTS.md` `## Program: <name>` blocks | `agent/standing_orders.py:42-43` (`_HEADER_RE`) |
| Native Windows runtime | `tools/powershell_run.py`, `tools/_win32_input.py`, `tools/_gui_backends.py`, `tools/system_click.py`, `tools/system_keystroke.py`, `tools/system_notify.py`, `cli_ui/clipboard.py` (PR #267 cross-platform deployment parity) |

### Tier-S security + safety primitives

| Hermes surface | OpenComputer evidence |
|---|---|
| `redact_secrets` + PII regex set | `security/redact.py:83-110` (`redact_runtime_text` covers email/phone/IPv4/SSN/credit-card + secret patterns); env-var toggle `OC_REDACT_RUNTIME` (not a config field) |
| `tirith` pre-exec scanner (Rust) | `security/tirith.py` exists |
| OSV malware check | `security/osv_check.py` |
| URL safety / allowlist | `security/url_safety.py` |
| Approvals / consent gate | `plugin_sdk/consent.py` `ConsentTier(IMPLICIT|EXPLICIT|PER_ACTION)` IntEnum — different mapping than Hermes's `manual\|smart\|off` mode strings; equivalent semantics |

---

## 2. Newly shipped in this PR

| Hermes parity item | OpenComputer addition |
|---|---|
| `hermes chat -c` / `--continue` | `oc chat -c` / `--continue` — sugar for `--resume last` (`cli.py` chat function) |
| `hermes chat -q "query"` | `oc chat -q "query"` — alias for `oc oneshot`; refactored body into `_run_oneshot_turn` helper so both paths share the same flow |
| `HERMES_TUI_RESUME` env var | `OPENCOMPUTER_TUI_RESUME` env var with values `1`/`true`/`yes`→`last` (auto-resume latest), or literal session id; precedence `--resume <id>` > `--continue` > env var. Plus matching `oc tui --continue` / `--resume <id>` flags. Ink/React TUI side reads `OC_TUI_RESUME` and seeds `sessionId.current` so subsequent `client.chat()` calls route into that session |
| `/background <prompt>` slash | New `BackgroundJobRegistry` (thread-safe in-memory, daemon-thread workers, fresh AgentLoop per job via factory) + `BackgroundCommand` slash with `start <prompt>` (or bare prompt) / `list` / `show <id>`. Inherits model+provider+toolsets via factory; new session id per job (no shared history). MVP excludes push-on-completion to the originating channel |

---

## 3. Verification revealed — Hermes has it, we don't (honest gaps)

These were initially listed as "parity ✓" in the first draft but the parallel verification sub-agents found OC either ships them under different names with different semantics, or doesn't ship them at all. Recording for honesty.

| Hermes surface | OC reality |
|---|---|
| Auxiliary model slots `vision` / `web_extract` / `compression` / `session_search` / `approval` / `triage_specifier` (6 task-typed slots with `auto`/`main`/`<provider>` sentinels) | `agent/auxiliary_client.py:73-84` ships 4 differently-named slots: `summary_model`, `classify_model`, `extract_model`, `title_model`. `aux_llm.py` has a `complete_vision` function but no per-task model override. **Different taxonomy.** |
| `delegation.max_concurrent_children` + `max_spawn_depth` + `orchestrator_enabled` config | `LoopConfig.delegation_max_iterations` + `max_delegation_depth` only — no concurrency cap, no spawn-depth tree, no orchestrator gate. |
| `streaming.transport: edit\|append\|replace`, `edit_interval`, `fresh_final_after_seconds` config dataclass | No `StreamingConfig` exists. Streaming behaviour is hardcoded inside provider plugins; not user-tunable. |
| `display.platforms.<channel>.tool_progress` per-channel overrides | `runtime_footer.py:39-43` shows the per-platform pattern *exists for the runtime footer*, but `tool_progress` itself is runtime-only (`runtime.custom`), not a config knob. Per-platform tool_progress override would require new wiring. |
| `display.busy_input_mode: interrupt\|queue\|steer` config knob | Per-turn `/queue` and `/steer` slash commands cover the same ground; no config-level default mode. |
| `display.busy_indicator.style: kawaii\|minimal\|dots\|wings\|none` | Cosmetic only; not implemented. |
| `quick_commands:` zero-token shell shortcuts | Not implemented; replaceable via shell aliases or a per-user skill. |
| `oc -s skill1,skill2` preload-skills *launch* flag | The kanban dispatcher uses `--skills` programmatically (`kanban/db.py:2845-2857`); no user-facing launch-time `-s` flag. |
| `oc gateway status` standalone subcommand | `oc service status` covers daemon health for the systemd/launchd-installed gateway; no equivalent for foreground gateway. |
| `oc uninstall` wrapper command | `pip uninstall opencomputer` + `rm -rf ~/.opencomputer/` covers it; no wrapper. |
| Config: `worktree: true` | No config field; `--worktree`/`-w` is a CLI flag on `oc chat` only. |
| Per-provider API timeouts (`request_timeout_seconds`, `stale_timeout_seconds`, per-model `timeout_seconds`) | No corresponding config fields. The two `timeout_seconds` hits in `config.py` are for `HookCommandConfig` and `FullSystemControlConfig`, not provider transports. |
| Docker sandbox security flags `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit 256` | `sandbox/docker.py:90-111` builds `--rm`, `--name`, `--memory`, `--network none`, `-v`, `-e`, `--cpus` only. Hardening flags missing. |
| `session_reset.mode: idle\|daily\|both\|none` policy + `idle_minutes` / `at_hour` triggers | Not implemented. `auto_prune_days` covers the start-up retention slice. |
| `hygiene_hard_message_limit: 400` | Not implemented. |
| WSL2 reference doc (full setup walkthrough — wsl.conf systemd, networking, NAT/mirrored mode, port-proxy, Task Scheduler) | Native Windows works (PR #267); WSL2-via-script works too but isn't documented end-to-end. README now has a 1-line acknowledgement (companion commit). |
| Web search backends — `searxng`, `parallel` | `tools/search_backends/__init__.py:28-32` wires `ddg`, `brave`, `tavily`, `exa`, `firecrawl` only — no `searxng` or `parallel` backend. |
| Browser config — `inactivity_timeout`, `dialog_policy: must_respond\|auto_dismiss\|auto_accept` | `extensions/browser-control/control_daemon.py:96` has `command_timeout_s`; the inactivity-timeout and dialog-policy knobs aren't present. |
| TTS — `minimax`, `gemini`, `xai` | OC voice ships `edge`, `openai`, `elevenlabs`, `piper`, `neutts`, `kittentts` (`voice/tts_command.py:24`). Three Hermes-supported providers missing. |
| STT — local Whisper file (`mlx-whisper` / `whisper-cpp`), `mistral` | `voice/stt.py` (OpenAI Whisper-1) + `voice/groq_stt.py` (Groq Whisper). Local-Whisper file path isn't there; `mistral` STT not wired. |
| `display` config dataclass fields — `show_cost`, `runtime_metadata_footer`, `language` | Not implemented as a `DisplayConfig`. Some live on `runtime.custom` (e.g. `bell_on_complete`, `show_reasoning`); language is single-locale (en) only. |
| `privacy.redact_pii` config field | Toggle is env-var-only (`OC_REDACT_RUNTIME`); no top-level `privacy.redact_pii` config key. |
| `group_sessions_per_user` (per-user isolation in group chats) | No grep evidence in `gateway/` or `extensions/`. Not implemented. |
| `unauthorized_dm_behavior: pair\|ignore` | Implemented on a *sibling branch* (`feat/gateway-parity-pr1-2026-05-08` — Messaging Gateway PR-1 from memory `project_messaging_gateway_pr1.md`). Not yet on `origin/main`. |
| `security.tirith_enabled` and `website_blocklist` config fields | The `tirith.py` Rust binary wrapper exists but `tirith_enabled` is not a config field. `website_blocklist` not implemented. |
| `checkpoints.enabled` + `max_snapshots` config | Checkpoint store exists (`extensions/coding-harness/rewind/store.py`) + auto-checkpoint hook + `tools/rewind.py`. Config knobs `enabled` and `max_snapshots` aren't surfaced. |
| `memory_enabled` / `user_profile_enabled` config field names | `MemoryConfig` (`config.py:191-258`) has overall `enabled: bool = True` plus `memory_char_limit` / `user_char_limit`. Different field names from Hermes; equivalent semantics. |
| `oc doctor` explicit hooks check | Doctor covers provider/MCP/introspection/voice/browser. Hooks subsystem isn't an explicit doctor row. |
| `oc config check` and `oc config migrate` subcommands | OC `oc config` group has `show / get / set / path / edit / variants / init` — no `check` or `migrate`. |

---

## 4. Deliberately scoped out (won't-do, with rationale)

| Hermes item | Rationale |
|---|---|
| Modal / Daytona / Vercel-Sandbox / Singularity terminal backends | Already deliberately scoped out per 2026-05-06 deep-comparison. We ship local/docker/ssh + native introspection. |
| Asia-region channels (DingTalk, WeCom, Feishu, QQ, Zalo) | Geographic / language mismatch — already deliberately scoped out. |
| Atropos RL training submodule, trajectory compression | Out of scope (training infra, not user-facing). |
| Skills marketplace at full scope (payments + ratings + curation) | Tier 1.A's minimal hub (browse/install/publish/tap/audit) is the right ceiling. |
| Native mobile apps | Won't-do. |
| Full i18n (Chinese, Japanese) | English-only ships. Reopen on demand. |

---

## 5. Parked — awaiting demand signal

| Hermes item | Trigger to reopen |
|---|---|
| Auxiliary model slot expansion to Hermes's 6-task taxonomy | A new feature requires per-task aux routing the current 4-slot set can't express. |
| `delegation.max_concurrent_children` / `max_spawn_depth` config | A user runs into delegation fan-out and wants control. |
| `display.platforms.<channel>.tool_progress` overrides | A multi-channel gateway user wants per-channel verbosity. |
| Docker sandbox hardening flags (`--cap-drop ALL` etc.) | Security review or operator request. |
| Per-provider API timeouts | Long-running provider call surfaces a stall users can't address. |
| Push-on-completion for `/background` jobs | A user develops a workflow that needs the result auto-pushed back to the originating channel. |

---

## 6. Honest README touch — Windows acknowledgement

The original `README.md` install header read *"One-line install (macOS / Linux / Termux)"*. OpenComputer actually runs natively on Windows (Python 3.13+) per PR #267. The companion commit updates the install section to acknowledge native Windows + point WSL2 users at the script.

---

## 7. Closing

This snapshot supersedes the over-stated first draft at the same path. The verified parity surface is honest about:
- Where OC matches Hermes 1:1 (§1).
- Where this PR closes 1-line + slash gaps that survived the "makes sense" filter (§2).
- Where Hermes ships something OC doesn't, named differently or not at all (§3) — *recorded but not auto-promoted to a roadmap*.
- Where we deliberately scoped out (§4) and what would justify reopening (§5).

Future Hermes deep-comparisons supersede this snapshot. Filename is date-stamped so the supersession is clean.
