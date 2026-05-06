# OpenClaw Deep-Comparison Follow-Up — Design

**Date:** 2026-05-06
**Source brief:** `OpenComputer/docs/refs/openclaw/2026-05-06-deep-comparison.md` (TL;DR ranks S3→S2→S1→A3 over ~14 days).
**Reality check:** main `0009365d` is far ahead of the brief's reference point `5c62a12`. Three of the four picks are substantially shipped. This spec re-bases the plan on what is *actually* missing today, then ships the highest-leverage surgical PR first.

---

## 1. Reality vs. brief

| Brief pick | Brief claim | What's actually on main |
|---|---|---|
| **S3 — full hook taxonomy** | "OC has 6 events, OpenClaw has 16" | **20 events shipped** (`plugin_sdk/hooks.py`): PreCompact, BeforeCompaction, AfterCompaction, PreLLMCall, PostLLMCall, BeforePromptBuild, BeforeMessageWrite, TransformToolResult, TransformTerminalOutput, PreApprovalRequest, PostApprovalResponse, PreGatewayDispatch + 8 originals. |
| **S2 — plugin install + marketplace** | "completely missing — `cp -r`" | **Substantially shipped:** `remote_install.py` (catalog → tarball → sha256 verify → safe extract via `filter='data'`) + `catalog_signing.py` (ed25519) + `security.py` (uid + symlink + chmod gates) + 24h cache + `min_host_version`. |
| **S1 — inbound queue modes** | "1980s-style asyncio.Lock per chat" | **Genuinely missing.** `gateway/dispatch.py:355` confirms `dict[tuple[str, str], asyncio.Lock]` is the only mode. Photo-burst merging is one narrow exception. No queue-mode selector, no debounce, no drop policy. |
| **A3 — auth profiles rotation pool** | "no rotation pool, 429-pain" | **Substantially shipped:** `agent/credential_pool.py` with 4 strategies (fill_first, round_robin, random, least_used) + JWT refresh + 60s cooldown on 401 + 3600s on 429 + multi-key support across all 3 providers. |

### True remaining gaps (in priority order)

1. **S1 — Inbound queue modes (entirely missing).** The biggest user-facing ROI: any Telegram/Discord user sending two messages in quick succession hits this. Concurrency-model change → deserves its own dedicated PR.
2. **S2 leftovers.** Three small pieces complete the "trustworthy install" story:
   - `git+https://` / `git+ssh://` install source
   - Raw `https://...tar.gz` install source (bypassing catalog for one-off installs)
   - `install-security-scan.py` AST/pattern guard *before* activation
   - Integrity-drift detection (re-fetch + sha256 compare on demand)
3. **S3 leftover events.** Two events compose with S2's security scan:
   - `BEFORE_INSTALL` — security plugin can veto an install
   - Distinct `BEFORE_MODEL_RESOLVE` (PreLLMCall fires *post-resolve*, after the model is already chosen)
4. **A3 leftovers.** Smallest category:
   - `doctor` health checks for pool exhaustion / quarantine state
   - Per-agent rotation pool layer (current pool is per-provider)

---

## 2. Phasing — what ships when

### Phase 1 — "Trustworthy install completion" (this PR, today)

**Scope:** Combine S2 leftovers + S3's `BEFORE_INSTALL` event into one cohesive PR. The story is **"third-party plugin install paths are now trustworthy and pluggable."**

**Why first:**
- **Surgical.** Extends `remote_install.py` and `plugin_sdk/hooks.py`. Zero concurrency-model changes. Zero loop changes.
- **Cohesive.** The four pieces (git source · url source · AST scan · BEFORE_INSTALL hook) compose into a single user-facing story.
- **Completes a v1.0+ ecosystem story.** Without these, "install third-party plugins" still depends on either local `cp -r` or catalog-only installs. With them, plugin authors can publish via `git+https://github.com/x/y` *and* security-conscious operators can plug in their own scan policy.
- **Low risk.** Existing flow stays default; new sources are opt-in by URL scheme.
- **Shippable today** at ~700 LOC + ~250 LOC tests with full coverage.

**Out of scope (deferred):**
- npm install source (Python ecosystem uses pip, not npm — brief acknowledges this)
- `--unsafe` opt-out flag (defer until at least one user complains; security-by-default is the v1.x stance)
- Marketplace registry beyond what `remote_install.py` already does

### Phase 2 — Inbound queue modes (next dedicated PR)

**Scope:** S1 in full — 5 queue modes (steer/followup/collect/steer-backlog/interrupt) + debounce + drop policy + per-session config + `/queue-mode` slash command.

**Why deferred to its own PR:**
- Concurrency model change. Any regression here breaks every channel user.
- Needs its own brainstorm + design pass once Phase 1 is merged.
- Estimate: ~1,000 LOC + ~400 LOC tests + dedicated test matrix mirroring OpenClaw's `queue.collect.test.ts` etc.

### Phase 3 — Hook + auth leftovers (later, batched with other work)

**Scope:**
- `BEFORE_MODEL_RESOLVE` distinct event (PreLLMCall is post-resolve)
- `MESSAGE_SENDING` / `MESSAGE_SENT` — narrower than `PreGatewayDispatch`
- `oc doctor --auth` health checks for credential pool quarantine state
- (Maybe) per-agent rotation pool layer

**Why deferred:** Each piece is small (<150 LOC) and individually low-leverage. Better batched into an unrelated PR than a stand-alone one.

---

## 3. Phase 1 design — Trustworthy Install Completion

### 3.1 Components

```
opencomputer/plugins/
├── remote_install.py            # extended — git_url + raw_url install paths
├── install_security_scan.py     # NEW — AST + regex pattern guard
├── integrity.py                 # NEW — re-fetch + drift detection
└── loader.py                    # touch — fire BEFORE_INSTALL hook before extract

plugin_sdk/
├── hooks.py                     # extended — HookEvent.BEFORE_INSTALL
└── __init__.py                  # re-export (none needed; HookEvent is already exported)

opencomputer/
└── cli_plugin.py                # extended — accept git/url install args
```

### 3.2 New install source matrix

| Source | URL scheme example | Detection rule | Verification |
|---|---|---|---|
| catalog (existing) | `slug` (no scheme) | argument has no `://` | catalog sha256 + ed25519 sig |
| git (new) | `git+https://github.com/x/y.git` or `git+ssh://...` | scheme starts with `git+` | optional `--ref <sha>` pin (default tracks default branch); `git ls-remote` HEAD sha logged for audit |
| url (new) | `https://example.com/plugin-0.1.0.tgz` | scheme is `https://` | required `--sha256 <hash>` pin (no `--unsafe-no-checksum` flag in Phase 1) |

**Tarball format constraint (both catalog and url paths):** only `.tar.gz` / `.tgz` accepted. `.tar.bz2`, `.tar.zst`, `.zip`, etc. raise `UnsupportedTarballFormat`. This matches the existing `tarfile.open(..., mode="r:gz")` in `extract_tarball`.

**Plugin-id integrity (catalog, git, and url paths):** after extract, the loader reads the extracted `plugin.json` and confirms its `id` field matches the slug or repo-name the user typed. Mismatch raises `PluginIdMismatchError`. (Today, the catalog path enforces this by construction via `find_entry`; the new git/url paths need explicit checking because there's no catalog binding.)

**Rationale for per-source verification asymmetry:**
- Catalog: full chain (sha256 + ed25519 sig).
- git: ref pin is the verification (ref is content-addressable — sha is the hash). When `--ref` is omitted, we resolve the default branch's HEAD sha via `git ls-remote`, log it, and store it in the per-profile installed-plugin index so `oc plugin verify` knows the pin retroactively.
- url: explicit `--sha256` required by default. No `--unsafe-no-checksum` flag in Phase 1.

**Git binary detection:** `shutil.which("git")` (handles `git.exe` on Windows). Missing binary → clear error message: `"git binary not found on PATH — install Git or use catalog/url install instead."`

**Git private-repo authentication:** we shell out to `git clone`, so the user's existing SSH agent / `~/.git-credentials` / git credential helper handles auth. We don't implement custom git protocols.

**Git submodule policy:** `git clone --depth=1` (no `--recurse-submodules`). Submodules are a supply-chain attack surface — opt-in via a future `--with-submodules` flag if a user requests it. Phase 1 ships without that flag.

### 3.3 `install_security_scan.py` — AST + regex guard

Inspired by OpenClaw's `install-security-scan.ts` (109 LOC). Runs *after* extract, *before* `BEFORE_INSTALL` hook fires, *before* the loader imports anything.

**What it scans:**
- `subprocess.Popen("rm -rf ...")` and similar destructive shell patterns (regex over `*.py`)
- `os.system("...")` calls that look like exfiltration (regex)
- Eval/exec of network-fetched bytes (AST: `Call` whose func is `eval`/`exec`/`compile` and whose arg is a `requests.get(...)` chain)
- Known-bad import patterns: `import socket; ...connect((...,53))` style (regex)

**Output:** A `ScanReport` dataclass with `findings: list[Finding]`, each with `severity: "info"|"warn"|"block"`, `file`, `line`, `pattern`, `excerpt`.

**Default policy:** any `block` finding raises `InstallSecurityScanError`. `warn` findings are logged but install proceeds.

**Initial pattern severities (Phase 1):**
- `eval()`/`exec()`/`compile()` whose argument is a network-fetch chain → `block` (this is the unambiguous "remote-code-execution loader" anti-pattern; false-positive rate is essentially zero)
- `rm -rf` / `unlink` of the user's home → `warn` (could be legitimate cleanup; observable + reportable but not blocked)
- Suspicious socket usage → `warn`
- Unparseable `.py` → `warn` (soft-fail; the Python loader will catch real syntax errors at import)

A future tightening pass (post-dogfood) can promote individual `warn` patterns to `block`. The two-tier API exists from day one so the promotion is a one-line change.

### 3.4 `BEFORE_INSTALL` hook event

```python
class HookEvent(str, Enum):
    ...
    BEFORE_INSTALL = "BeforeInstall"
```

`HookContext` extension fields (all optional; existing callers unaffected):
- `install_source: str | None` — `"catalog" | "git" | "url" | "path"`
- `install_url: str | None` — the raw arg the user typed
- `install_plugin_id: str | None` — resolved plugin id
- `install_scan_report: object | None` — `ScanReport` from §3.3

Fires from `_install_from_remote` (and the equivalent for git/url paths) immediately after the security scan and immediately before extract-and-activate. A handler returning `decision="block"` aborts the install with the handler's `reason`.

### 3.5 Integrity drift — `integrity.py`

A standalone helper called by a new CLI subcommand `oc plugin verify <slug>`:

1. Resolve installed plugin's directory.
2. Read its `plugin.json` to get its claimed source (catalog slug + version, or git url + ref, or raw url + sha256).
3. Re-fetch the source bytes.
4. Compare bytes-for-bytes against the on-disk extracted tree.
5. Report mismatches.

**Out of scope for Phase 1:** auto-repair, scheduled drift checks. Just the manual `oc plugin verify` command.

### 3.6 CLI surface

```
oc plugin install <slug>                            # existing, unchanged
oc plugin install git+https://github.com/x/y.git    # NEW — git source
oc plugin install git+ssh://git@host/x/y.git --ref abc123   # NEW — git pinned
oc plugin install https://example.com/x-0.1.0.tgz --sha256 abc...   # NEW — url source
oc plugin verify <slug>                             # NEW — drift check
oc plugin uninstall <slug>                          # existing, unchanged
```

### 3.7 Tests

- `tests/test_remote_install_git.py` — git+https smoke (mock subprocess), git+ssh smoke, ref pin enforcement, missing-`--ref` warning, dirty-clone rejection.
- `tests/test_remote_install_url.py` — https tarball smoke, missing-`--sha256` rejection, mismatched-sha256 rejection, oversize tarball rejection.
- `tests/test_install_security_scan.py` — AST scan: catches `eval(requests.get(...))`, regex scan: catches `rm -rf`, clean plugin passes, scan report shape.
- `tests/test_install_hooks.py` — `BEFORE_INSTALL` fires once per source type, `decision="block"` aborts install, hook receives populated `install_scan_report`.
- `tests/test_integrity.py` — `oc plugin verify` happy path, drift detection (mutate one file, expect mismatch), missing-source-info graceful degrade.

Target: ≥ 90% line coverage on new code; full test suite stays green; ruff clean.

### 3.8 Backwards compat invariants

- Existing `oc plugin install <slug>` (catalog-only) flow is byte-identical.
- Existing `BEFORE_*` hook callers see no new mandatory fields (the new `install_*` fields default to `None`).
- Existing plugin manifests don't need to change.
- The 26 existing extension files frozen by `tests/test_plugin_extension_boundary.py` are untouched (new code lives under `opencomputer/plugins/`, not `extensions/`).
- Existing `~/.opencomputer/<profile>/plugins/` filesystem layout is unchanged.
- Existing `--profile X` / `--global` flag behaviour for installs is unchanged — git/url paths flow through the same `_install_dest` resolution as catalog paths today.

### 3.9 Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| AST scan false positives blocking legitimate plugins | Med | All new patterns start at `severity="warn"`. Promote to `"block"` only after dogfooding. |
| `git` install requires `git` binary on PATH | Low | Detect at runtime; fail with a clear message ("git binary not found — use catalog or url install instead"). |
| Tarball-from-url missing checksum is the obvious foot-gun | High | Refuse install without `--sha256` by default. No `--unsafe-no-checksum` flag in Phase 1. |
| `BEFORE_INSTALL` handler hangs on slow scan | Low | `HookSpec.timeout_ms` already exists and is fail-open. |
| Integrity-drift comparison breaks on file-mtime metadata | Low | Compare content bytes only, not stat metadata. |
| Re-fetch on `oc plugin verify` fails because source URL rotated | Med | Catch `httpx.HTTPError` / `subprocess.CalledProcessError` and emit `SourceUnreachable` with the recorded source url; don't crash. |
| Race: two `oc plugin install <slug>` calls in parallel | Low | Existing `dest.mkdir(exist_ok=False)` in `extract_tarball` already catches it; second caller gets `FileExistsError` → mapped to "plugin already installed, use --force". Add a regression test. |
| User on Windows without Git for Windows installed | Low | `shutil.which("git")` returns `None` → clear error message; catalog/url paths still work. |
| Plugin author's git repo has submodules that pull malicious code | Med | `--depth=1` (no `--recurse-submodules`) by default; opt-in flag deferred. Documented in §3.2. |
| AST scan can't parse a `.py` file (Python 3.14 syntax in a 3.12 host) | Low | Soft-fail with `severity="warn"` finding; loader will catch real errors at import. |

---

## 4. Phase 2 design (sketch only — full design deferred)

**Inbound queue modes** — replace the per-(profile, session) `asyncio.Lock` with a `QueueManager` per session that supports:
- 5 modes: `steer` (abort + restart with new context), `followup` (finish + reply), `collect` (buffer until done), `steer-backlog` (finish + treat rest as backlog), `interrupt` (preempt).
- Per-session configurable, with a per-channel default and a global default.
- Debounce window (configurable, default 1.5s).
- Drop policy on overflow (`old`/`new`/`summarize`) with cap default 50.
- `/queue-mode <mode>` slash command for runtime override.

**Surface area estimate:**
- `plugin_sdk/queue.py` — `QueueMode` literal + `QueueConfig` dataclass
- `gateway/queue_manager.py` — replacement for the dict-of-locks
- `gateway/dispatch.py` — refactor to call `QueueManager.dispatch(event)`
- `agent/slash_commands_impl/queue_mode.py` — `/queue-mode` command
- `bindings.yaml` — per-channel default
- Tests mirroring OpenClaw's `queue.collect.test.ts`, `queue.dedupe.test.ts`, `queue.drain-restart.test.ts`

**Why not bundled with Phase 1:** concurrency-model changes are blast-radius operations. They warrant their own design + audit cycle, not a "while we're here" addition.

---

## 5. Phase 3 sketch (later)

- `BEFORE_MODEL_RESOLVE` distinct event — fires inside `model_resolver.resolve_model()` so a plugin can swap the alias-target before resolution. Estimated 80 LOC.
- `MESSAGE_SENDING` / `MESSAGE_SENT` narrower than `PreGatewayDispatch` — fires per outgoing message, not per inbound dispatch. Estimated 100 LOC each.
- `oc doctor --auth` — surfaces credential-pool quarantine state, JWT expiry, last-rotation timestamps. Estimated 150 LOC + tests.

Bundle these with the next unrelated PR rather than ship as a stand-alone increment.

---

## 6. Decision log

| Decision | Rationale |
|---|---|
| Phase 1 ≠ S3 first (the brief's TL;DR top pick) | S3 is mostly already shipped; brief was written against stale main. |
| Phase 1 picks the cohesive subset, not the highest-LOC item | "Trustworthy install completion" is a single user-facing story; S1 is one too but with concurrency risk. |
| Defer S1 to its own PR | Concurrency-model changes deserve their own audit cycle; the user's workflow rule mandates phase-by-phase commits. |
| Drop npm install source | Python ecosystem; brief acknowledges this. |
| No `--unsafe-no-checksum` in Phase 1 | Security by default; reopen if a real user asks. |
| Integrity drift = manual `oc plugin verify` only | Auto-repair + scheduled drift are easy to add later; getting one working surface first is the priority. |
| AST scan patterns start at `warn`, not `block` | Avoid false-positive lockouts on legitimate plugins until dogfooded. |

---

## 7. Open questions (resolved internally; flagged for visibility)

- **Q: Should `git+https://` clones be shallow?**
  A: Yes — `--depth=1` by default; `--full-history` opt-in flag. Saves disk + bandwidth on big repos.
- **Q: What happens if the user passes `git+https://...` without `--ref`?**
  A: Clone the default branch's HEAD, log the resolved sha, store in `installed_plugin_index.json` so `oc plugin verify` knows the pin.
- **Q: Where does `installed_plugin_index.json` live?**
  A: `~/.opencomputer/<profile>/plugins/.installed_index.json` — per-profile, hidden, JSON.
- **Q: What if the AST scan can't parse a file (syntax error)?**
  A: Soft-fail with a `severity="warn"` finding. The Python loader will catch real syntax errors at import time.
- **Q: Should installs be written to the existing audit log (F1 consent layer)?**
  A: Yes-but-deferred. Phase 1 logs to standard `logger.info`; explicit audit-log integration is a follow-up so this PR doesn't grow.
- **Q: What if a `.tgz` file in the wild is actually `.tar.bz2` mis-named?**
  A: `tarfile.open(..., mode="r:gz")` raises `tarfile.ReadError`; we map to `UnsupportedTarballFormat`.

---

## 8. Acceptance criteria for Phase 1 PR

- All new tests pass; full suite (~9000+ tests) stays green.
- ruff check clean on all touched paths.
- `oc plugin install git+https://github.com/anthropics/example-plugin.git` works end-to-end (manual verification with a tiny test plugin repo).
- `oc plugin install https://example.com/x.tgz --sha256 ...` works end-to-end.
- `oc plugin install <plugin-with-eval-of-network-fetch>` is blocked by the AST scan with a useful error pointing at the offending file/line.
- A test plugin registering a `BEFORE_INSTALL` hook can veto an install regardless of scan result.
- A test plugin whose `plugin.json` `id` doesn't match the install argument is rejected with `PluginIdMismatchError`.
- `oc plugin verify <slug>` reports drift correctly when a file is mutated post-install.
- Existing `oc plugin install <slug>` (catalog mode) is byte-identical.
- README + CHANGELOG updated.
