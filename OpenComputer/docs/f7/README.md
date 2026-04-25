# OpenComputer F7 — Open Interpreter Capability (coding-harness OI bridge)

> **Phase 5 refactor (PR-3 of 2026-04-25 Hermes parity plan):** OI tools were merged into the
> coding-harness as `extensions/coding-harness/oi_bridge/` per `docs/f7/interweaving-plan.md`.
> The standalone `extensions/oi-capability/` plugin is now a deprecated compat shim — remove in next
> major release. Tools are registered via `extensions/coding-harness/plugin.py`; no separate plugin
> enable step is needed (coding-harness is enabled by default).

## What is this?

OI Capability gives OpenComputer the ability to **read your local environment + (with consent) act on it** — screenshot, OCR, file search, calendar lookup, mail metadata, optional shell + AppleScript execution. It wraps [Open Interpreter](https://github.com/OpenInterpreter/open-interpreter) (AGPL v3) inside an isolated subprocess to keep the AGPL boundary clean.

23 tools across 5 risk tiers:

| Tier | Tools | Risk | Consent surface |
|---|---|---|---|
| **1 (read-only)** | read_file_region, list_app_usage, read_clipboard_once, screenshot, extract_screen_text, list_recent_files, search_files, read_git_log | low | "Read X — output may contain personal data" |
| **2 (communication, drafts-only)** | read_email_metadata, read_email_bodies, list_calendar_events, read_contacts, send_email (drafts-only) | medium | Per-resource, with metadata-vs-body distinction |
| **3 (browser)** | read_browser_history, read_browser_bookmarks, read_browser_dom | medium | Includes "may open a browser window visibly" |
| **4 (system control, MUTATING)** | edit_file, run_shell, run_applescript, inject_keyboard | **high** | Per-action confirmation, never blanket |
| **5 (advanced)** | extract_selected_text, list_running_processes, read_sms_messages | medium-high | Per-action with expanded explanation |

## Safety guarantees

These are the load-bearing rules — verifiable in code:

1. **Integrated into coding-harness.** OI tools are registered by `extensions/coding-harness/plugin.py` (PR-3). The bridge starts disabled at the capability level — individual tools require explicit user consent per their tier before executing.

2. **AGPL boundary** — OI is AGPL v3. Our wrapper NEVER imports `interpreter` directly; OI runs only inside `extensions/coding-harness/oi_bridge/subprocess/server.py`. **CI test** (`tests/test_coding_harness_oi_agpl_boundary.py`) greps the codebase for `import interpreter` outside the subprocess dir — any match fails the build. Detected on every PR.

3. **Telemetry kill-switch.** OI ships hardcoded PostHog telemetry (`interpreter/core/utils/telemetry.py`). Our subprocess server replaces the telemetry module with a no-op **before** any OI import via `sys.modules` patching. Belt-and-suspenders: the subprocess also blocks egress to `posthog.com` at the network level. Test: `test_coding_harness_oi_telemetry_disable.py` patches `requests.post` with a fail-loudly assertion and runs the dispatcher — never fires.

4. **Subprocess isolation.** OI runs in a separate Python venv at `<profile_home>/oi_capability/venv`. JSON-RPC over stdin/stdout — zero network exposure even on localhost.

5. **Resource limits.** Subprocess gets a 4 GB RAM cap (configurable). Stops a runaway OCR / image processing call from eating your RAM.

6. **Per-tool `enabled` flag.** Beyond the global plugin disable, you can disable individual tools (e.g. only allow Tier 1, never Tier 4). Useful for users who want read-only.

7. **Tier 4 (mutating) enforces per-action consent.** No "allow LLM to run shell for the next hour" blanket grants; every `run_shell` invocation prompts.

8. **`send_email` is drafts-only.** Plugin wrapper rejects `send_now=True`. Email goes to your draft folder; YOU send it from your email client. Never auto-sends.

9. **Subprocess audit log** at `<profile_home>/oi_capability/subprocess.log` (rotated). Every OI invocation traceable.

10. **No auto-install of OI's heavy deps.** Subprocess venv installs OI with a minimal `requirements.txt` (no torch, no opencv) by default. Heavy deps installed only if a tool that needs them is invoked.

## What you can do today (Phase 5 complete)

**23 OI tools are registered via `extensions/coding-harness/plugin.py`** as part of the coding-harness.
Architecture: `docs/f7/design.md`. Upstream deep-scan: `docs/f7/oi-source-map.md` (578 lines, AGPL
audit included). Phase 5 refactor contract: `docs/f7/interweaving-plan.md`.

## Phase status

| Phase | Status | What ships |
|---|---|---|
| **C1** | ✅ Landed | Deep-scan + design doc + interweaving plan + this README |
| **C3** | ✅ Landed | Plugin skeleton: subprocess wrapper, JSON-RPC protocol, telemetry kill-switch, venv bootstrap, 23 tools, AGPL boundary CI test, ~95-105 tests |
| **C5** | ✅ Landed | 8 use-case libraries: autonomous code refactoring, life-admin/calendar, personal-knowledge-management, proactive security monitoring, dev-flow assistant, email triage + draft generation, context-aware code suggestions, temporal pattern recognition |
| **Session A Phase 5 (PR-3)** | ✅ **Complete — 2026-04-25** | Wired ConsentGate via `capability_claims` on each tool class (F1 auto-enforces at dispatch). SANDBOX_HOOK pending 3.E wrapper API match (see tier_4/5 comments). Refactored `extensions/oi-capability/` → `extensions/coding-harness/oi_bridge/` per `docs/f7/interweaving-plan.md`. Old plugin is now a compat shim. |

## Setup (Phase 5 complete)

```bash
# OI bridge is now part of coding-harness — no separate plugin enable needed.
# First-use: the subprocess venv is bootstrapped on first tool call.
opencomputer oi-capability doctor       # verifies bootstrap + telemetry-disabled + AGPL boundary
```

## FAQ

**Why AGPL discipline if you're just wrapping it?** AGPL v3 includes a "network use is distribution" clause. Even though we don't redistribute OI binaries, importing it as a Python library would arguably make our wrapper a derivative work — bringing the AGPL obligation. Subprocess isolation is the consensus-clean separation.

**Will the agent run shell commands on my machine without asking?** No. Tier 4 (mutating) tools require **per-action consent**, never blanket. Even with the plugin enabled, every `run_shell` prompts you.

**Can I disable Tier 4-5 entirely?** Yes — per-tool `enabled: bool` flag in the plugin config. Set `tier_4_enabled: false` to ban shell + AppleScript + keyboard injection while keeping Tier 1-3 read-only access.

**What happens if OI's subprocess crashes?** The wrapper detects on next call and respawns. Audit log captures the crash. No data loss; no agent loop disruption.

**Does the subprocess send any data to the OI maintainers?** No — telemetry kill-switch (point 3 above). Verified by test on every CI run.

**Is this safe to enable on a corp Mac?** Maybe — depends on your IT policy. Ask your security team. The AGPL boundary, telemetry kill-switch, and per-tier consent give you a defensible posture, but `run_shell` access is a serious capability. Consider running with Tier 4-5 disabled.

**What if I regret enabling and want everything gone?**
```bash
opencomputer plugin disable oi-capability
rm -rf ~/.opencomputer/oi_capability   # nukes venv + audit log
```

**Why not use Anthropic's `computer_use` directly instead?** Partially considered (see `design.md` §Alternative #4). For Anthropic users, Phase 5 routes overlapping tools (display, keyboard, mouse, bash) through `computer_use` natively. For non-Anthropic providers + non-overlapping tools (mail, calendar, SMS), OI subprocess is the path.

---

*Last updated: 2026-04-25 — Phase 5 refactor complete (PR-3). Updated paths to reflect oi_bridge location.*
