# Hermes Doc-Parity — Quickstart + CLI/TUI/WSL2/Configuration

**Date:** 2026-05-08
**Status:** Spec — implementation scope DELIBERATELY MINIMAL
**Source:** Two Hermes Agent reference docs supplied by the user verbatim:
1. *Hermes Agent — Quickstart & Install Reference*
2. *Hermes Agent — CLI, TUI, WSL2 & Configuration Reference*

---

## 1. Problem statement

The user supplied two large reference docs from Hermes Agent describing user-facing surfaces (install flow, CLI flags, TUI behaviours, WSL2 setup, and the full `config.yaml` schema), with the instruction "implement this as well as this".

A naive read of "implement" would be a multi-week parity port. That is wrong for two reasons:

- **OpenComputer has already absorbed ~95% of the load-bearing surface area** (verified against `docs/refs/hermes-agent/2026-05-06-deep-comparison.md` and a fresh code-walk on 2026-05-08).
- **The user explicitly course-corrected mid-discovery:** *"Only integrate something that actually makes sense. If you already have it, don't do it. If you're missing it, it doesn't mean that we should just fill it just because we're missing it. We will fill it because it makes sense."*

The real question this spec answers is: *with that filter applied, what (if anything) needs to ship today?*

---

## 2. Gap analysis (Hermes-doc surface → OpenComputer state)

### 2.1 Already shipped — parity ✓

| Hermes feature | OC equivalent |
|---|---|
| `curl … \| bash` install | `scripts/install.sh` (mirrors hermes shape, multi-strategy fallback: pipx > pip --user > venv) |
| Per-user vs root install | `install.sh --no-user` |
| `hermes model` interactive picker | `oc model` (`opencomputer/cli_model_picker.py`) |
| `hermes setup` wizard | `oc setup` (`opencomputer/setup_wizard.py` + `cli_setup/`) |
| `hermes gateway setup` | `oc gateway` (+ `--install-daemon` for systemd/launchd) |
| `hermes update [--check] [--backup]` | `oc update` + `oc backup` (PR #474) |
| `hermes doctor` | `oc doctor` (`opencomputer/doctor.py`) |
| `hermes sessions list` | `oc sessions` |
| `hermes config set/get/edit/check/migrate` | `oc config` typer group |
| `hermes -w` worktree mode | OC worktree mode |
| `hermes --resume <id>` | `oc chat --resume <id>` (and `oc resume` picker) |
| Push-to-talk voice (`Ctrl+B`) | Voice mode (PR #199), recent wake-word port (PR #485) |
| `Ctrl+G` open in `$EDITOR` | Tier S external-editor port |
| `Tab` autocomplete slash | Slash command completion |
| Slash command bundle (`/help /tools /model /save /skin /voice /reasoning /title /verbose /usage /history /background-equivalents`) | 24+ commands in `agent/slash_commands_impl/` |
| `/queue` + `/steer` busy-mode commands | `queue_mode_cmd.py` + `agent/steer.py` (Wave 5 T3) |
| Background sessions (foreground inline panel) | Substantial gap — see §2.3 |
| Status bar (model / context / cost / time) | Recent dashboard+TUI work (PRs #486 #487) |
| `--tui` with modal pickers | Recent TUI port |
| LaTeX rendering, alternate-screen | TUI work |
| Compression — `enabled`, `threshold`, `protect_last_n`, model | `opencomputer/agent/compaction.py`, model-aware widths PR #343 |
| `auto_prune` session retention | `auto_prune_days` / `auto_prune_untitled_days` (Tier A4 from deep-comparison) |
| Terminal backends — local/docker/ssh | `opencomputer/sandbox/` |
| `display.tool_progress` `off\|new\|all\|verbose` | `display_toggles_cmd.py` runtime toggle |
| `streaming.transport` (edit mode) | Recent partial-stream recovery (PR #482) |
| `privacy.redact_pii` | `security/redact.py` |
| `group_sessions_per_user`, `unauthorized_dm_behavior` | Gateway DM pairing (PR-1) |
| `security.tirith_enabled`, `website_blocklist` | Tier-S security stack |
| `approvals.mode: manual\|smart\|off` | F1 consent layer |
| `checkpoints.enabled` | Coding-harness checkpoint manager |
| Context files `HERMES.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules` | `agent/subdirectory_hints.py` discovers `OPENCOMPUTER.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules` (priority chain) |
| Standing Orders from `AGENTS.md` | `agent/standing_orders.py` |
| `worktree: true` config | OC config flag |
| Memory backends + char limits | Plugin-SDK memory contract; Honcho default |
| MCP via `mcp_servers:` config | OC MCP integration |
| Per-provider API timeouts | OC provider config |
| TTS — Edge default, ElevenLabs, OpenAI, MiniMax, Mistral, Gemini, xAI | OC voice extras (Edge default, multiple backends) |
| STT — local Whisper, Groq | OC STT (PR #485 wake-word + voice loop) |
| Native Windows runtime | `_win32_input.py`, `powershell_run.py`, clipboard (PR #267 cross-platform deployment parity) |

### 2.2 Missing AND deliberately not shipping (won't-do, with rationale)

| Hermes item | Why we are not adding it |
|---|---|
| `oc uninstall` command | Standard `pip uninstall opencomputer` + `rm -rf ~/.opencomputer/` covers it. A wrapper command is a footgun (wrong invocation could nuke profile data); convenience is not worth the maintenance + safety surface. |
| `oc chat -q "query"` | We already have `oc oneshot "..."` — the same feature, different name. Forking the CLI surface with two paths to the same behaviour is API drift. |
| `oc -c` short flag for `--continue` | We have `oc resume` (and `oc chat --resume <id>`/`pick`/`last`) which covers the same ground. Short-flag aliases multiply CLI churn. |
| `oc --tui` `OPENCOMPUTER_TUI_RESUME=1` env var auto-resume | Niche. The picker + `--resume` flag already cover the workflow without env-var coupling. |
| `oc chat --toolsets "web,terminal,skills"` toolset filter | Touches the tool-registry contract for a feature with no current pain signal. Defer to demand. |
| `oc -s skill1,skill2` preload-skills launch flag | Skills load on demand and via taps. The existing `--skills` plumbing inside the kanban dispatcher is sufficient for the current internal use case. |
| `oc gateway status` | `oc service status` already reports systemd/launchd-installed daemon health. Foreground gateways report status to stdout when running. Adding a third surface is duplication. |
| `display.busy_input_mode: interrupt\|queue\|steer` config knob | Per-turn `/queue` and `/steer` slash commands already give precise control. Promoting to a default-policy config is a YAGNI knob until users ask for it. |
| `display.busy_indicator.style: kawaii` | Pure cosmetic. |
| `display.platforms.<channel>:` per-channel display overrides | No current pain signal from gateway users. Defer. |
| `quick_commands:` yaml zero-token shell shortcuts | Replaceable by shell aliases or a per-user skill. Adds yaml schema surface for marginal benefit. |
| `session_reset.mode: idle\|daily` policy + `idle_minutes` / `at_hour` | `auto_prune_days` covers the load-bearing case. The reset-on-idle policy is a niche refinement. |
| `hygiene_hard_message_limit: 400` gateway safety valve | Niche; gateway already handles bloat via compaction + auto_prune. |
| Modal / Daytona / Vercel-Sandbox / Singularity terminal backends | Already deliberately skipped per the 2026-05-06 deep-comparison (we ship local/docker/ssh + native introspection). Reopen on demand. |
| WSL2 reference doc (full setup walkthrough) | The user is a Mac user — no Windows-via-WSL demand. Native Windows already works (see §2.1). A line in the README pointing Windows users at WSL2 *or* native install is sufficient (see §3.2). |
| Asia-region channels (DingTalk, WeCom, Feishu, QQ, Zalo) | Geographic mismatch — already deliberately skipped. |

### 2.3 Missing, plausible value, but **not now** (parked)

| Hermes item | Reason to park, not skip outright |
|---|---|
| `/background <prompt>` slash command | Real utility (spawn isolated daemon thread session, inherits model/provider/toolsets, result as inline panel). Substantial: needs spawn mechanism + result-rendering pipe + lifecycle. Reopen if the user (Telegram-driven) develops a workflow that needs concurrent agent threads. |
| `display.platforms.<channel>:` per-channel overrides | Genuinely useful for users running multi-channel gateways (e.g. Telegram=verbose, Discord=quiet). Wait for an explicit gateway-user ask. |
| Aux model slot expansion (web_extract / session_search / approval / triage_specifier) with `auto`/`main` sentinels | Current aux slot set is sufficient. Add when a feature requires a new aux call. |

### 2.4 Honest README undersell

The current `README.md` install section reads *"One-line install (macOS / Linux / Termux)"*. OpenComputer actually runs natively on Windows (per PR #267 cross-platform deployment parity, 2026-04-29 — `PowerShellRun`, Win32 `SendInput` shim, clipboard, 9+ files containing `sys.platform == "win32"` paths).

Saying we don't support Windows when we do is *less honest than the code*. Worth a one-line correction.

---

## 3. Implementation scope

### 3.1 Findings doc

Create `docs/refs/hermes-agent/2026-05-08-quickstart-cli-tui-wsl2-config-parity.md`.

Contents:
- One-paragraph context (why this comparison was done — the user supplied two reference docs and the "makes sense" filter).
- The four tables from §2 of this spec.
- A short closing paragraph noting that the parity question is closed for these two docs, with one parked item (`/background`) flagged for future demand-driven reopen.

This is *the deliverable that the user can review six months from now to see why we did or did not port each Hermes surface*. Without this artifact, the analysis would have to be redone.

### 3.2 README Windows acknowledgement

Modify `OpenComputer/README.md` install section:

**Before:**
```
**One-line install** (macOS / Linux / Termux):
```

**After:**
```
**One-line install** (macOS / Linux / Termux — on Windows run inside WSL2, or use native install):
```

Plus a one-paragraph note under the install command pointing Windows users at either: (a) the install script via WSL2, or (b) native install via `pip install opencomputer` (since Windows runtime support already ships).

That's it. Two markdown files.

---

## 4. Out of scope (explicitly)

- New CLI commands.
- New slash commands.
- New config schema entries.
- Code changes of any kind.
- Tests (no code → no tests).
- WSL2 deep documentation.
- `/background` slash implementation.

---

## 5. Risk register

| Risk | Mitigation |
|---|---|
| Findings doc rots (becomes stale as Hermes evolves) | Filename includes the date `2026-05-08`; document is a snapshot, not a contract. Future deep-comparison docs supersede it. |
| README change breaks existing tone / install instructions | Surgical 1-line edit + 1-paragraph addition. Re-read post-edit; no other rewriting. |
| User wanted more than this | The spec is presented before execution; user can course-correct to expand scope before the plan executes. |

---

## 6. Validation

- Run `pytest tests/` after the doc commits to confirm no regression. Doc-only changes shouldn't move test counts; if they do, investigate.
- Run `ruff check` (no Python touched, but worth running for safety).
- Render the findings doc locally to confirm tables are well-formed.
- Re-read the README install section in full to verify the Windows note flows naturally with the existing prose.

---

## 7. Decision

Ship §3.1 + §3.2. Park everything in §2.3. Skip everything in §2.2.
