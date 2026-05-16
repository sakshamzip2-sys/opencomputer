# OC parity with Hermes + OpenClaw — Part 2: Plan & Plan Audit

Date: 2026-05-16
Owner: Saksham
Companion file: `PART-1-brainstorm-and-audit.md` — read first.

This file implements Phases 3 and 4 of the workflow: the concrete plan, then the harsh self-critic pass.

---

## Phase 3 — /plan

### "Done" in one sentence

OC ships a sandbox-scope policy with `agent`/`session`/`tool` scopes and a `oc sandbox explain` command, an in-loop tool-call duplicate detector with audit logging, an E2B ephemeral-sandbox backend behind a multi-backend resolver, NeuTTS as an optional local-voice tool, and a Microsoft Graph tool covering mail/calendar/OneDrive — all behind the existing capability-registry gates, with `pytest` green on the boundary tests.

### Milestones

#### Milestone 1 — Sandbox scope + tool-loop detection (LOAD-BEARING, **MVP**)

Done when: `oc sandbox enable --scope=session` works on this laptop; `oc sandbox explain` prints the effective policy; agent halts on a 3rd identical-tool-call within an 8-call window and logs the trip to `audit.db`.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T1.1 Read `opencomputer/sandbox/{docker,linux,macos,ssh}.py` end-to-end; write a one-page implementation summary at `docs/refs/oc-sandbox-baseline.md` | S | — | If existing code is stubbier than expected, this milestone grows. **Validate before writing T1.2.** |
| T1.2 Design `SandboxScope` enum (`none`/`agent`/`session`/`tool`) + `SandboxPolicy` dataclass in `opencomputer/sandbox/policy.py` | S | T1.1 | API stability; commit early to the dataclass shape |
| T1.3 Wire scope selector into `sandbox/runner.py`; container key includes the scope | M | T1.2 | Backward-compat: existing users default to `none` |
| T1.4 Add `oc sandbox enable` / `disable` / `explain` CLI subcommands in `opencomputer/cli_sandbox.py` (new file, mirrors `cli_consent.py` style) | M | T1.3 | — |
| T1.5 Config schema: `sandbox.scope`, `sandbox.tools.allow`, `sandbox.tools.deny` in `<profile>/config.yaml`. Add to `plugin_sdk/settings.py` schema. | S | T1.2 | Config migrations are scary; gate on opt-in `oc sandbox enable` |
| T1.6 `LoopDetector` class in `opencomputer/agent/loop_detector.py`. Sliding window of last 8 ToolCalls; trip if 3+ identical (`(tool_name, sha256(json.dumps(args, sort_keys=True)))`); `loop_safe` opt-out on `BaseTool`. | M | — (parallel) | False-positive rate; threshold tuning |
| T1.7 Wire `LoopDetector` into `agent/loop.py`; on trip, return `StepOutcome(stop_reason="tool_loop")`, append entry to `audit.db` table `tool_loop_trips` | S | T1.6 | — |
| T1.8 Tests: `tests/sandbox/test_scope_policy.py` (scope selection per session/agent/tool), `tests/agent/test_loop_detector.py` (window edge cases, opt-out), `tests/sandbox/test_explain_cli.py` (CLI golden output) | M | T1.4, T1.7 | — |
| T1.9 Docs: `docs/sandbox-and-scope.md`, `docs/loop-detection.md`. Both linked from `README.md` features section. | S | T1.4, T1.7 | — |

Milestone-1 total: ~**L** (target: 7–10 working days).

#### Milestone 2 — E2B ephemeral sandbox backend + multi-backend resolver

**Replaces the previous M2 (Crabbox plugin).** Rationale: Crabbox is the wrong shape for OC's agent use case — it leases full VMs for minutes-to-hours, optimised for "run my CI suite on a remote beefy box." What OC actually needs for risky tool calls is **ephemeral, fast-boot, per-call** containers. E2B is purpose-built for that.

**Honest framing of what this milestone is vs. what Hermes does** (corrected after reading Hermes source):

| Aspect | Hermes today | OC after M2 |
|---|---|---|
| Backend files in `sandbox/` | 8 files (`tools/environments/`) | OC's existing 5 + new `e2b.py` |
| What picks the backend | `os.getenv("TERMINAL_ENV")` read once at process startup | Resolver function called per tool invocation |
| Per-tool sandbox declarations | None (only the shell tool uses backends) | New `BaseTool.sandbox_preference` / `sandbox_backend_hint` fields |
| Mid-session backend switch | Restart the process | `oc sandbox set --backend e2b` |

OC's M2 extends Hermes' multi-backend **file layout** but the resolver + per-tool routing is OC-original. The earlier draft of this doc oversold it as "Hermes-style"; correcting that now.

Done when:
1. `pip install opencomputer[e2b]` adds the SDK dep.
2. `~/.opencomputer/<profile>/config.yaml` accepts `sandbox.backend: e2b` and `sandbox.e2b.api_key_env: E2B_API_KEY`.
3. The agent runs `BashTool(command="ls /")` and it executes inside a fresh E2B container, not on the host.
4. `oc sandbox set --backend e2b` switches the active session's backend.
5. `oc sandbox explain` shows: backend, scope, fallback, and which tools are sandboxed.
6. A tool can declare `sandbox_preference = "required" | "skip" | "default"` and `sandbox_backend_hint = "any" | "<name>"`; the resolver respects both.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T2.1 Read E2B's Python SDK docs (e2b.dev/docs); write a one-page extraction at `docs/refs/e2b/2026-05-16-sdk-survey.md` covering: `Sandbox.create()`, command execution, filesystem ops, lifecycle / auto-kill timeout, pricing per-second model. Optional pre-spike: 20-line Python script that creates, execs `echo hello`, destroys; prints duration + cost. | S | — | SDK shape could change before v1; pin to a known version |
| T2.2 Pin E2B SDK version range in `pyproject.toml` under `[project.optional-dependencies]` as `e2b = ["e2b>=X.Y,<X.Z"]` | S | T2.1 | — |
| T2.3 Implement `opencomputer/sandbox/e2b.py` (~200 LOC). Implements the same `SandboxBackend` interface as `docker.py`, `linux.py`, etc. Methods: `create()`, `exec(command)`, `read_file()`, `write_file()`, `destroy()`. Lazy-import the SDK; missing dep = backend unavailable but doesn't crash OC. | M | T2.2 | API surface must match other backends exactly; resolver depends on this contract |
| T2.4 Implement `opencomputer/sandbox/resolver.py` (~150 LOC). Single function `resolve_backend(tool, config, ctx) -> SandboxBackend`. Logic order: tool-says-skip = return None; user-disabled-globally = respect tool's `required` claim; tool has backend_hint and it's available = use hint; else = use user's configured default. **Plain branching, no magic.** | S | T2.3 | API stability — this dataclass + function shape enters `plugin_sdk/sandbox.py` and third-party plugins will depend on it |
| T2.5 Extend `plugin_sdk/tool_contract.py::BaseTool` with two new optional fields: `sandbox_preference: Literal["required","skip","default"] = "default"` and `sandbox_backend_hint: str | None = None`. Backwards-compatible (default values match current behaviour). | S | parallel with T2.3 | API stability — additive only; never break existing tools |
| T2.6 Wire resolver into the tool dispatch path in `opencomputer/agent/loop.py`. Before each tool invocation: call resolver, get backend, route call through it. Existing tools without preference set continue to behave exactly as today. | M | T2.3, T2.4, T2.5 | Backward-compat critical: existing OC users must see zero behaviour change until they opt in via `oc sandbox set` |
| T2.7 CLI: `oc sandbox set --backend <name> --scope <agent|session|tool>` and `oc sandbox explain`. Mirrors `cli_consent.py` style. | S | T2.4 | Config persistence: write to `<profile>/config.yaml` atomically |
| T2.8 Cost guard: E2B charges per-second of execution. Each `exec()` call reports duration; resolver feeds it into `opencomputer/cost_guard/` for the daily/session caps. Default cap: $1/session, configurable. | M | T2.3, T2.6 | E2B SDK exposes duration in metadata; verify in T2.1 |
| T2.9 Fallback policy: if `sandbox.fallback: local` is set and E2B is unreachable, run on host with a clear warning logged to `audit.db`. If `fallback: error`, fail loudly. Default: `error` (do not silently downgrade safety). | S | T2.4 | "Silent fallback to host" is a footgun — default to fail |
| T2.10 Tests: `tests/sandbox/test_e2b_backend.py` (mocked SDK), `tests/sandbox/test_resolver.py` (full decision matrix), `tests/sandbox/test_backend_fallback.py`, `tests/tools/test_basetool_sandbox_fields.py` | M | T2.3, T2.4, T2.5 | E2E against real E2B = nice-to-have, gated on having an E2B API key in CI |
| T2.11 Docs: `docs/sandbox/e2b.md` (config, env var, cost model, when to use vs `docker`), update `docs/sandbox-and-scope.md` from M1 to reference the resolver | S | T2.7 | — |

Milestone-2 total: ~**M** (target: 5–7 working days). Same calendar budget as the original Crabbox plan, materially more value: E2B integrates with OC's existing sandbox shape, no Go-CLI shell-out, no broker-config dance.

**Crabbox status:** removed from v1 scope. One-line trigger to re-evaluate: *"when there is a real, recurring 'run OC's pytest suite on a remote VM weekly' workflow."* Until then, E2B + the user's hosting box (Hetzner / Fly / Render) covers the agent-sandbox use case completely.

#### Milestone 3 — Microsoft Graph tool (mail + calendar + OneDrive only)

Done when: `oc auth login graph` runs the device-code OAuth flow; `oc chat` exposes `GraphSendMail`, `GraphListCalendar`, `GraphListDriveFiles`; tokens auto-refresh on 401.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T3.1 Read Hermes' `tools/microsoft_graph_client.py` + `microsoft_graph_auth.py`; write an extraction notes file at `docs/refs/hermes/microsoft-graph.md` (NOT a copy of the code — patterns only) | S | — | Don't lift code (license/style mismatch) |
| T3.2 Implement `opencomputer/integrations/graph/client.py` — minimal `GraphClient(token)` with `.mail.send()`, `.calendar.list()`, `.drive.list()` methods using `httpx.AsyncClient` | M | T3.1 | Graph paging is non-trivial; do it right |
| T3.3 Auth: extend `opencomputer/auth/` with a `graph_oauth.py` that runs device-code flow; store tokens in `auth.json` alongside existing OAuth tokens; integrate with `oc auth login graph` | M | — (parallel with T3.2) | Existing `mcp_oauth_manager` may already cover this; reuse, don't duplicate |
| T3.4 Build the three tools: `GraphSendMailTool`, `GraphListCalendarTool`, `GraphListDriveFilesTool` in `opencomputer/tools/graph_*.py`. Capability claims = `EXPLICIT` for send-mail, `IMPLICIT` for list-calendar / list-files. | M | T3.2, T3.3 | Schema design: `to` field validation, datetime tz handling |
| T3.5 Tests: `tests/tools/test_graph_send_mail.py`, `test_graph_list_calendar.py`, `test_graph_list_files.py` — each mocks `httpx` + asserts payload shape | M | T3.4 | — |
| T3.6 Docs: `docs/integrations/microsoft-graph.md`. Note that Teams + SharePoint are deferred. | S | T3.4 | — |

Milestone-3 total: ~**L** (target: 7–9 working days).

#### Milestone 4 — NeuTTS local voice synthesis (optional dependency)

Done when: `pip install opencomputer[neutts]` adds the deps; `oc voice install-neutts` downloads the model; `VoiceSynthesizeLocalTool` returns a `.wav` file without an OpenAI call.

| Task | Size | Deps | Risks |
|---|---|---|---|
| T4.1 Check NeuTTS distribution: pip-installable wheel vs. download-on-first-use model. Pin a tested version. | S | — | If it's heavy (PyTorch+CUDA), gate on optional extra `[neutts]` |
| T4.2 Add `[neutts]` to `pyproject.toml` optional-deps | S | T4.1 | — |
| T4.3 Implement `opencomputer/voice/neutts_provider.py` mirroring the OpenAI provider's interface (already exists) | M | T4.2 | API surface stays uniform |
| T4.4 `VoiceSynthesizeLocalTool` in `opencomputer/tools/voice_synthesize_local.py`. Tool registration gated on `try: import neutts`. | S | T4.3 | Missing dep = tool absent, no agent confusion |
| T4.5 `oc voice install-neutts` setup command that triggers model download with a Rich progress bar | S | T4.3 | Don't trigger on cron path |
| T4.6 Tests: `tests/voice/test_neutts_provider.py` with monkeypatched model; `tests/tools/test_voice_synthesize_local.py` for tool registration gating | M | T4.4 | — |
| T4.7 Docs: append to `docs/integrations/voice.md` | S | T4.4 | — |

Milestone-4 total: ~**M** (target: 4–6 working days).

#### Milestone 5 — Reference-repo extraction for deferred items (DOCS ONLY)

Done when: `docs/refs/openclaw/fleet-routing.md` and `docs/refs/hermes/voice-mode.md` exist, each describing the architecture in enough detail that a future milestone can implement without re-reading the source. **No code.**

| Task | Size | Deps | Risks |
|---|---|---|---|
| T5.1 Read OpenClaw's Tailscale + Bonjour + widearea-DNS + node-host modules in `dist/`; write architecture extraction | M | — | Bundled JS is hash-named; need to grep symbol names |
| T5.2 Read Hermes' `voice_mode.py` end-to-end; write extraction at `docs/refs/hermes/voice-mode.md` | S | — | — |
| T5.3 Read OpenClaw's sandboxed-browser + noVNC bridge (we already have partial notes in `docs/refs/openclaw/browser/`); finish the bridge-auth + RDP-style flow extraction | S | — | — |
| T5.4 Update `docs/superpowers/specs/2026-05-16-oc-parity-with-hermes-openclaw/PART-3-deferred.md` (new file) summarising what's deferred and why | S | T5.1–T5.3 | — |

Milestone-5 total: ~**M** (target: 3–4 working days).

### Milestone summary

| # | Milestone | Size | Calendar (1 eng) |
|---|---|---|---|
| **1** | **Sandbox scope + loop detection (MVP)** | L | 7–10 days |
| 2 | E2B sandbox backend + multi-backend resolver (was: Crabbox) | M | 5–7 days |
| 3 | Microsoft Graph tool (mail/cal/drive) | L | 7–9 days |
| 4 | NeuTTS local voice | M | 4–6 days |
| 5 | Reference extraction for deferred items | M | 3–4 days |

Total: **~5–6 calendar weeks** for one engineer working sequentially. Some parallelism possible (T1.6 can start while T1.1–T1.5 run; M2 + M4 don't depend on M1).

### Explicitly out of scope (v1)

- Fleet routing (Tailscale + Bonjour + widearea-DNS + node-host + nodes-screen). **Deferred. M5 produces docs.**
- Full-duplex voice-call over chat platforms. **Deferred. M5 produces docs.**
- Sandbox network egress rules per scope. **Deferred to a later sandbox refinement.**
- Sandboxed browser with noVNC bridge. **Deferred. Notes already partial in `docs/refs/openclaw/browser/`.**
- Microsoft Graph Teams + SharePoint. **Deferred to Graph v2.**
- NeuTTS voice cloning + custom voices. **Deferred to NeuTTS v2.**
- **Crabbox plugin (whole thing).** Removed from v1. Wrong-shape tool for OC's agent use case (Crabbox = full-VM lease for minutes-to-hours; OC needs per-call ephemeral containers, which is E2B's job). Re-evaluate only if a recurring "run OC's pytest suite on a remote VM weekly" workflow shows up.
- Modal / Vercel Sandbox / Daytona backends. Hermes ships all three. OC can add later as additional backend files (`opencomputer/sandbox/{modal,vercel,daytona}.py`) once E2B + the resolver land in M2. Same pattern; the resolver already handles N backends. **Deferred, but cheap to add post-v1.**

---

## Phase 4 — /audit-plan

Harsh critic pass. Revising the plan until it holds.

### 4.1 — Unvalidated assumptions

| Assumption | Validation status | Plan revision |
|---|---|---|
| `opencomputer/sandbox/` is functional, not stubs | **Validated in PART-1 audit; T1.1 explicitly re-checks.** | T1.1 stays as gating task. If T1.1 finds stubs, escalate to user, do NOT silently inflate milestone. |
| E2B Python SDK is stable at the version we pin | **Not yet validated.** | T2.1 reads SDK docs end-to-end; T2.2 pins a specific version range; integration tests gated on a stored E2B API key in CI secrets. |
| NeuTTS pip package exists and is maintained | **Not yet validated.** | T4.1 made gating: if NeuTTS is abandoned or wheel-broken, swap to Coqui-XTTSv2 (also offline, more maintained). |
| `mcp_oauth_manager.py` can be reused for Graph OAuth | **Not yet validated.** | Added explicit task: T3.3 reviews `mcp_oauth_manager.py` first; if reusable, T3.3 shrinks to S. If not, stays M. |
| Loop detection threshold of 3-in-8 won't false-positive on real workloads | **Unvalidated; pure design intuition.** | Added: T1.7.5 — soft-launch loop detection in **observe-only mode** first (logs trips, doesn't stop the agent) for one week of real use, then flip to enforcing. |

### 4.2 — Undersized tasks hiding real complexity

- **T1.3 "Wire scope selector into runner.py" is more than M.** Container lifecycle on `session` scope requires reading session-end signals from `SessionDB`. That's two new integration points, not one. Resize: **M→L**.
- **T2.6 "Wire resolver into `agent/loop.py`" sounds simple, but the loop already has a tool-dispatch path with caching, error-handling, and tool-result storage.** Inserting the resolver in the right place without breaking the existing dispatch contract is non-trivial. Resize: **M is right, with 1 day of buffer for integration debug.**
- **T3.2 "Minimal GraphClient" undersells paging.** Graph endpoints return `@odata.nextLink`. A real client needs pagination, not just one-shot. Resize: **M→L** (add a `paginate()` helper).
- **T4.5 model download command size = S** is fine for a happy path, but resumable download + checksum verification + cache-dir layout adds half a day. Accept: **S→M**.
- **T1.6 LoopDetector being marked M** — I underestimated. We need: window data structure, args canonicalization (sort dict keys, normalize numerics), test fixtures for the false-positive patterns, opt-out plumbing on `BaseTool`. Honest size: **M is right**, but add 2 days of buffer.

After resizing: Milestone 1 grows from 7–10 days to **9–12 days**. Milestone 2 stays. Milestone 3 grows from 7–9 to **9–11 days**.

New total: **~6–7 calendar weeks**, not 5–6.

### 4.3 — What breaks if Milestone 1 slips

Milestone 1 is the load-bearing one. If it slips:

- M2, M3, M4 do **not** depend on M1's code (only on the boundary rules being unchanged). So they can proceed.
- BUT: M2's E2B backend integrates with M1's scope policy (the resolver consumes `SandboxPolicy` from M1). If M1 slips, M2 can ship E2B in standalone mode (per-call ephemeral container, no scope=session reuse); scope-aware E2B is a follow-up. That's a feature reduction, not a regression. Accept.
- Loop detection slipping means more agent runaways in production. Mitigation: ship the observe-only mode (T1.7.5) early — it's just logging. The stopping behaviour can come later.

**Verdict:** Plan is resilient to M1 slip. M1 is the right MVP because it's the most fragile to skip, not because everything else blocks on it.

### 4.4 — Simpler path to the same outcome?

Considered: drop M3 (Microsoft Graph) entirely. Most users don't have Microsoft accounts. Replace with a generic "OAuth-protected REST" tool template that handles Graph, Salesforce, Zendesk, etc. with one tool factory.

**Rejected.** Reason: Hermes' Graph client is a real differentiator for OC's "complete tool surface" goal. A generic template doesn't replace per-API knowledge of payload shapes, paging, throttling. Keep Graph; revisit "generic OAuth REST tool" as a separate proposal post-v1.

Considered: collapse M4 (NeuTTS) into "buy a Coqui-XTTS skill." A skill that walks the user through installing Coqui and calling it via CLI.

**Rejected.** Skill-driven voice synthesis means the agent has to *recall* the skill mid-turn. Voice should be a first-class tool. Keep M4.

Considered: skip M5 (deferred-item extraction) entirely; rely on re-reading the source when the time comes.

**Rejected.** The reference repos shift. We already have partial notes in `docs/refs/openclaw/browser/` from prior extraction work — that work paid off. M5 is cheap insurance.

### 4.5 — What will I wish I'd done differently in the retro?

Pre-emptive retro hypotheses:

1. **"I should have spiked the E2B SDK first, before writing PART-1."** → Action: pre-milestone-2 spike (sign up at e2b.dev, write a 20-line Python script that creates a sandbox, runs `echo hello`, destroys it, prints duration + cost). Folded into T2.1. This validates SDK shape and pricing assumptions before T2.3 commits.
2. **"Loop detection false-positive rate was higher than the design predicted; we tripped on legitimate polling tools."** → Action: T1.7.5 observe-only mode buys us calibration data.
3. **"Sandbox scope semantics in OpenClaw were richer than we ported; we shipped session-scope without realising tool-scope needed per-tool container reuse."** → Action: T1.1's read-the-source pass MUST cover OpenClaw's `dist/sandbox-DEFCexaq.js` symbols, not just OC's own files. Updated T1.1 to require this.
4. **"Microsoft Graph's auth was an existing OAuth pattern; I duplicated `mcp_oauth_manager.py`."** → Action: T3.3 starts with the explicit "review existing OAuth plumbing" step.
5. **"NeuTTS was abandoned, and we discovered too late."** → Action: T4.1 picks the actual library AFTER checking last-commit-date + open-issues count.

All five revisions folded into the task list above.

### 4.6 — Revised plan summary

The plan that ships, after the audit:

1. **Milestone 1 (MVP):** Sandbox scope policy + tool-loop detection in observe-only-then-enforcing mode. **9–12 days.** Includes mandatory pre-task source-read of both OC's and OpenClaw's sandbox internals.

2. **Milestone 2:** E2B ephemeral-sandbox backend + multi-backend resolver. **5–7 days.** Adds `opencomputer/sandbox/e2b.py` alongside the existing docker/linux/macos/ssh backends (file layout pattern borrowed from Hermes), plus `opencomputer/sandbox/resolver.py` that picks the right backend per tool call (OC-original — Hermes only routes via one env var, read once at startup). New optional `BaseTool` fields (`sandbox_preference`, `sandbox_backend_hint`) let tool authors declare what they need. Crabbox plugin is **removed** from v1 — wrong shape for the agent use case.

3. **Milestone 3:** Microsoft Graph tool covering mail send + calendar list + OneDrive list. **9–11 days.** Includes pagination support, mandatory review of existing OAuth plumbing.

4. **Milestone 4:** NeuTTS (or substitute) local voice synthesis as optional dep. **5–7 days.** Includes maintenance-status check before picking the library.

5. **Milestone 5:** Documentation-only extraction of deferred items (fleet routing, full-duplex voice, sandboxed browser with noVNC). **3–4 days.** Produces files in `docs/refs/` for future implementers.

**Calendar:** ~6–7 weeks for one engineer. Parallelizable to ~4–5 weeks if you can run M2 + M4 concurrently with M1 (different subsystems, no shared files).

**Explicit deferrals** (no code in v1): fleet routing, full-duplex voice-call, sandbox network egress, sandboxed browser + noVNC, Graph Teams + SharePoint, NeuTTS voice cloning, Crabbox plugin (entire thing — wrong shape; E2B covers the agent-sandbox use case), Modal/Vercel/Daytona sandbox backends (cheap post-v1 adds).

### 4.7 — Pre-flight checklist before any code

Before T1.1, the engineer must:

- [ ] Confirm `~/Vscode/claude/sources/openclaw/` and `~/Vscode/claude/sources/hermes-agent/` are cloned and up-to-date (per `AGENTS.md`).
- [ ] Confirm `pytest` is green on `main` (baseline).
- [ ] Confirm `ruff check opencomputer/ plugin_sdk/ extensions/ tests/` is clean.
- [ ] Read `CLAUDE.md`, `AGENTS.md`, and `docs/sdk-reference.md` end-to-end. (Hermes uses `run_agent.py`; OC uses `agent/loop.py` — different abstractions, don't conflate.)
- [ ] Make sure `pip install -e . --no-cache-dir --no-deps && hash -r` has run since last worktree change.

If any of these fail, halt and report; don't paper over.

---

## Honest closing note

This plan deliberately leaves three of the bigger items (fleet routing, sandboxed browser + noVNC, full-duplex voice-call) out of v1. The user asked for the "best of both", and I'm telling you upfront that *the best from OpenClaw on fleet routing is multi-week unknown territory*. Shipping a partial fleet implementation in a v1 timeline would either ship broken or cause M1–M4 to slip. The honest move is to spec it out (M5) and execute it as a v2 once M1 has earned us the right to do bigger work.

If you want a different trade — for example, "drop M3 Graph and use that capacity for sandboxed browser + noVNC" — that's a swap I'm happy to make; the priorities aren't fixed. But the total budget *is* fixed at ~6–7 weeks for the listed scope. Cuts must come from somewhere if additions land.
