# OC parity with Hermes + OpenClaw — Part 1: Brainstorm & Design Audit

Date: 2026-05-16
Owner: Saksham
Working dir: `/Users/saksham/Vscode/claude/OpenComputer`
Companion file: `PART-2-plan-and-plan-audit.md`

---

## Pre-work: Self-audit of my own earlier claims

Before brainstorming, I have to correct what I told you one turn ago, because half of the "Hermes-only gap" list I rattled off is already shipped in OC. Greped on-disk, not guessed:

| Feature I claimed was a gap | Actual status in OC |
|---|---|
| MoA tool | **shipped** — `opencomputer/tools/mixture_of_agents.py`, 186 LOC |
| Batch runner | **shipped** — `opencomputer/batch.py`, 226 LOC + `agent/batch_orchestrator.py` etc. |
| OSV vulnerability scan | **shipped** — `opencomputer/security/osv_check.py`, 154 LOC |
| Tirith security scanner | **shipped** — `opencomputer/security/tirith.py`, 418 LOC |
| Voice synthesis | **partial** — OpenAI TTS wired (`voice_synthesize.py`), local NeuTTS not |
| Camoufox stealth browser | **partial** — `extensions/browser-harness/browser_camofox.py` exists; provider seam parked (`docs/browser-port/wave4-adapters/DEFERRED.md:188`) |
| Sandbox subsystem | **shipped (basic)** — `opencomputer/sandbox/` is 1151 LOC across docker/linux/macos/ssh/none — but missing scope policy, browser/noVNC bridge, remote lease |

So the real gap list is shorter than my last turn implied. The failure mode is the one I have a written rule for in `MEMORY.md` (#2): don't claim features without grepping. I burned that rule by listing Hermes features without checking OC's own source. Folding the correction into the plan instead of hiding it.

**Post-conversation revision (2026-05-16, late):** After working through "where would Saksham host OC, and what does the agent actually need from a sandbox?", the original gap #3 (Crabbox plugin) was revised. Crabbox is for *long-lived remote VM leases* (CI-style), not for *per-call ephemeral containers* (agent-tool style). The v1 ephemeral backend is E2B; Crabbox is removed from v1, kept in the deferred list. PART-2 §"Milestone 2" reflects this. Original gap #3 text below is **superseded** by the revised version.

**Terminology correction (2026-05-16, latest):** Earlier passes of this doc described the M2 design as "Hermes-style multi-backend resolver." Reading Hermes' source directly (`~/.hermes/hermes-agent/tools/environments/`) shows Hermes ships 8 backend files BUT they are consumed only by `terminal_tool.py`, with the backend picked once at process startup via `TERMINAL_ENV` env var. There is no per-tool routing, no resolver, no per-tool sandbox declaration in Hermes. **OC's M2 design extends Hermes' multi-backend file layout, but adds per-tool routing on top — that part is OC's own design, not a port.** Three motivations for going beyond Hermes-raw, all driven by Saksham's specific setup:
1. OC is hosted 24/7 on Hetzner (or similar). Env-var-once-at-startup means restarting the daemon to change sandbox behaviour. The resolver allows `oc sandbox set --backend e2b` mid-session.
2. Not every tool should be sandboxed: `Read`/`Edit` operate on the user's actual files (sandboxing them is wrong); `Bash`/`ExecuteCode` should always be sandboxed (sandboxing them is right). The resolver expresses this; the env-var approach cannot.
3. The `BaseTool.sandbox_preference` field is a 10-line additive change; the API-commitment risk is bounded by keeping the field optional with `"default"` as the default.

Where this doc earlier said "Hermes-style" without qualification, read it as "extends Hermes' multi-backend layout pattern." The per-tool routing is OC-original.

**The real verified gaps:**

1. **Sandbox scope policy** — OC has a runner per backend, but no `agent` vs `session` vs `tool` scope selector, no allow/deny tool lists per scope, no `explain` command. OpenClaw `agents.defaults.sandbox.scope` + `sandbox explain` is missing.
2. **Sandboxed browser with noVNC bridge** — referenced in `docs/refs/openclaw/browser/05-server-and-auth.md`, never built. Critical for "agent ran the browser in a box, here's what it saw" workflows.
3. **Ephemeral sandbox backend (E2B-class) + multi-backend resolver** — OC has docker/linux/macos/ssh sandbox backends but no ephemeral per-call container backend (E2B / Modal / Vercel Sandbox class), and no resolver that picks the right backend per tool call. Hermes ships 8 backends behind a resolver in `tools/environments/`; OC has the shape but neither the backend nor the resolver yet. **Crabbox** (remote VM lease, OpenClaw's separate tool) was considered and rejected for v1 — it's a CI-runner tool, not an agent-sandbox tool; wrong shape for the use case.
4. **Tool-loop detection** — zero hits for `tool_loop`/`loop_detection` in `opencomputer/`. OpenClaw ships `tool-loop-detection-*.js`. When an agent calls the same tool with the same args N times, you stop it.
5. **NeuTTS local synthesis** — only OpenAI TTS is wired. NeuTTS is offline-capable and is what Hermes ships.
6. **Microsoft Graph client** — zero presence. Hermes' `microsoft_graph_client.py` + `microsoft_graph_auth.py` give you Outlook mail/calendar, Teams, OneDrive, SharePoint via one auth. No equivalent in OC.
7. **Multi-node fleet routing** — OC's `gateway/` is single-machine. OpenClaw has Tailscale + Bonjour mDNS + widearea-DNS + node-host + nodes-screen. "My phone talks to my laptop talks to my server through one gateway" doesn't work in OC.
8. **QR pairing for iOS / mobile devices** — `channels/pairing_codes.py` exists but isn't wired to a `qr` command or a mobile companion flow.
9. **Full-duplex voice-call over chat platforms** — Hermes has `voice_mode.py`, OpenClaw has `voice-call` + `talk-voice` extensions. OC has TTS+STT tools but no live audio loop bridged to Telegram/WhatsApp/Discord voice.

That's the real list. Now I can brainstorm.

---

## Phase 1 — /brainstorm

### Goal

Bring OC to parity-or-better on the verified gaps above, without breaking the boundary rules (extensions only import `plugin_sdk/*`, core never imports extensions) and without bloating the agent loop.

### Approaches considered

#### Approach A — "All-in, one big port"

Port everything: Crabbox, NeuTTS, Graph client, fleet routing, voice-call, sandbox scope, browser-noVNC, loop detection. One enormous PR set, ~6 weeks of work.

- **Effort:** XL (5–7 weeks). Touches sandbox, gateway, channels, security, tools.
- **Risk:** High. Multi-node networking, sandbox security, and live audio are each their own rabbit hole. Conflating them means none lands cleanly.
- **Upside:** OC matches both reference implementations on one calendar.

#### Approach B — "Three vertical slices, parallel"

Carve the gap list into three independent slice teams (Slice S = Sandbox-scope + ephemeral-backend + loop-detection, Slice F = Fleet+QR+voice-call, Slice T = Tools: NeuTTS+Graph). Ship each as its own milestone train.

- **Effort:** L (4–5 weeks per slice, runnable in parallel if you had collaborators; sequentially ~10–12 weeks for one person).
- **Risk:** Medium. Slices touch different subsystems, so they don't conflict, but Slice F (fleet) has the deepest unknowns.
- **Upside:** Each slice is independently valuable and shippable.

#### Approach C — "Sandbox-first, defer the rest" (recommended)

Treat sandbox-scope + sandbox-backends + loop-detection as the **load-bearing** work (it changes how *every* tool call runs and gates whether remote execution is even safe), and defer fleet routing + voice-call to after the sandbox lands. Tools (NeuTTS, Graph) bolt on opportunistically.

- **Effort:** M for the load-bearing sandbox track (3–4 weeks), then S each for opportunistic tool additions.
- **Risk:** Low-to-medium. The big unknowns (sandbox scope semantics, ephemeral-backend SDK shape, loop-detection false-positive rate) are contained to one track.
- **Upside:** Highest-impact features land first. You get a real safety story (sandbox) before adding more network surface (fleet).

#### Approach D — "Outsource the ephemeral sandbox to E2B (or class equivalent)"

Don't build a custom ephemeral sandbox runtime. Use E2B's hosted ephemeral-container service (or Modal / Vercel Sandbox as alternatives). Add a backend file (`opencomputer/sandbox/e2b.py`) that implements the same `SandboxBackend` interface as the existing backends; the agent's tool dispatch goes through it transparently when the user configures `sandbox.backend = e2b`.

- **Effort:** S–M (~1 week for the backend file + resolver + CLI).
- **Risk:** Low. E2B owns the container infra, security, lifecycle, autoscaling. We just call the SDK.
- **Upside:** Massive leverage. Ephemeral sandboxing is *exactly* what E2B is built for, and OC's existing `sandbox/` directory already has the shape to drop a new backend in.
- **Downside:** Couples to E2B's API stability. Mitigation: keep the backend file thin (~200 LOC), pin the SDK version, define the `SandboxBackend` interface so swapping E2B → Modal → Daytona is one file each.

#### Approach E — "Skill-driven, no code"

Most of these gaps could be expressed as skills (markdown + helper code) rather than first-class OC features. Microsoft Graph → a skill that knows the auth flow + curl recipes. Voice-call → a skill that orchestrates existing voice tools + the channel plugin. Fleet routing → docs.

- **Effort:** S (1–2 weeks of skill authoring).
- **Risk:** Medium-high. Skills are reactive; the agent has to recall the skill mid-task. They don't replace tools the user wants always-available.
- **Upside:** Cheapest possible delivery.
- **Downside:** Doesn't actually achieve "parity" — features that should be first-class CLI subcommands or persistent processes (sandbox scope, loop detection, fleet routing) cannot be skills.

#### Approach F — "MCP-only catalog growth"

Reuse the existing MCP preset bet. Add Graph and NeuTTS as MCP servers (third-party or written by us). Don't grow OC core. Sandbox backends are an agent-loop concern; they can't be MCPs.

- **Effort:** S–M (3 weeks of MCP server authoring).
- **Risk:** Medium. NeuTTS is a Python model, not naturally an MCP — wrapping a model in MCP is awkward. Graph has good wrappers already. Sandbox/loop-detection/fleet **cannot** be MCP — they're agent-loop concerns.
- **Upside:** No core code changes.
- **Downside:** Same problem as Approach E for the agent-loop items.

#### Approach G — "Steal the architecture, not the code"

Read both ref repos at `~/Vscode/claude/sources/hermes-agent/` and `~/Vscode/claude/sources/openclaw/`, document the patterns in `docs/refs/`, then **rewrite** for OC's conventions (plugin SDK boundary, capability registry, hooks). No direct ports.

- **Effort:** L (4–6 weeks).
- **Risk:** Low. Forces understanding before code. Avoids licensing entanglement.
- **Upside:** Code fits OC's idioms. Test suite stays clean.
- **Downside:** Slower than copying, slower than wrapping. We've already done a partial version of this in `docs/refs/openclaw/` for the browser port.

#### Approach H — "Hybrid: C + D + targeted G"

Sandbox scope + loop detection: Approach C (build it ourselves, OC-native, load-bearing).
Ephemeral sandbox backend: Approach D, with **E2B** as the chosen provider (Crabbox rejected — wrong shape for agent tool calls).
Graph + NeuTTS: Approach G (light rewrite in OC's idioms).
Fleet routing + voice-call: explicitly defer to v2, with a docs-only stub now.

- **Effort:** M (4–5 weeks total).
- **Risk:** Low. Each item uses the cheapest sensible delivery for its kind.
- **Upside:** Best risk-adjusted ratio. Doesn't pretend fleet + voice are cheap.
- **Downside:** Three different delivery mechanisms = three different test conventions.

### Scoring

| Approach | Effort | Risk | Upside |
|---|---|---|---|
| A — All-in, one big port | XL | High | High |
| B — Three vertical slices | L–XL | Medium | High |
| C — Sandbox-first, defer rest | M | Low-Medium | High |
| D — Outsource ephemeral sandbox to E2B | S–M | Low | High (replaces a load-bearing port) |
| E — Skill-driven, no code | S | Medium-High | Low |
| F — MCP-only catalog growth | S–M | Medium | Low-Medium |
| G — Steal architecture, rewrite | L | Low | Medium |
| **H — Hybrid C + D + targeted G** | **M** | **Low** | **High** |

### Convergence

Top 3: **H, C, D**.

- **C** alone solves sandbox-scope + loop-detection but leaves the ephemeral-backend + Graph + NeuTTS for later.
- **D** alone is the cheapest single ticket but doesn't touch the agent-loop gaps.
- **H** combines both with explicit defer on fleet + voice-call.

### Winner: H

Why H wins on merit, not familiarity:

- **Risk-adjusted upside is highest.** Sandbox scope + loop detection get the OC-native treatment they need (capability registry, hooks, SDK boundary). Ephemeral execution doesn't get reinvented when E2B already provides it as a hosted service — we just add a thin backend file. Graph + NeuTTS get rewritten lightly because they need to live inside OC's conventions for caching, secrets, and consent.
- **Honest defer on fleet + voice-call.** These are multi-week unknowns. Approach A pretends otherwise; B sells "parallel slices" you can't actually parallelize when there's one engineer. H names the deferral.
- **Maps cleanly to the boundary rules.** Sandbox scope + new backends stay in `opencomputer/sandbox/` (core). E2B backend is `opencomputer/sandbox/e2b.py` alongside docker/linux/macos/ssh. Tools (NeuTTS, Graph) become new `tools/*.py` registrations — same shape OC already uses for `voice_synthesize.py`.
- **The deferred items (fleet, voice-call) are exactly the ones that need OpenClaw architecture study first.** Doing them later isn't laziness — it's "we shouldn't port what we haven't read yet."

---

## Phase 2 — /audit-design

Stress-testing approach H. Each finding is resolved or marked accepted-risk.

### 1 — Assumption check

| Assertion | Validated? | Resolution |
|---|---|---|
| OC's existing `opencomputer/sandbox/{docker,linux,macos,ssh}.py` is functional and we're just adding scope policy on top | **Unvalidated.** I read line counts, not function shape. Could be stubs. | Pre-Milestone 1 task: read each file end-to-end, write a one-page summary of what's actually implemented. If they're stubs, sandbox track becomes M→L. |
| E2B Python SDK has a stable interface OC can pin and depend on | **Unvalidated.** SDK is actively evolving. | Pre-Milestone 2 task (T2.1): read SDK docs end-to-end, run a 20-line spike (`Sandbox.create()` → `exec()` → `destroy()`); pin to a specific version range in `pyproject.toml`. |
| NeuTTS can be wrapped as a tool without bundling 1GB of model weights at install | **Unvalidated.** Need to check actual NeuTTS distribution shape. | Pre-Milestone 3 task: confirm pip-installable wheel vs. download-on-first-use model. If it's the latter, document the disk cost gate. |
| Microsoft Graph auth (device-code flow) plays nicely with OC's `auth.json` / `oauth_login` plumbing | **Partially validated** — `mcp_oauth.py` exists. Graph is a v2 OAuth source like the others, should work. | Accept-risk: implement; if it fights, fall back to PAT/client-credentials flow. |
| Tool-loop detection is a single-iteration check, not a multi-iteration state machine | **Unvalidated.** Hermes/OpenClaw both keep a window of recent tool calls. Stateless detection misses the "5 cycles of the same 3 tools" pattern. | Resolution: design for a sliding window of N=8 recent ToolCalls; loop = ≥3 identical (name+arg-hash) calls in the window. Tunable. |

### 2 — Architecture stress (edge cases)

- **Sandbox scope = `session` but session lives across multiple chat threads.** Resolution: session-scope container is keyed on `session_id` from `SessionDB`, not on chat thread. Document this explicitly.
- **E2B sandbox auto-kills mid-tool-call** (default 5-min timeout). Resolution: backend surfaces the timeout in its return value as a structured error; the agent gets an error result it can retry on a fresh sandbox or abort. No silent retries. Long-running tools can bump the timeout via a `sandbox_timeout_s` field on `BaseTool`.
- **NeuTTS model download triggered on cron job at 3 AM, fails because no terminal for progress bar.** Resolution: model-fetch is a separate `oc voice install-neutts` setup command, not lazy on first tool call.
- **Loop detection false-positive on a legitimate retry loop** (e.g. polling for build status). Resolution: tool authors can mark a tool as `loop_safe=True` in its `BaseTool` to opt out of detection. Default is `False`.
- **Graph token expires mid-task.** Resolution: refresh-token plumbing inside the Graph client, not in the tool. Same pattern as `mcp_oauth_manager.py`.

### 3 — Alternative dismissal

Approaches A, B, E, F dismissed on merit:
- **A** pretends multi-week unknowns are knowable.
- **B** sells parallelism that requires headcount you don't have.
- **E** can't replace agent-loop primitives with skills.
- **F** can't make sandbox scope an MCP server.

Approach G is partially adopted — every port reads the ref repo's architecture first. Approach D is fully adopted for the ephemeral-sandbox slot — E2B (a hosted service) is consumed rather than reimplemented.

This isn't default-choice; H was selected by elimination of the alternatives on merit.

### 4 — Requirement gap

What the user implicitly asked for but I haven't named:

- **The user wants "best of both, on OC."** That means the *experience* of running OC should feel as polished as Hermes' (deep tools) and OpenClaw's (fleet, sandbox-as-default). Adding capabilities without UX = invisible to the user. Resolution: each milestone ships a `oc <subcommand> --help` example that demonstrates the new surface.
- **Implicit need: don't break the current install.** OC is already deployed and used. Sandbox scope is a config schema change → must be backward-compatible (default scope = current behaviour).
- **Implicit need: tests.** OC ships 1,453 test files. Anything new needs equivalent coverage or the boundary tests will catch it.
- **Implicit need: docs.** OC has `docs/refs/openclaw/` and `docs/superpowers/specs/`. The reference-repo notes for the new ports go there.

### 5 — Composability

Do the four sub-tracks fit together?

- **Sandbox scope + loop detection:** loop detection runs in the agent loop, sandbox runs around tools. Don't interact. ✓
- **Sandbox scope + E2B backend:** the resolver is the integration point. Scope policy (M1) decides container lifetime per session/agent/tool; the backend (M2) decides where the container runs (host docker / E2B / etc.). Resolver consumes both. Tested via `test_resolver.py` decision matrix. Note: the resolver is OC-original; Hermes' equivalent is a single env-var read inside `terminal_tool.py`.
- **NeuTTS + Graph + existing tools:** new tools in `tools/` register the same way. ✓
- **Capability registry + new tools:** every new tool MUST declare `capability_claims`. This is already the convention. ✓

One real composability risk: **the sandbox `explain` command needs to see both core scope policy AND any plugin-registered policies.** Resolution: scope policy lives in `opencomputer/sandbox/policy.py` with a registration API that plugin extensions can hook into.

### 6 — Scope honesty

Where am I undersizing?

- **"Sandbox scope policy" sounds small but isn't.** OpenClaw's policy involves: agent vs session vs tool scope, per-scope allow/deny lists, browser-vs-common-vs-bookworm image selection, persistent vs ephemeral volumes, network egress rules per scope. Honest size: **L**, not M.
- **"E2B backend" is small only if the SDK is stable.** Honest size: **M** for the backend file + resolver + CLI; **L** if we discover the SDK's lifecycle model needs adapter code (e.g. async-only API, callback patterns).
- **"NeuTTS tool" looks like a wrapper but pulls in PyTorch deps the OC environment doesn't already have.** Honest size: **M**, includes optional-dep gating.
- **"Microsoft Graph client" is huge** if we cover mail + calendar + Teams + OneDrive. Honest size: **L** for a minimal "send mail + read calendar" version, **XL** for full parity with Hermes.
- **"Loop detection" is the smallest item.** Honest size: **S**. ~150 LOC in `agent/loop.py` + a tunable config.

Total honest size if all five ship: **3–4 calendar weeks for one engineer**, not the "M" I was about to write.

### 7 — API stability

What interfaces will outlive v1?

- `BaseTool` contract: stable, don't touch.
- Capability registry: stable, don't touch.
- New: `SandboxPolicy` interface in `plugin_sdk/sandbox.py` so plugin extensions can declare per-tool sandbox preferences. **This is a v1 commitment** — if we ship it, third-party plugins will depend on it. Resolution: keep it minimal in milestone 1; expand in subsequent versions only via additive fields.
- New: `LoopDetector` is internal to `agent/loop.py`. Not part of `plugin_sdk/`. Free to refactor.
- New: `SandboxBackend` interface in `opencomputer/sandbox/` (mirrors the file-per-backend pattern Hermes uses) and `BaseTool.sandbox_preference` / `BaseTool.sandbox_backend_hint` fields in `plugin_sdk/` (OC-original; Hermes has nothing equivalent). **These are v1 API commitments.** Resolution: keep both fields optional with safe defaults (`"default"` / `None`); the surface area is small enough that v2 changes can stay additive.
- New: NeuTTS tool extends existing voice subsystem. Voice public API in `plugin_sdk/voice.py` already exists; we don't change it.
- New: Graph tool's schema is large. Lock the auth-config shape early — auth.json schema changes are painful.

### 8 — Failure map

Per-design-choice failure modes in production:

| Choice | Production failure | Mitigation |
|---|---|---|
| Sandbox scope default = `agent` | User upgrades, every tool call now spins a Docker container the first time | Migration script: keep existing user's `sandbox.scope = none` until they opt in via `oc sandbox enable` |
| E2B SDK not installed (user skipped `[e2b]` extra) | Backend registration fails silently → resolver picks `local` fallback unconfigured → tool runs on host unexpectedly | Lazy-import SDK; if missing, backend marks itself unavailable; resolver raises clear `SandboxBackendUnavailable("install with `pip install opencomputer[e2b]`")` instead of falling back |
| NeuTTS bundled as optional dep | User installs OC fresh, voice tool returns "import error" | Tool registration is gated on `try/except ImportError`; missing → tool isn't registered, no agent confusion |
| Graph OAuth token expires | Tool returns 401 mid-conversation | Auto-refresh once; if refresh fails, return a structured error pointing at `oc auth login graph` |
| Loop detection false-positive | Agent stops on a legitimate retry pattern | Per-tool `loop_safe` opt-out; threshold-tunable; loop-trip logs to audit DB so users can post-hoc tune |
| Sandbox `explain` parser breaks on plugin-declared policies | `oc sandbox explain` shows partial output | Wrap each plugin policy load in try/except; failures show as `<error>` entries in the explain output |

### 9 — YAGNI sweep

What's in the design that no caller actually needs?

- **Fleet routing (deferred to v2).** Caught by the explicit defer in Approach H. Good.
- **Voice-call full-duplex (deferred to v2).** Same.
- **Sandbox network egress rules per scope.** Mentioned in #6 — actually do we need this in milestone 1? **No.** Default = no network restriction; "block egress to RFC1918" is a v2 feature.
- **Crabbox plugin (entire thing).** Removed from v1 scope after the post-conversation revision. Crabbox is a remote-VM-lease CI tool; the agent use case is ephemeral per-call containers, which is E2B's job. Moved to the deferred list in PART-2.
- **Graph "full parity with Hermes" version.** Cut to milestone-1-MVP scope: **send mail + read calendar + list OneDrive files**. Drop Teams + SharePoint to a later milestone.
- **NeuTTS voice cloning.** Hermes supports it. Our v1 only needs default-voice TTS.

Trimming these gets the honest size from "3–4 weeks" down to **2.5–3 weeks**.

---

## Audit conclusion

Design holds with the following accepted risks:

1. **Sandbox track size is L not M.** Plan must reflect this.
2. **E2B SDK is the v1 ephemeral-sandbox dependency.** Pin to a specific version range; document the API-key requirement and the per-second cost model. Crabbox is rejected for v1 (wrong shape: CI-runner, not agent-sandbox).
3. **NeuTTS requires extra PyTorch deps.** Optional install; gated tool registration.
4. **Graph v1 ships mail + calendar + OneDrive only.** Teams/SharePoint = v2.
5. **Fleet routing, full-duplex voice-call, sandbox network egress, Crabbox plugin (entire thing), Modal/Vercel/Daytona backends, NeuTTS voice cloning are out of scope for v1.** Explicit, named, documented in PART-2.

Proceed to PART-2 for the milestone-level plan and plan audit.
