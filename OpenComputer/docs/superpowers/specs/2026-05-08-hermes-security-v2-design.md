# Hermes Security v2 — OpenComputer Parity Design Spec

**Date:** 2026-05-08
**Source reference:** `~/Downloads/hermes-security-v2.md` (Hermes "Security — Full Reference" doc)
**Goal:** Bring OpenComputer to *capability* parity with the Hermes 7-layer security model, mapped into OC's idioms (consent layer, sandbox strategies, SDK boundary). NOT a literal API clone — `/yolo` stays deprecated; capabilities land in OC-shaped interfaces.

---

## 1. Audit — Hermes spec vs OpenComputer current state

| Hermes layer | Hermes ref | OC equivalent (existing) | Gap |
|---|---|---|---|
| 1. User authorization | per-platform allowlists, DM pairing | `channels/allowlist.py` (env+file+pairing), `channels/pairing_codes.py` (8-char, OWASP/NIST) | none — already shipped (PRs #488 / messaging-gateway-parity) |
| 2. Dangerous command approval | `tools/approval.py` `manual\|smart\|off`, `/yolo`, hardline | `tools/bash_safety.py` (heuristic only); `agent/consent/gate.py` (consent tiers); `--auto` replaces `/yolo` | **HARDLINE BLOCKLIST missing** as a non-bypassable gate |
| 3. Container isolation | hardened Docker flags | `sandbox/docker.py` — has `--memory --cpus --network none` | **Hardening flags missing** (`--cap-drop`, `--security-opt`, `--pids-limit`, tmpfs trio) |
| 4. MCP credential filtering | strip env to PATH/HOME/USER/LANG/LC_ALL/TERM/SHELL/TMPDIR/XDG_*, redaction | `mcp/client.py` (line 595 — `scope_subprocess_env`), `security/redact.py` | **AUDIT NEEDED** — confirm strict whitelist + redaction in error messages |
| 5. Context file scanning | prompt-injection detector | `security/instruction_detector.py`, `security/sanitize.py` | none — already shipped |
| 6. Cross-session isolation | profile dirs, cron paths | `path_safety.py`, per-profile dirs (Phase 14.A), pairing flock | none — already shipped |
| 7. Input sanitization | working-dir allowlist | `security/sanitize.py`, `security/scope_lock.py` | none — already shipped |

### Adjacent hardening (called out separately in Hermes doc)

| Item | Hermes | OC current | Gap |
|---|---|---|---|
| SSRF | URL validator, RFC1918/127/169.254/100.64/cloud-meta blocked | `security/url_safety.py` — wired into `WebFetch`, `WebSearch` | none |
| Tirith pre-exec scan | auto-install + cosign + `tirith_fail_open` | `security/tirith.py` | none |
| Website blocklist | wildcard domains, shared files, 30s cache | (none) | **MISSING — net new** |
| Approvals timeout | fail-closed on expiry | consent gate has 300s default | none |
| `HERMES_ALLOW_ROOT_GATEWAY` | refuse to start as root unless overridden | (none — verified by grep) | **MISSING — net new** |
| Production checklist | `~/.hermes/.env chmod 600`, allowlist hygiene, etc. | (no equivalent doc) | **MISSING doc** |

---

## 2. Scope (in)

The minimum work to close every honest gap above:

1. **Hardline blocklist module** — `opencomputer/security/hardline.py`. Non-bypassable. Runs at tool entry **before** consent gate / approvals / `--auto`. Container path also runs hardline (defence-in-depth: bind-mounts can leak destruction back to host).
2. **Hardline integration** in `BashTool.execute()` and `ExecuteCode.execute()` (and any future shell-out tool). One-line check, returns `is_error=True` with explanatory message.
3. **Docker security hardening flags** in `sandbox/docker.py` — exact flag list mirrors Hermes (cap-drop ALL + cap-add DAC_OVERRIDE/CHOWN/FOWNER + no-new-privileges + pids-limit + tmpfs trio).
4. **Website blocklist module** — `opencomputer/security/website_blocklist.py`. Domain matcher with `exact`, `*.subdomain`, `*.tld` rules. Reads `security.website_blocklist.{enabled,domains,shared_files}` from config. Cached 30s. Integrated into `WebFetch`, `WebSearch`, and the existing `is_safe_url` chain so it composes cleanly.
5. **Root-gateway check** — `gateway/server.py` startup refuses `os.geteuid() == 0` unless `OPENCOMPUTER_ALLOW_ROOT_GATEWAY=1`. POSIX-only (skipped on Windows).
6. **MCP env-filter audit** — verify `mcp/client.py` actually strips secrets to the Hermes whitelist; add explicit test if not. Also verify `security/redact.py` runs over MCP error messages before they hit the LLM.
7. **Production checklist doc** — `OpenComputer/docs/security-production.md`. Adapted from Hermes checklist with OC paths (`~/.opencomputer/<profile>/.env`, `oc gateway`, sandbox strategies, `OPENCOMPUTER_ALLOW_ROOT_GATEWAY`).
8. **Tests** — new test files for each new module; coverage adds to `tests/`.

## 3. Scope (out — honest deferrals)

| Item | Why deferred |
|---|---|
| `approvals.mode: manual\|smart\|off` config knob | Parallel branch `feat/hermes-config-v2-2026-05-08` is currently shipping `security.*` config keys (`security.redact_secrets`, `privacy.redact_pii`); adding `security.approvals.*` here invites merge conflict. The capability already exists via `agent/consent/gate.py` with `--auto` bypass. Re-surface as a thin config alias in a follow-up PR after that branch lands. |
| Smart-mode auxiliary LLM risk assessor | Substantive new dependency on a "judge" LLM call inside the dispatch loop. Not in any user-visible request. Defer until a real demand signal. |
| Skill-scoped `required_environment_variables` / `required_credential_files` frontmatter | Touches the skill loader + frontmatter parser + 2 sandbox strategies. Cleanly factorable into its own PR; not a security regression to ship without it (current skills don't use the keys). Open follow-up: phase-14F-credential-isolation. |
| Per-platform `unauthorized_dm_behavior: pair\|ignore` | Already covered by AllowlistGate's deny-with-pairing-code path; adding the platform override knob is YAGNI without a real platform-conflict report. |
| Approval flow for headless cron | Hermes maps to `HERMES_EXEC_ASK=1`; OC's cron path uses `--auto` semantics. Equivalent capability, no new code. |
| `tee /etc/`, `xargs rm`, `find -exec rm` heuristics | These are *approval-trigger* patterns in Hermes (warn-and-prompt), not hardline. Adding them here would require the deferred `approvals.mode` knob. They sit in `tools/bash_safety.py` already as advisory plan-mode signals. |

## 4. Design

### 4.1 Module: `security/hardline.py`

Single source of truth for the never-bypassable blocklist. Pattern shape mirrors `bash_safety.py` for consistency, but the semantic contract is different:

- `bash_safety.detect_destructive` is **advisory** (used by plan-mode hook).
- `hardline.check_command` is **enforcement** (returns a refusal, called from tools).

```python
# opencomputer/security/hardline.py
@dataclass(frozen=True, slots=True)
class HardlinePattern:
    pattern_id: str
    pattern: re.Pattern[str]
    reason: str

HARDLINE_PATTERNS: list[HardlinePattern] = [
    # rm -rf / and explicit no-preserve-root variant
    # bash fork bomb (re-uses bash_safety regex, lifted)
    # mkfs / mkfs.ext4 / mkfs.xfs against root device
    # dd if=/dev/zero of=/dev/sd*
    # curl URL | sh / wget URL | sh at top level
]

def check_command(cmd: str) -> HardlinePattern | None:
    """Return matching pattern (refusal trigger) or None."""
```

Patterns are tighter than `bash_safety.detect_destructive`. Where they overlap (fork bomb, rm-rf-/), hardline RE-USES the same regex by importing — DRY. The hardline check fires FIRST; if it fires, no other gate runs.

### 4.2 Tool integration (T2)

Both `BashTool.execute()` and `ExecuteCode.execute()` (and any other shell-out tool) call:

```python
from opencomputer.security.hardline import check_command

hit = check_command(cmd)
if hit is not None:
    return ToolResult(
        tool_call_id=call.id,
        content=f"Refused: {hit.reason} (hardline pattern '{hit.pattern_id}'). "
                f"This pattern is non-bypassable and cannot be approved.",
        is_error=True,
    )
```

The check fires **before** the existing profile-scoping / consent-gate logic. Order rationale: a hardline match should never even produce a consent prompt.

### 4.3 Docker hardening (T3)

`sandbox/docker.py:_wrap()` adds these constants to the argv (mirrors Hermes spec exactly):

```python
_SECURITY_ARGS = [
    "--cap-drop", "ALL",
    "--cap-add", "DAC_OVERRIDE",
    "--cap-add", "CHOWN",
    "--cap-add", "FOWNER",
    "--security-opt", "no-new-privileges",
    "--pids-limit", "256",
    "--tmpfs", "/tmp:rw,nosuid,size=512m",
    "--tmpfs", "/var/tmp:rw,noexec,nosuid,size=256m",
    "--tmpfs", "/run:rw,noexec,nosuid,size=64m",
]
```

Spliced into `_wrap` after `--cpus`. No config knob — Hermes doesn't have one and these are always-safe defaults; an opt-out hatch invites footguns. Test asserts argv contains every flag.

### 4.4 Website blocklist (T4)

```python
# opencomputer/security/website_blocklist.py
@dataclass(frozen=True, slots=True)
class WebsiteBlocklistPolicy:
    enabled: bool
    domains: tuple[str, ...]   # exact / *.subdomain / *.tld rules
    shared_files: tuple[Path, ...]  # extra rule files

def is_blocked(url: str, policy: WebsiteBlocklistPolicy) -> bool:
    """Return True if url's host matches any rule."""

def load_policy_cached(config: SecurityConfig) -> WebsiteBlocklistPolicy:
    """30-second cache (mirrors Hermes spec)."""
```

Match logic:
- Exact: `host == "admin.example.com"`
- Subdomain wildcard: `host.endswith(".internal.company.com")` if rule is `*.internal.company.com`
- TLD wildcard: `host.endswith(".local")` if rule is `*.local`
- Shared file: parse one rule per line, `#` for comments; missing file logs warning + skipped (mirrors Hermes "Missing/unreadable files log a warning but don't disable other web tools").

Wired into `WebFetch.execute()` and `WebSearch.execute()` AFTER `is_safe_url` (SSRF first, then policy):

```python
if not is_safe_url(url):
    return ToolResult(...is_error=True)
if is_blocked(url, load_policy_cached(...)):
    return ToolResult(...is_error=True, content=f"Refused: {url} matches website blocklist policy")
```

### 4.5 Root-gateway check (T5)

`gateway/server.py` startup adds:

```python
import os, sys
if hasattr(os, "geteuid") and os.geteuid() == 0:
    if os.environ.get("OPENCOMPUTER_ALLOW_ROOT_GATEWAY") != "1":
        sys.stderr.write(
            "Refusing to start gateway as root. Run as a non-root user, "
            "or set OPENCOMPUTER_ALLOW_ROOT_GATEWAY=1 to override.\n"
        )
        sys.exit(2)
```

The check sits at the entry point of `gateway run`, before any channel adapter loads. Skipped on Windows (`hasattr` guard).

### 4.6 MCP env-filter audit (T6)

Two-part:

(a) Read `opencomputer/mcp/client.py` lines around 595-620 (the `spawn_env` setup). Confirm it actually filters to the Hermes whitelist. If not, tighten.

(b) Add a regression test asserting the spawned MCP subprocess receives ONLY: `PATH`, `HOME`, `USER`, `LANG`, `LC_ALL`, `TERM`, `SHELL`, `TMPDIR`, plus any `XDG_*` keys + per-server `env:` declared.

### 4.7 Production checklist doc (T7)

`OpenComputer/docs/security-production.md` — direct port of Hermes section translated to OC paths. Items:

- chmod 600 on `~/.opencomputer/<profile>/.env`
- allowlist hygiene (`GATEWAY_ALLOW_ALL_USERS=true` only with explicit acceptance)
- pick a sandbox strategy (`docker` recommended for production gateways)
- DM pairing over hardcoded user IDs
- audit `command_allowlist:` entries periodically
- `OPENCOMPUTER_ALLOW_ROOT_GATEWAY` only if explicit
- monitor `~/.opencomputer/<profile>/logs/`
- `oc update` regularly
- `tirith_fail_open: false` for high-security envs

## 5. Files affected

**Add (new):**
- `opencomputer/security/hardline.py`
- `opencomputer/security/website_blocklist.py`
- `tests/test_hardline_blocklist.py`
- `tests/test_website_blocklist.py`
- `tests/test_docker_hardening.py`
- `tests/test_root_gateway_check.py`
- `tests/test_mcp_env_filter.py` (or extend existing)
- `OpenComputer/docs/security-production.md`

**Modify:**
- `opencomputer/security/__init__.py` — re-export new modules
- `opencomputer/sandbox/docker.py` — add `_SECURITY_ARGS` splice
- `opencomputer/tools/bash.py` — hardline check at entry
- `opencomputer/tools/execute_code.py` — hardline check before run_ptc
- `opencomputer/tools/web_fetch.py` — website-blocklist check after `is_safe_url`
- `opencomputer/tools/web_search.py` — website-blocklist check on result list
- `opencomputer/gateway/server.py` — root-uid check on entry

**Total estimate:** ~700 LOC (modules + tests + doc).

## 6. Risk register

| Risk | Mitigation |
|---|---|
| Merge conflict with parallel `feat/hermes-config-v2-2026-05-08` branch over `security.*` config schema | Use a fully-qualified `security.website_blocklist.*` namespace; no overlap with `security.redact_secrets` (a sibling key). Rebase if branch lands first. |
| Hardline regex misses obfuscated variant (e.g., `${PATH}`-built `rm -rf /`) | Documented limitation; this is defense-in-depth against accidents, not a determined adversary. Consent gate + sandbox provide the second wall. |
| `--security-opt no-new-privileges` unsupported on very old Docker | Docker 1.11+ (May 2016) — predates our Python 3.12+ floor. Not realistic. |
| `--pids-limit 256` breaks legitimate multi-process workloads in container | Hermes uses 256 too — battle-tested. Containers shouldn't be running 256+ procs; if one is, that's a code-smell. |
| Test for root-gateway depends on `os.geteuid()` (not on Windows) | Test marked `pytest.skipif(sys.platform == "win32")`. |
| Website blocklist false-positive on legitimate company domain | User-controlled list; if they added the wrong rule that's a config error, not a bug. Include doc warning in production checklist. |

## 7. Honest sizing

- T1 hardline module + tests: 90 min
- T2 wire into 2 tools + tests: 60 min
- T3 Docker flags + tests: 30 min
- T4 website blocklist + tests + 2 integrations: 120 min
- T5 root-gateway check + test: 30 min
- T6 MCP audit + test: 60 min
- T7 production checklist doc: 45 min
- T8 ruff + full pytest + commit + PR: 60 min
- **Total: ~7.5 hours** (one session at max effort with parallel tool dispatch).

## 8. Out-of-scope follow-ups (track in issues, not this PR)

- `approvals.mode` config knob — depends on parallel branch landing
- Smart-mode auxiliary LLM risk assessor — deferred without demand signal
- Skill-scoped env-passthrough frontmatter — phase-14F item
- `unauthorized_dm_behavior` per-platform override — YAGNI
