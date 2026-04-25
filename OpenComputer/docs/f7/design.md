# OpenComputer F7 — Open Interpreter Capability Plugin Design

> **Phase C1 design doc.** Companion to `oi-source-map.md` (OI deep-scan).
> Audience: Session C implementers + Session A integration owner (especially Phase 5 coding-harness refactor) + Session B reviewers.

---

## 1. Goal (recap)

Wrap [Open Interpreter](https://github.com/OpenInterpreter/open-interpreter) (**AGPL v3**) as the standalone `extensions/oi-capability/` plugin. The plugin runs OI in a strictly isolated subprocess (separate Python venv, JSON-RPC over stdio), exposes 23 curated tools across 5 risk-tiered groups, and disables OI's PostHog telemetry FIRST in the subprocess bootstrap. Per the master plan, this plugin will later be **refactored by Session A's Phase 5 into the coding-harness as a bridge layer** — but **C3 ships it standalone** to avoid touching coding-harness territory.

---

## 2. AGPL boundary discipline (non-negotiable)

OI is AGPL v3 (confirmed in source map). The legal posture:

| What is | License implication |
|---|---|
| OI installed in a separate venv as a binary subprocess | ✅ Acceptable — we use it as a tool, not as a library |
| Importing `from interpreter import ...` from our codebase | ❌ **Forbidden** — would AGPL-contaminate our wrapper |
| Distributing OI binaries with our installer | ⚠️ Acceptable but requires source-availability obligation; we install on-demand via pip into the venv instead |
| Modifying OI source | ❌ **Forbidden** in our wrapper; if we need patches, file upstream PRs |

**CI lint test (C3 deliverable):** `tests/test_oi_agpl_boundary.py` greps the entire codebase OUTSIDE `extensions/oi-capability/subprocess/` for any `import interpreter` or `from interpreter` line. Any match fails the build. This is the load-bearing guarantee.

The **subprocess server script** at `extensions/oi-capability/subprocess/server.py` is the ONLY file that imports OI. It runs in OI's venv, isolated from our codebase by both filesystem and process boundary.

---

## 3. Subprocess architecture

```
agent loop  →  TierN_Tool.execute(args)
                   │
                   ├─ ConsentGate.require(...)        ← Session A wires in Phase 5
                   ├─ SandboxStrategy.guard(...)      ← Session A wires in Phase 5
                   ├─ JSON-RPC request → subprocess stdin
                   │      {"jsonrpc": "2.0", "id": N, "method": "computer.files.search", "params": {...}}
                   │
                   │  subprocess (OI venv at <evolution_home>/oi_venv/bin/python):
                   │      ├─ telemetry_disable.py FIRST → patches PostHog to no-op
                   │      ├─ from interpreter import OpenInterpreter   (only here)
                   │      ├─ dispatch loop reads JSON-RPC from stdin
                   │      ├─ executes against `interpreter.computer`
                   │      └─ writes JSON-RPC response to stdout
                   │
                   ├─ JSON-RPC response ← subprocess stdout
                   ├─ AuditLog.append(...)             ← Session A wires in Phase 5
                   └─ return Tool result
```

**Why JSON-RPC over stdio (not HTTP / WebSocket):**
- Zero network exposure — even localhost ports can leak in shared dev environments
- Built-in lifecycle: subprocess dies → no orphaned listener
- stdin/stdout buffering is well-understood (asyncio.subprocess handles it)
- Trivial to stress-test with mock subprocess

---

## 4. Telemetry kill-switch (CRITICAL)

Source map confirms: PostHog is hardcoded at `interpreter/core/utils/telemetry.py` line 52, with the API key exposed in source. Every OI invocation triggers usage events.

**Our discipline**:

```python
# extensions/oi-capability/subprocess/telemetry_disable.py
"""MUST be the FIRST import in subprocess/server.py — before any 'from interpreter'."""

import sys

# Pre-empt the module: register a fake telemetry module so subsequent
# 'from interpreter.core.utils import telemetry' gets our no-op instead.
class _NoopTelemetry:
    @staticmethod
    def send_telemetry(*args, **kwargs):
        return None
    @staticmethod
    def get_distinct_id():
        return "opencomputer-subprocess"

class _NoopModule:
    send_telemetry = _NoopTelemetry.send_telemetry
    get_distinct_id = _NoopTelemetry.get_distinct_id

sys.modules["interpreter.core.utils.telemetry"] = _NoopModule()
```

```python
# extensions/oi-capability/subprocess/server.py
import sys

# CRITICAL ORDERING — telemetry_disable MUST run before OI imports
from . import telemetry_disable  # noqa: F401, E402

# Now safe to import OI
from interpreter import OpenInterpreter  # noqa: E402
```

**Test (C3 deliverable):** `tests/test_oi_telemetry_disable.py` patches `requests.post` with a "fail loudly" assertion; runs the subprocess server with a no-op JSON-RPC dispatch; verifies `requests.post` was NEVER called.

---

## 5. The 23 tools across 5 tiers

Source map maps each to its OI module. Tiering reflects safety risk (Tier 1 = pure read; Tier 5 = arbitrary code execution).

### Tier 1 — Introspection (8 tools, read-only, lowest risk)

| Tool | OI module | Platform | Notes |
|---|---|---|---|
| `read_file_region` | `core/computer/files/files.py::Files.read` (custom wrapper) | all | Read a slice of a file (offset + length) — never the whole file by default |
| `list_app_usage` | OS module + `psutil` | macOS/Linux | Recently-active apps (last N hours) |
| `read_clipboard_once` | `core/computer/clipboard/clipboard.py::Clipboard.view` | all | Single read, never streamed |
| `screenshot` | `core/computer/display/display.py::Display.view` | all | Returns base64 PNG; redactor pass needed (PII in screenshots) |
| `extract_screen_text` | `core/computer/display/display.py::Display.ocr` | all | OCR via Tesseract; output is plain text |
| `list_recent_files` | OS-specific (Spotlight/locate) | macOS/Linux | Files modified in last N hours |
| `search_files` | `core/computer/files/files.py::Files.search` | all | aifs-backed; query string |
| `read_git_log` | shell wrapper (no OI dependency!) | all | Plain `git log` parser; doesn't even need OI subprocess |

### Tier 2 — Communication (5 tools, reads + drafts)

| Tool | OI module | Notes |
|---|---|---|
| `read_email_metadata` | `core/computer/mail/mail.py` | Headers only (from/subject/date); no body |
| `read_email_bodies` | `core/computer/mail/mail.py` | **Stricter consent** — full body text |
| `list_calendar_events` | `core/computer/calendar/calendar.py` | EventKit on macOS |
| `read_contacts` | `core/computer/contacts/contacts.py` | Contacts.app via AppleScript |
| `send_email` | `core/computer/mail/mail.py::Mail.send` | **DRAFTS-ONLY** — saves to draft folder, NEVER auto-sends. Wrapper rejects if `send_now=True`. |

### Tier 3 — Browser (3 tools)

| Tool | OI module | Notes |
|---|---|---|
| `read_browser_history` | `core/computer/browser/browser.py` (custom + sqlite) | Reads Chrome/Safari history sqlite directly, no Selenium |
| `read_browser_bookmarks` | same | sqlite read |
| `read_browser_dom` | `core/computer/browser/browser.py` (Selenium) | **Stricter consent** — drives a browser; user sees it open |

### Tier 4 — System Control (4 tools, MUTATING — highest consent bar)

| Tool | OI module | Notes |
|---|---|---|
| `edit_file` | `core/computer/files/files.py::Files.edit` | String replacement; **per-edit consent prompt** |
| `run_shell` | `core/computer/terminal/terminal.py::Terminal.run` | **Stricter consent + sandbox**; output captured, not streamed |
| `run_applescript` | macOS only; `osascript` subprocess | macOS app control; consent per-app |
| `inject_keyboard` | `core/computer/keyboard/keyboard.py::Keyboard.write` | Types text — **stricter consent**; user can't intercept once started |

### Tier 5 — Advanced (3 tools, niche)

| Tool | OI module | Notes |
|---|---|---|
| `extract_selected_text` | OS clipboard hack (Cmd+C → read) | macOS only initially |
| `list_running_processes` | psutil | All platforms |
| `read_sms_messages` | `core/computer/sms/sms.py` | macOS chat.db read; **sticter consent** — entire iMessage history is sensitive |

---

## 6. Per-tier consent surface (Phase 5 wiring spec)

| Tier | Consent prompt language template |
|---|---|
| 1 | "Read [resource] — output may contain personal data." (single grant per session) |
| 2 | "Access your [email/calendar/contacts] data: [METADATA / FULL BODIES]." (per-resource, with metadata vs body distinction) |
| 3 | "Read browser data: [HISTORY / BOOKMARKS / OPEN A BROWSER WINDOW]." |
| 4 | "**Modify your system**: [EDIT FILE / RUN COMMAND / SCRIPT APP / TYPE KEYS]. Per-action confirmation required." (per-action, never blanket) |
| 5 | "Access [advanced capability]." (per-action, with expanded explanation) |

Session A's Phase 5 wires these into ConsentGate. C3 ships the per-tool `consent_tier: int` metadata so Phase 5 can route correctly.

---

## 7. JSON-RPC protocol

```python
# Request (parent → subprocess)
{
    "jsonrpc": "2.0",
    "id": <int>,                   # caller-assigned correlation id
    "method": "<computer.module.method>",  # e.g. "computer.files.search"
    "params": {<kwargs>}
}

# Response (subprocess → parent)
{
    "jsonrpc": "2.0",
    "id": <same int>,
    "result": <serializable>       # success
    # OR
    "error": {"code": <int>, "message": str, "data": ...}  # failure
}
```

Errors map to JSON-RPC standard codes (-32700 parse error, -32601 method not found) plus our app-specific range -32000 to -32099 (e.g., -32001 = consent denied, -32002 = sandbox violation).

**Stream handling:** subprocess flushes after each response. Wrapper reads line-delimited JSON. No fancy streaming — keeps protocol trivially testable.

---

## 8. Venv bootstrap

```python
# extensions/oi-capability/subprocess/venv_bootstrap.py
def ensure_oi_venv() -> Path:
    """Lazy-create <evolution_home>/oi_venv with open-interpreter installed.

    Returns path to the venv's python binary. Idempotent — re-running just
    returns the existing path. Pin OI version via OPENCOMPUTER_OI_VERSION env
    var (default: a known-good version we've audited).
    """
```

The plan to put it under `<evolution_home>` is a misnomer — OI isn't part of evolution. Better: put it under `<plugin_state_home>/oi_capability/venv` per Session A's per-plugin-state convention (when defined). For C3 MVP, use `<_home() / "oi_capability" / "venv">`.

**On Windows**: pip install of OI may fail without build tools. The bootstrap surfaces a clear error with platform-specific install guidance. C3 deliberately defers Windows test coverage to Session A's Phase 5.

---

## 9. Test plan (C3 spec)

| Test file | What it covers | ~Tests |
|---|---|---|
| `test_oi_subprocess_wrapper.py` | JSON-RPC roundtrip with mock subprocess; timeout handling; correlation-id matching | 12-15 |
| `test_oi_protocol.py` | Request/response schema validation; error code mapping | 8-10 |
| `test_oi_telemetry_disable.py` | Verifies `requests.post` is NEVER called even after a "computer" call (mocks the entire OI module) | 4-6 |
| `test_oi_venv_bootstrap.py` | Lazy creation; idempotent; clear error on missing pip; honors `OPENCOMPUTER_OI_VERSION` | 6-8 |
| `test_oi_tools_tier_1_introspection.py` | 8 tool stubs schema correctness + mocked execute() | 16 |
| `test_oi_tools_tier_2_communication.py` | 5 tools; **send_email enforces drafts-only** | 12 |
| `test_oi_tools_tier_3_browser.py` | 3 tools | 9 |
| `test_oi_tools_tier_4_system_control.py` | 4 tools; `run_shell` honours dry-run | 14 |
| `test_oi_tools_tier_5_advanced.py` | 3 tools | 9 |
| `test_oi_agpl_boundary.py` | **CI guarantee**: greps for `import interpreter` outside `subprocess/` dir; fails build on any match | 3 |

Target ~95-105 new tests. Mocking strategy: subprocess is mocked (no real OI invocation in CI); only the wrapper logic + protocol parsing is exercised.

---

## 10. Manifest

```json
{
  "id": "oi-capability",
  "name": "Open Interpreter capability",
  "kind": "tools",
  "version": "0.1.0",
  "enabled_by_default": false,
  "description": "Wraps Open Interpreter as a sandboxed subprocess. AGPL v3 — kept isolated.",
  "schema_version": 1
}
```

---

## 11. What ships in C3 vs what waits for Session A's Phase 5

**C3 ships:**
- Subprocess wrapper, protocol, telemetry disable, venv bootstrap
- 23 tool classes with `schema` + `execute()` (mocked-subprocess paths in tests)
- AGPL boundary CI test
- LICENSE + NOTICE attribution
- 95-105 tests

**Session A wires in Phase 5:**
- ConsentGate per-tier wiring
- SandboxStrategy guards on Tier 4 (run_shell, edit_file, etc.)
- AuditLog appends for every tool call
- **Coding-harness interweaving** — refactor `extensions/oi-capability/` into `extensions/coding-harness/oi_bridge/` per master plan §F7
- `enabled_by_default` flip (only after legal review)

The interweaving plan is documented separately at `docs/f7/interweaving-plan.md` (next file).

---

# Part I — Self-audit (expert critic)

## Flawed assumptions

1. **"Telemetry kill-switch via `sys.modules` patch is bulletproof."** Wrong. If OI lazy-imports `telemetry` in a way that bypasses module cache (rare but possible — `importlib.reload`, or `__import__` with absolute path), our patch is bypassed. **Mitigation:** add a network-level check too — block egress to `posthog.com` from the subprocess's process tree. Belt-and-suspenders. Detect violation in `test_oi_telemetry_disable.py` by mocking the entire `requests` library.

2. **"OI's `computer.X.Y(args)` API is stable."** OI 0.4.x has been refactoring rapidly. Method signatures change between minor versions. **Mitigation:** pin a specific OI version in `venv_bootstrap.py`; bump only after re-running the source map.

3. **"Subprocess venv install completes in reasonable time."** OI has heavy deps (PyTorch optional, Selenium, OpenCV optional). Cold install can take 5+ minutes. **Mitigation:** install only the minimal set (no torch, no opencv) by pinning a `requirements.txt` we maintain that excludes the heavy stuff. Document the trade-off.

4. **"23 tools across 5 tiers is the right curation."** Speculation. Some tools (read_sms, run_applescript) may be too sensitive even with consent; others may be too niche to bother with. **Mitigation:** ship 23 but ALSO ship per-tool `enabled: bool` flag in the plugin config, so users can disable subsets they don't trust.

5. **"Drafts-only `send_email` is enforceable in our wrapper."** OI's `Mail.send` may not support a draft mode. We may need to introspect Apple Mail's drafts folder and write directly. **Mitigation:** if OI doesn't expose drafts, our wrapper writes via AppleScript directly (bypassing OI for this one operation). Document the deviation.

6. **"AGPL CI lint catches all contamination."** The lint catches direct imports. Doesn't catch indirect contamination (e.g., copying a snippet from OI into our wrapper). **Mitigation:** include a code-review checklist item: "Did this PR copy any OI source code into our wrapper?" (Answer must be no.)

## Edge cases not addressed

1. **Subprocess crashes mid-call.** Our wrapper hangs waiting on response. **Mitigation:** per-call timeout (default 60s; configurable per tool). On timeout, kill subprocess + raise.

2. **Subprocess dies between calls.** Our wrapper detects on next call and respawns. **Mitigation:** `subprocess.poll()` check before each request; respawn on dead.

3. **OI's venv gets corrupted.** Disk full mid-pip-install. **Mitigation:** bootstrap detects partial venv (no `python` binary, missing `interpreter` package); offers to nuke + reinstall.

4. **Multiple concurrent tool calls.** Our protocol uses correlation ids — but if the subprocess is single-threaded (it is, by default), serializes. **Mitigation:** wrapper queues calls; documents that tool latency may stack. Future: pool of 3 subprocesses.

5. **OI prompts the user interactively.** OI was designed for an interactive terminal; some operations may try to read from stdin. **Mitigation:** subprocess server hardcodes `interpreter.auto_run = True` (skip confirmations) AND captures stdin to ensure no read. If OI insists, raise.

6. **Telemetry leaks via OI's `litellm` dep.** litellm has its own telemetry. **Mitigation:** patch `litellm._turn_off_message_logging` + add `litellm.telemetry = False` in the subprocess server bootstrap.

7. **OI uses `print()` for diagnostic output.** Pollutes stdout, breaks JSON-RPC parsing. **Mitigation:** subprocess server redirects stdout to BytesIO; only the JSON-RPC dispatcher writes to actual stdout via `sys.__stdout__`.

8. **Apple Silicon vs Intel vs Linux x86 vs Linux ARM.** OI's deps may have arch-specific wheels. **Mitigation:** subprocess venv install respects platform; document that first-time install on Apple Silicon downloads ~500 MB.

## Missing considerations

1. **Coding-harness interweaving plan** — Session A's Phase 5 will refactor our `oi-capability/` into `coding-harness/oi_bridge/`. We need to design our module boundaries so the refactor is mechanical (no rewrite). **Mitigation:** write `docs/f7/interweaving-plan.md` (deliverable C1.5).

2. **Telemetry beyond PostHog.** OI may add more telemetry vectors in future versions. **Mitigation:** version-pin (already in §8); audit at every bump.

3. **Subprocess audit log.** OI's stderr should land somewhere. **Mitigation:** stderr → `<_home() / "oi_capability" / "subprocess.log">` (rotated); accessible via `opencomputer oi-capability logs` CLI in Phase 5.

4. **Sandboxing on macOS via `sandbox-exec`**. Apple's sandbox-exec can confine the subprocess to specific syscalls / file paths. **Mitigation:** out of C3 scope; flagged for Session A's Phase 5 sandbox strategy.

5. **Resource limits.** OI's image OCR can consume gigs of RAM. **Mitigation:** subprocess spawned with `resource.setrlimit(RLIMIT_AS, ...)` cap (default: 4GB). Configurable.

6. **Cleanup on plugin disable.** Disabling the plugin should kill the subprocess + (optionally) delete the venv. **Mitigation:** plugin's `unregister(api)` hook (when SDK supports it) handles this.

## Refinements applied to plan

- **Network-level egress block** added as belt-and-suspenders for telemetry.
- **Pin OI version** in venv_bootstrap; document upgrade procedure.
- **Minimal requirements.txt** — exclude heavy deps (torch, opencv) by default.
- **Per-tool `enabled: bool` flag** in plugin config.
- **Drafts-only via direct AppleScript** as fallback if OI doesn't expose draft mode.
- **Code-review checklist** for AGPL contamination.
- **Subprocess timeout + respawn** plumbing.
- **Stdout redirection** to prevent JSON-RPC pollution.
- **stderr → log file** for diagnostics.
- **Resource limits** (RAM cap) on subprocess.
- **Interweaving plan** as a separate doc.

---

# Part II — Adversarial self-review

## Alternative #1 — Reimplement OI's capabilities in our own code (rejected)

**Shape:** Skip OI entirely. Write our own screenshot, OCR, file-search, etc.

**Pros:** No AGPL. No subprocess overhead. No version skew. Full control.

**Cons:** Each capability is non-trivial. AppleScript scripting alone is days of work. OCR via Tesseract is well-trodden but error-prone at the edges. We'd reinvent OI's wheel poorly.

**Verdict:** Rejected for breadth. **Partial accept**: where we'd reimplement anyway (e.g., `read_git_log` is just `git log` parsing), do it inline without OI. Source map already flagged this — `read_git_log` doesn't need OI subprocess at all.

## Alternative #2 — Use a different multi-capability framework (rejected)

**Shape:** AutoGPT, AgentGPT, or other agent frameworks have similar capabilities under MIT/Apache.

**Pros:** Permissive licenses; no AGPL.

**Cons:** Most are abandoned, less mature, or have wider scope (full agent loop) that we don't need. OI's per-capability granularity and active maintenance are uniquely valuable.

**Verdict:** Rejected. OI is the right tool despite the AGPL friction.

## Alternative #3 — Ship OI integration ONLY for Tier 1 (read-only) tools; defer 2-5 (partial accept)

**Shape:** C3 ships only Tier 1 (8 tools). Tiers 2-5 wait for Session A's sandbox strategy in Phase 5.

**Pros:** Lower-risk MVP. Tier 1 is read-only — even with consent gates absent, blast radius is small.

**Cons:** Half the value of the plugin missing. Plan §C3 spec assumes all 23 ship.

**Verdict:** PARTIAL ACCEPT — flag as a fallback if Session A's Phase 5 timeline slips. C3 default ships all 23 (each gated by `enabled: bool` flag from §13.4 above), but if pre-PR review reveals Tier 4-5 are too risky to merge without sandbox, ship Tier 1-3 first and Tier 4-5 in C3.5.

## Alternative #4 — Use Anthropic's `computer_use` directly (related, partial accept)

**Shape:** Skip OI; use Anthropic's `computer_use_2024_10_22` tools natively (already supported by Claude). Talk directly to the model with the standard tools.

**Pros:** Native to Claude; no AGPL; no subprocess; consent surface is built-in.

**Cons:** Locks us to Anthropic. OpenAI / Gemini users get nothing. Also: Anthropic's `computer_use` is more limited than OI's surface (no email/calendar/SMS).

**Verdict:** PARTIAL — for Anthropic users, route via `computer_use` natively for the overlap surface (display, keyboard, mouse, bash). For non-Anthropic users + non-overlapping tools (mail, calendar, SMS), use OI subprocess. This is a routing decision in Phase 5.

## Hidden assumptions surfaced

1. **"Subprocess venv is universally bootstrapable."** Some user environments (corporate Macs with restricted pip) may block the bootstrap. **Mitigation:** detect + emit clear error with workaround (manual install path).

2. **"OI's licensing posture is stable."** AGPL today doesn't mean AGPL tomorrow. **Mitigation:** document version pin + license fingerprint at bootstrap time.

3. **"23 tools cover real user needs."** Speculation. Some users may want read-only-everything (Tier 1 only); others may want write-anywhere (full Tier 4). **Mitigation:** per-tool `enabled` flag enables this.

4. **"Tests adequately mock the subprocess."** Real subprocess behavior (signal handling, stdout buffering edge cases, OOM kills) is hard to mock. **Mitigation:** add ONE integration test (Phase 5) that runs actual subprocess against actual OI on macOS — confirms the wire protocol works end-to-end. Out of C3 scope.

5. **"Phase 5 will refactor cleanly."** Coding-harness's existing module boundaries may not align with our `oi-capability/` layout. **Mitigation:** the interweaving plan doc is the contract — Session A reviews + signs off before C3 starts; if changes needed, fold them into C3 design now.

## Quantified uncertainty

| Claim | Confidence | Swing |
|---|---|---|
| Subprocess wrapper effort ~1 week | 70% | Could double if JSON-RPC stream debugging gets thorny |
| 23 tools wire cleanly via mocked subprocess | 75% | Some tools may need OI version-specific handling |
| AGPL CI lint catches all contamination | 85% | Mostly catches imports; can miss copy-paste contamination (mitigation: code-review checklist) |
| Telemetry kill-switch holds across OI versions | 70% | OI could rearrange the telemetry module path; would need a re-test on every bump |
| Phase 5 refactor is mechanical (per interweaving plan) | 50% | Coding-harness internals are Session A's domain; we have limited visibility |

## Worst-case edges

**WC1 — Telemetry leaks despite our patch.** Detected too late = data already exfiltrated. **Mitigation:** belt-and-suspenders — module patch + network-level egress block + integration test that asserts no `requests.post` calls fired during a real OI invocation.

**WC2 — OI subprocess executes a malicious payload.** OI is Turing-complete; a clever prompt could escape. **Mitigation:** sandbox strategy in Phase 5 (Session A scope); for C3 MVP, document that the subprocess is NOT sandboxed yet — plugin must remain disabled by default.

**WC3 — venv install pulls in a compromised pip package.** Supply-chain attack on OI's deps. **Mitigation:** version-pin all deps in our `requirements.txt`; manual review on bumps.

**WC4 — JSON-RPC parse error wedges the subprocess.** **Mitigation:** subprocess server wraps every dispatch in try/except; on parse error, returns standard JSON-RPC -32700 + continues serving.

**WC5 — User has multiple OI versions installed system-wide.** Our subprocess venv is isolated, so this doesn't matter — but documenting it avoids confusion.

## Refinements applied after adversarial review

- **Network egress block** added as second layer of telemetry defense.
- **Per-tool `enabled` flag** in plugin config so users can disable subsets.
- **Anthropic `computer_use` routing** as Phase 5 enhancement (Alternative #4 partial accept).
- **Tier 1-only fallback** documented as risk-mitigation if Phase 5 slips (Alternative #3 partial accept).
- **`read_git_log` NOT routed through OI subprocess** — implemented inline in our wrapper (zero AGPL exposure, simpler).
- **Single end-to-end integration test in Phase 5** to validate wire protocol against real subprocess.
- **Code-review checklist** for AGPL copy-paste contamination.
- **Version pin + license fingerprint** at bootstrap time.

---

## 12. Status

C1 design locked. C3 implementation maps 1:1 to §3 architecture sketch and §5 tier structure. PR review by Session A confirms:
- The interweaving plan (separate file) is acceptable for Phase 5 refactor.
- Tier-level consent surface design is compatible with ConsentGate's intended granularity.
- `read_git_log` carve-out (no OI subprocess) is acceptable.
- Default-disabled-with-per-tool-flags model.

**Coordination items for Session A** (please flag in PR review):
- Sandbox strategy timeline — Tier 4-5 unsafe without it.
- Anthropic `computer_use` routing — accept for Phase 5 or defer?
- Per-plugin state directory convention (where venv lives).
- AGPL contamination code-review checklist — does Session A want this in the global PR template?

---

## 16. Phase 5 refactor complete — 2026-04-25 (PR-3)

Session A's Phase 5 was completed on 2026-04-25 as PR-3 of the Hermes parity plan
(`docs/superpowers/plans/2026-04-25-hermes-parity-and-coordination-items.md`).

### What was done

1. **Files moved** — `extensions/oi-capability/subprocess/` and `extensions/oi-capability/tools/`
   were moved to `extensions/coding-harness/oi_bridge/` via `git mv`. The oi_bridge `__init__.py`
   was created separately (worktree already had the moves staged).

2. **Imports updated** — All `extensions.oi_capability.*` references inside moved files were updated
   to `extensions.coding_harness.oi_bridge.*` via sed. Tier files (tier_1 through tier_5) already had
   correct relative imports (`from ..subprocess.wrapper import OISubprocessWrapper`).

3. **ConsentGate wiring** — `capability_claims` class attrs were already declared on all 23 tool
   classes (the branch had this done). F1 ConsentGate auto-enforces at dispatch — no `ConsentGate.require()`
   call needed in `execute()`. All `# CONSENT_HOOK` and `# AUDIT_HOOK` markers were replaced.
   Counts: 23 `capability_claims` declarations (8 tier-1, 5 tier-2, 3 tier-3, 4 tier-4, 3 tier-5).

4. **SANDBOX_HOOK status** — All 7 Tier 4-5 tools have `# SANDBOX_HOOK pending 3.E API match`
   comments explaining why wiring was deferred. The OI subprocess IS the subprocess boundary;
   direct `run_sandboxed()` wiring would require extracting the argv from inside the JSON-RPC wrapper
   call, which `OISubprocessWrapper` doesn't expose. Needs `wrapper.pre_exec_hook` in 3.E.
   Tracking: `docs/f7/sandbox-wiring-todos.md` (create when 3.E exposes the hook).

5. **coding-harness/plugin.py** — OI Bridge section added with try/except guard to register all 23
   tools via `ALL_TOOLS` lists on each tier module.

6. **Tests moved** — 10 `test_oi_*.py` files renamed to `test_coding_harness_oi_*.py` via `git mv`.
   Imports updated from `extensions.oi_capability.*` to `extensions.coding_harness.oi_bridge.*`.
   `test_oi_use_cases_*.py` (8 files) left as-is per spec.

7. **conftest.py** — Added `extensions.coding_harness` → `extensions/coding-harness/` alias
   (mirrors the `oi_capability` alias pattern). Kept the `oi_capability` alias for use_cases tests.

8. **AGPL CI guard** — `ALLOWED_PATH` updated to point at
   `extensions/coding-harness/oi_bridge/subprocess/server.py`.

9. **Compat shim** — `extensions/oi-capability/__init__.py` updated with DeprecationWarning.
   `plugin.py` made a no-op stub. `plugin.json` marked as deprecated.

10. **Docs** — `docs/f7/README.md` updated with new paths + phase-5-complete status. This §16 added.

### What was deferred

- **SANDBOX_HOOK wiring** for all Tier 4-5 tools — pending `OISubprocessWrapper` exposing a pre-exec
  hook so `run_sandboxed()` can wrap the subprocess call. See comments in `tier_4_system_control.py`
  and `tier_5_advanced.py`.
- **Compat shim removal** — scheduled for next major version bump (the `DeprecationWarning` is active).
- **`test_oi_use_cases_*.py` import updates** — use_cases tests still use `extensions.oi_capability.*`
  (via conftest alias). Left as-is per spec to keep the diff smaller.
