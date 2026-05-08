# Hermes Config v2 — Foundation

**Date:** 2026-05-08
**Status:** Spec — implementation scope: 10 features in one PR
**Source:** `~/Downloads/hermes-configuration-v2.md` (full Hermes config reference)
**Worktree:** `~/.claude/worktrees/hermes-config-v2-2026-05-08`
**Branch:** `feat/hermes-config-v2-2026-05-08`
**Companion specs (non-overlapping):**
- Wave 3 provider-config: `2026-05-08-hermes-wave3-provider-config-design.md` (custom_providers, fallback chains, timeouts, MCP filters, mlx-whisper) — provider integrations
- CLI/TUI/Sessions v2: `2026-05-08-hermes-cli-tui-sessions-v2-parity-design.md` (input loop, slash, sessions, TUI polish)

This spec covers the ~/.opencomputer/ config-management layer that's the third leg of the v2 reference doc — orthogonal to the two specs above.

---

## 1. Problem

The Hermes config-v2 reference doc spans ~860 lines covering 28 surfaces. A gap-survey against `origin/main` (commit `abb3d9ce`) identified the following:

| Tier | Surface | OC status |
|---|---|---|
| Already shipped | `oc config` CLI (show/get/set/edit/variants/init) | `cli.py:3591` |
| Already shipped | DelegationConfig (max_concurrent_children, max_spawn_depth, etc.) | `agent/config.py:128` |
| Already shipped | WorktreeConfig + `.worktreeinclude` | `agent/config.py:627` |
| Already shipped | Display config + per-platform overrides | `gateway/display_config.py:24` |
| Already shipped | Sandbox backends (local/docker/ssh + apptainer stub) | `sandbox/*.py` |
| Already shipped | CheckpointsConfig | `agent/config.py:656` |
| Already shipped | Vision aux model | `agent/aux_llm.py:124` |
| Wave 3 covered | Custom provider list, fallback chain, per-provider timeouts, MCP filters | separate spec |
| **Missing — load-bearing** | `${VAR}` substitution in config.yaml | gap-fill |
| **Missing — load-bearing** | `oc config set` secret-routing → .env | gap-fill |
| **Missing — load-bearing** | `oc config check` | gap-fill |
| **Missing — load-bearing** | Top-level `timezone:` IANA | gap-fill |
| **Missing — load-bearing** | `auxiliary.compression.{provider, model, base_url, api_key, timeout}` | gap-fill |
| **Missing — load-bearing** | `privacy.redact_pii: bool` | gap-fill |
| **Missing — load-bearing** | `security.redact_secrets: bool` | gap-fill |
| **Cheap polish** | `agent.disabled_toolsets` | gap-fill |
| **Cheap polish** | `agent.api_max_retries` | gap-fill |
| **Cheap polish** | `sessions.vacuum_after_prune` | gap-fill |

**Out of scope this wave** (rationale in §6):
- `oc config migrate` interactive wizard (wait for `check` to validate the manifest scan)
- Aux slots beyond compression (web_extract, session_search, approval, triage_specifier, skills_hub, mcp) — defer until per-slot demand surfaces
- `approvals.mode: smart` — needs aux LLM gate; defer
- `command_allowlist`, `quick_commands`, `code_execution.mode`, `browser.dialog_policy`
- `credential_pool_strategies` per-provider override
- Modal/Daytona/Vercel/Singularity backends
- `human_delay`, `runtime_metadata_footer`, `group_sessions_per_user`, `unauthorized_dm_behavior`
- TTS/STT provider additions (Wave 3 ships mlx-whisper)
- Dashboard kanban toggle, AGENTS.md/CLAUDE.md/.cursorrules priority

**The makes-sense filter** (per memory rule): each shipping item has a concrete reliability/UX/privacy/cost benefit. Each parked item has a one-line reopen trigger.

---

## 2. Verified gap evidence (file:line)

| Item | Verified absent | Evidence |
|---|---|---|
| `${VAR}` substitution | YES | `config_store.py:236-266` — `load_config` uses `yaml.safe_load` directly; no env expansion pass |
| `oc config set` secret-routing | YES | `cli.py:3614` — `config set` writes everything to config.yaml; no `.env` heuristic |
| `oc config check` | YES | `cli.py:3591-3850` — has `show/get/set/edit/variants/init`; no `check` subcommand |
| Top-level `timezone:` | YES | `agent/config.py` — no `timezone` field; system prompt uses naive `datetime.now()` |
| `auxiliary.compression.*` slot | YES | `auxiliary_client.py:73-85` — flat `summary_model: str | None`; no `{provider, model, base_url, api_key, timeout}` shape |
| `privacy.redact_pii` | YES | no `privacy:` block in `Config` |
| `security.redact_secrets` | YES | no top-level `security.redact_secrets`; `tirith` exists separately |
| `agent.disabled_toolsets` | YES | no field on `LoopConfig` (or wherever toolset filtering lives) |
| `agent.api_max_retries` | YES | retries hardcoded; no config knob exposed |
| `sessions.vacuum_after_prune` | YES | `SessionConfig` has `auto_prune_days` but no `vacuum_after_prune` |

---

## 3. Design

### 3.1 `${VAR}` substitution in config.yaml loader

**Hermes contract** (verbatim from v2 doc):
- `${VAR}` syntax only — bare `$VAR` not expanded.
- Multiple references in one value work (`"${HOST}:${PORT}"`).
- Undefined vars kept verbatim (`${UNDEFINED_VAR}`).
- One-pass — no recursion (a value `${A}` resolving to `${B}` does NOT then resolve B).

**Implementation in `opencomputer/agent/config_store.py`:**

```python
import os
import re

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

def _expand_env_vars(value: Any) -> Any:
    """Recursively walk a dict/list, substituting ${VAR} in string values.

    - Single pass; no recursive expansion.
    - Undefined vars kept verbatim.
    - Only ${VAR} syntax; bare $VAR is not expanded.
    """
    if isinstance(value, str):
        def _sub(m: re.Match[str]) -> str:
            name = m.group(1)
            return os.environ.get(name, m.group(0))
        return _ENV_VAR_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    return value
```

Called once in `load_config` between `yaml.safe_load` and `_apply_overrides`.

**Tests** (`tests/test_config_env_substitution.py`):
- `${OPENAI_API_KEY}` substitutes from env.
- `${UNDEFINED}` kept verbatim.
- Multiple in one string: `"${HOST}:${PORT}"` → `"localhost:8080"`.
- Bare `$OPENAI_API_KEY` NOT expanded.
- Nested in lists / dicts.
- Non-string values (ints, bools) untouched.
- Recursion guard: `OPENAI_API_KEY=${OTHER_VAR}` resolves once (literal).

### 3.2 `oc config set` secret-routing

**Heuristic:** key (last segment after `.`) matches `_SECRET_KEY_PATTERN = re.compile(r"(?i)(api_key|token|secret|password|webhook_url)$")` → write to `~/.opencomputer/<profile>/.env`. Otherwise → `config.yaml`.

**Override flags:** `--secret` forces .env; `--public` forces config.yaml.

**User-facing message:** Always tell the user where the value landed:

```
$ oc config set OPENAI_API_KEY sk-abc...
✓ Wrote OPENAI_API_KEY to ~/.opencomputer/default/.env (secret pattern matched)

$ oc config set memory.provider honcho
✓ Wrote memory.provider to ~/.opencomputer/default/config.yaml
```

**`.env` writer:** parse existing .env (KEY=VAL lines), update or append the line, write atomically (tempfile + rename), `chmod 0600`.

**Tests** (`tests/test_config_set_routing.py`):
- `OPENAI_API_KEY` → .env.
- `GITHUB_TOKEN` → .env.
- `memory.provider` → config.yaml.
- `--secret memory.provider honcho` → .env (override).
- `--public OPENAI_API_KEY sk-...` → config.yaml (override; print warning).
- .env file mode is `0600` after write.
- Existing keys updated in place; new keys appended.

### 3.3 `oc config check`

**Behavior:** Walk:
1. The bundled `Config` dataclass tree — every nested field with a default.
2. Every installed plugin's manifest's `required_environment_variables` list (per memory: this exists in plugin_sdk).

For each expected key, check:
- Is it set in config.yaml or .env?
- Is the env var defined (for plugin manifests)?

Print missing items grouped by category:
```
$ oc config check
Bundled config (3 missing):
  ✗ timezone — IANA timezone (default: server-local)
  ✗ privacy.redact_pii — Hash phone/user/chat IDs in gateway
  ✗ security.redact_secrets — Strip API key patterns from tool output

Plugins (2 missing required env vars):
  ✗ TAVILY_API_KEY — required by tavily-search plugin
  ✗ GROQ_API_KEY — required by groq-stt plugin

Run `oc config check --fix` to interactively add missing config keys (env vars
must be set manually in your shell or via `oc config set <NAME> <value> --secret`).
```

**`--fix` flag:** auto-adds bundled-config keys with their dataclass defaults. Does NOT touch `.env` (env vars need user input). Equivalent to `oc config migrate` minus interactive confirmation per-key.

**Implementation in `opencomputer/cli.py`** (new subcommand under `config` group). Reuses `_to_yaml_dict` and atomic-write helpers.

**Tests** (`tests/test_config_check.py`):
- Fresh config with no overrides → reports all defaults as "missing" (defaults aren't in YAML, but they are in dataclass — clarify: only items that are nested-but-default-empty).
- Plugin env-var scan: install fixture plugin with `required_environment_variables: ["FAKE_KEY"]`; assert `oc config check` flags `FAKE_KEY` missing.
- `--fix` writes default values to config.yaml without overwriting user values.

**Note on the "what's actually missing" question:** Most dataclass defaults are non-empty (e.g., `max_iterations: int = 100`). `oc config check` flags keys that are explicitly *new* in this version but not yet in the user's saved config.yaml — using the `_default_keys() - _yaml_keys()` set difference, then filtering for "interesting" defaults (not just "key matches default").

### 3.4 Top-level `timezone:` IANA

**Schema:**
```yaml
timezone: "America/New_York"  # IANA name. Empty/null = server-local time.
```

**Validation at `load_config`:** if non-empty, `zoneinfo.ZoneInfo(value)`. On `ZoneInfoNotFoundError`, raise `RuntimeError` with clear message.

**Use sites:**
1. **System prompt time injection** (`opencomputer/agent/system_prompt.py` — wherever `datetime.now()` is injected): use `datetime.now(zoneinfo.ZoneInfo(cfg.timezone))` if set.
2. **Cron scheduling** (`opencomputer/cron/`): pass `tzinfo` to scheduler.
3. **Log timestamps** (`logs/errors.log`, `logs/gateway.log`): formatter respects timezone.

**Tests** (`tests/test_timezone_config.py`):
- Valid IANA name parses.
- Invalid name raises clear error at load.
- System prompt embeds tz-aware time.
- Empty string → server-local fallback (current behavior unchanged).

### 3.5 `auxiliary.compression.*` nested slot

**Backward-compat strategy:** keep flat `summary_model: str | None` working. Add nested `compression: AuxSlotConfig` parallel to it. Resolution order: flat field → nested field → DEFAULT_MODEL_BY_TASK.

**New dataclass (in `auxiliary_client.py`):**

```python
@dataclass(frozen=True, slots=True)
class AuxSlotConfig:
    """Per-slot auxiliary model configuration.

    Hermes-shape — provider/model/base_url/api_key/timeout. ``provider`` "auto"
    inherits the active main provider; "main" is an explicit alias for the
    same. Setting ``base_url`` takes precedence over ``provider``.
    """
    provider: str = "auto"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout: float = 120.0


@dataclass(frozen=True, slots=True)
class AuxiliaryConfig:
    summary_model: str | None = None       # legacy flat shape (kept)
    classify_model: str | None = None
    extract_model: str | None = None
    title_model: str | None = None
    temperature: float = 0.3
    # Hermes-v2 nested slots — when set, take precedence over the flat
    # model fields above.
    compression: AuxSlotConfig | None = None
```

**Resolver in `model_for(task)` and instantiator in `AuxiliaryClient.__init__`:**

When `compression` is set, use a `CompressionAuxClient` that constructs its own provider instance from `provider/base_url/api_key`. Otherwise fall back to the parent provider with the flat `summary_model` override.

**Hot-reload (deferred — see §6):** Hermes calls out hot-reload of `auxiliary.compression.*`. Not in this wave; document as follow-up.

**Tests** (`tests/test_aux_compression_slot.py`):
- Flat `summary_model: gpt-4o` continues to work (legacy).
- Nested `auxiliary.compression.{provider: openrouter, model: google/gemini-2.5-flash}` overrides flat.
- `provider: "main"` inherits active provider — same instance.
- `base_url` takes precedence — uses custom OpenAI client.
- `timeout` honored — custom httpx client.

### 3.6 `privacy.redact_pii: bool`

**Schema:**
```yaml
privacy:
  redact_pii: false   # default off (existing behavior preserved)
```

**Behavior:** Gateway-only. Before message text + sender ID enter LLM context, hash phone/user/chat IDs deterministically (HMAC-SHA256 with a per-installation salt at `~/.opencomputer/.pii_salt`).

**Scope:** WhatsApp, Signal, Telegram (per Hermes spec — Discord/Slack route IDs are already opaque and NOT covered).

**Implementation in `opencomputer/gateway/`:** add `pii.py` with `hash_user_id(raw: str) -> str` and `hash_chat_id(raw: str) -> str`. Hook into existing message-ingest path (single chokepoint per adapter).

**Routing/delivery still uses original IDs internally** — only the LLM-facing context sees hashed forms. Critical to test: send a hashed-ID message in, verify outgoing reply uses original.

**Salt rotation:** `~/.opencomputer/.pii_salt` is created on first use (32 bytes random). Backed up; never committed. Document in `docs/`.

**Tests** (`tests/test_privacy_redact_pii.py`):
- Off by default — IDs pass through unchanged.
- On — deterministic hash (same input → same output across calls).
- Salt missing → auto-generated.
- LLM context contains hashes; outbound message uses original ID.
- Non-supported adapter (Discord) — no transformation (pass-through).

### 3.7 `security.redact_secrets: bool`

**Schema:**
```yaml
security:
  redact_secrets: false   # default off
```

**Behavior:** Detect API-key patterns in tool output (only) and replace with `[REDACTED]`. Applies BEFORE the output enters conversation context AND before it lands in `~/.opencomputer/logs/`.

**Patterns** (curated regex set, opt-in):
- `sk-[A-Za-z0-9]{20,}` (OpenAI/Anthropic-style)
- `ghp_[A-Za-z0-9]{36}` / `github_pat_[A-Za-z0-9_]{82}` (GitHub PATs)
- `xox[bp]-[A-Za-z0-9-]{20,}` (Slack)
- `AKIA[0-9A-Z]{16}` (AWS access keys)
- generic `Bearer [A-Za-z0-9_-]{30,}` in Authorization headers

**Implementation in `opencomputer/agent/redactors.py`** (new file, ~80 LOC):

```python
def redact_secrets_in_text(text: str, patterns: tuple[re.Pattern[str], ...] = DEFAULT_PATTERNS) -> str:
    for p in patterns:
        text = p.sub("[REDACTED]", text)
    return text
```

Called from the tool-output sink (`opencomputer/agent/tool_executor.py`-ish layer — wherever results are normalized before LLM consumption). Off by default; gated by `cfg.security.redact_secrets`.

**Tests** (`tests/test_redact_secrets.py`):
- Off → no transformation.
- On → `sk-abc123...` → `[REDACTED]`.
- On → false-positive guard: `sk-` followed by short string (< 20 chars) NOT redacted.
- On → multiple secrets in one string handled.
- On → Bearer-token in `Authorization: Bearer ...` header redacted.

### 3.8 Cheap polish — three one-liners

**3.8.a `agent.disabled_toolsets: list[str]`**

```yaml
agent:
  disabled_toolsets:
    - memory
    - web
```

Add `disabled_toolsets: tuple[str, ...] = ()` to `LoopConfig`. Apply in tool-registry build (after per-platform tool config) — listed names dropped from registry.

**3.8.b `agent.api_max_retries: int`**

```yaml
agent:
  api_max_retries: 2   # default 2
```

Add field to `LoopConfig`. Plumb into existing retry layer (`opencomputer/providers/base_provider.py` or wherever retries live). `0` = fail-over to fallback chain on first transient error.

**3.8.c `sessions.vacuum_after_prune: bool`**

```yaml
sessions:
  vacuum_after_prune: true   # default true
```

Add field to `SessionConfig`. After `auto_prune` deletes rows, run `VACUUM` (SQLite) to reclaim disk. Existing prune sweep already runs nightly; one-line addition.

**Tests:**
- `test_disabled_toolsets.py` — disabling `memory` removes `memory_*` tools from registry.
- `test_api_max_retries.py` — `api_max_retries=0` triggers fallback on first 429.
- `test_sessions_vacuum.py` — VACUUM called after prune; assert SQLite file shrinks.

---

## 4. Architecture & file map

**Modified files:**
- `opencomputer/agent/config_store.py` — `_expand_env_vars` + call site in `load_config`
- `opencomputer/agent/config.py` — new dataclasses: `PrivacyConfig`, `SecurityConfig`; new fields on `Config`/`LoopConfig`/`SessionConfig`/`AuxiliaryConfig`; `timezone: str = ""` on `Config`
- `opencomputer/agent/auxiliary_client.py` — `AuxSlotConfig` dataclass; `compression: AuxSlotConfig | None` on `AuxiliaryConfig`; resolver branch
- `opencomputer/cli.py` — `config set` secret-routing + `--secret`/`--public`; `config check [--fix]`
- `opencomputer/agent/system_prompt.py` (or wherever time injection lives) — tz-aware
- `opencomputer/cron/scheduler.py` — pass `tzinfo`
- `opencomputer/gateway/<existing message-ingest>` — pii hook
- `opencomputer/agent/tool_executor.py` (or output sink) — redact-secrets hook

**New files:**
- `opencomputer/gateway/pii.py` — hash_user_id / hash_chat_id + salt management
- `opencomputer/agent/redactors.py` — secret-pattern regex set + redact function

**New tests:**
- `tests/test_config_env_substitution.py`
- `tests/test_config_set_routing.py`
- `tests/test_config_check.py`
- `tests/test_timezone_config.py`
- `tests/test_aux_compression_slot.py`
- `tests/test_privacy_redact_pii.py`
- `tests/test_redact_secrets.py`
- `tests/test_disabled_toolsets.py`
- `tests/test_api_max_retries.py`
- `tests/test_sessions_vacuum.py`

**Total: ~700 LOC + ~70 tests across one PR.**

---

## 5. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| `${VAR}` regex too greedy / matches non-vars | Low | Strict pattern `[A-Z_][A-Z0-9_]*`; tests cover edge cases |
| `${VAR}` escapes user data containing literal `${...}` | Low | Document: undefined kept verbatim; users wanting literal `${X}` must export `X=` empty (acknowledged limitation) |
| Secret-routing heuristic mis-classifies a non-secret as secret | Low | Fixed regex; user override via `--public` |
| Secret-routing mis-classifies a secret as non-secret | Medium | Conservative regex; whitelist documented; user can use `--secret` to force |
| `oc config check` produces noisy "missing" output for fields that match dataclass defaults | Medium | Filter: only report fields that are nested empty dataclasses (not scalar defaults) |
| `redact_pii` breaks gateway routing | Medium | Routing layer reads original IDs from envelope, NOT from LLM context; integration test validates round-trip |
| `redact_pii` salt loss = retroactive un-correlation | Low | Salt is at `~/.opencomputer/.pii_salt`; document backup |
| `redact_secrets` regex strips a legitimate string | Medium | Off by default; conservative patterns (require length); user-visible warning that "this is opt-in lossy" |
| `auxiliary.compression.*` slot points at unreachable endpoint | Medium | Validate at load (probe disabled — async); on first call, surface clear error and fall back to flat `summary_model:` if set, else error |
| Timezone with invalid IANA name | Low | Validate at `load_config` with clear error |
| `disabled_toolsets` removes a toolset another feature depends on | Low | Document; tools that require other tools (e.g. browser → screenshot) error at use, not at config load |
| `api_max_retries: 0` confuses users (no retry feels like instant failure) | Low | Default stays at 2; `0` documented as "fail-fast to fallback" |
| VACUUM blocks gateway during prune | Low | VACUUM runs in the same nightly thread the prune already runs in |
| Parallel-session conflict | Low | Worktree at `~/.claude/worktrees/hermes-config-v2-2026-05-08`; verified no other session on this branch |

---

## 6. Out of scope — explicit reopen triggers

| Item | Reopen on |
|---|---|
| `oc config migrate` interactive wizard | After `check` ships and 1+ user reports asking "how do I add the missing keys?" |
| Aux slots beyond compression (web_extract, session_search, approval, triage_specifier, skills_hub, mcp) | Per-slot demand: a user explicitly wants vision/extraction/approval-eval against a different model |
| `approvals.mode: smart` | User asks "can OC auto-approve safe commands?" |
| `command_allowlist` | After `approvals` adds the "always allow this pattern" UX path |
| `quick_commands` | A user asks for shell shortcuts in the gateway/CLI |
| `code_execution.mode` (project/strict) | A user reports a venv-vs-staging confusion |
| `browser.dialog_policy` | A user reports a hung browser session on a JS alert |
| `credential_pool_strategies` per-provider | A user uses 3+ keys for one provider and asks "how do I round-robin?" |
| Modal/Daytona/Vercel/Singularity backends | A user runs OC in cloud-sandbox CI |
| `human_delay` | A user runs anti-bot-detection scenarios |
| Group session isolation (`group_sessions_per_user`) | A user runs OC in a group chat with multiple addressed humans |
| Compression hot-reload | A user reports "I edited config.yaml but compression still uses the old model" |
| Aux full normalization (rename `summary_model`→`compression.model`) | After a deprecation cycle (next minor version) |
| Dashboard kanban toggle | A user wants to hide kanban tab |
| AGENTS.md/CLAUDE.md/.cursorrules priority | A user wants to load context from one of these in OC |

---

## 7. Validation gates

1. **Per-feature unit tests** — listed in §3 — must all pass.
2. **Full suite green** — `pytest tests/` (run before push).
3. **Ruff clean** — `ruff check .` (run before push).
4. **Backward-compat snapshot** — `tests/test_config_load_back_compat.py` loads a "minimal config.yaml" (today's typical user setup) and confirms no errors and equivalent semantics.
5. **Smoke test** — write a config.yaml using `${OPENAI_API_KEY}`, run `oc config show`, verify the literal string is shown but provider client uses substituted value.
6. **Integration: aux compression slot** — write `auxiliary.compression.provider: openrouter`, mock the provider, run a real compaction, verify the request uses the configured model.
7. **Integration: redact_pii** — gateway test fixture sends a message with phone number; verify LLM gets hash; verify outbound reply uses original.
8. **Honcho test-pollution flake** is known-pre-existing (`project_honcho_default_test_pollution_flake.md`). Not blocking GitHub CI.

---

## 8. Decision

Ship 10 features in one PR. Bundled because they share a single theme ("config management foundation") and are interdependent at the config-loader layer (`${VAR}` substitution + new dataclass fields + new YAML keys). Splitting them across PRs would mean repeated config_store/config.py touches with merge friction.

Net delta:
- ~700 LOC + ~70 tests in one PR.
- 1 day execution from a clean worktree.
- Zero new public APIs that aren't backward-compatible.
- Every shipping feature has a "reason to ship" backed by concrete UX/privacy/cost benefit.
- Every parked feature has a "reopen on" trigger documented in §6.

---

## 9. Spec self-review

- **Placeholder scan:** no TBD/TODO. Each row in §1 has explicit shipping/parked status.
- **Internal consistency:** §1 (gap) → §2 (verified evidence) → §3 (designs) → §4 (file map) → §5 (risks) — round-trips.
- **Scope check:** 10 features at ~70 LOC/feature average is a healthy single-PR size. Not 27 — explicitly parked items in §6.
- **Ambiguity check:** Each design block names the file path and the test file. Each risk has a mitigation.
- **API surface drift check:** every new field has a default that preserves old behavior. `summary_model` flat shape kept; nested `compression` parallel-not-replacing.
- **Composability:** `${VAR}` + `oc config set --secret` + `auxiliary.compression.api_key: ${OPENROUTER_KEY}` is the canonical "compose 3 features" path; tested end-to-end.
- **YAGNI re-check:** §6 lists 14 features parked with explicit reopen triggers. Each one has a "no demand signal yet" rationale.
- **Verification dependency:** none of the 10 features require an upstream API verification (they're all local config / gateway plumbing). Lower risk than Wave 3.

---

## 10. Brainstorm-phase 9-lens audit

| Lens | Finding | Resolution |
|---|---|---|
| Assumption-check | "Aux slot rename is non-breaking" | KEEP flat `summary_model` working; add nested `compression` parallel — no breaking rename |
| Architecture stress | `${VAR}` recursion edge case | Single-pass; documented; tested |
| Architecture stress | `oc config set` for dotted paths | Use existing key-path parser in `cli.py:3614` |
| Architecture stress | `redact_pii` must not affect routing | Routing reads original from envelope; LLM-context-only redaction; integration test asserts round-trip |
| Alternative dismissal | Jinja2 vs simple regex for `${VAR}` | Picked simple regex on merit (matches Hermes contract exactly; zero new deps) |
| Alternative dismissal | Aux slot full rename vs alias | Alias preserves backward compat; full rename deferred to next minor |
| Requirement gap | `oc config check` needs an "expected keys" manifest | Walk: bundled `Config` dataclass tree + plugin manifests' `required_environment_variables` |
| Composability claim | `${VAR}` + secret-routing + aux slot — do they compose? | YES; `auxiliary.compression.api_key: ${OPENROUTER_KEY}` is the canonical compose path; covered by §7.5 smoke test |
| Scope honesty | "Cheap polish" 3 one-liners | Each is genuinely ~10-30 LOC; no hidden complexity |
| API surface drift | New nested `auxiliary.compression` block | Old configs with flat `summary_model` work unchanged; YAML schema is additive |
| Failure mode map | `redact_pii` salt loss | Documented in §3.6; salt at `~/.opencomputer/.pii_salt`; backup recommended |
| Failure mode map | `${VAR}` undefined → leaks `${X}` literal into LLM context | Acknowledged risk; mitigation: `oc config check` flags YAML values containing `${` whose env var isn't set |
| YAGNI sweep | `oc config migrate` interactive | DROPPED from this wave; deferred to §6 |
| YAGNI sweep | `redact_pii` allow/deny lists | DROPPED; just per-platform on/off |
| YAGNI sweep | Aux compression hot-reload | DROPPED; documented as follow-up |
| YAGNI sweep | Aux slots beyond compression | DROPPED; deferred per-slot |

All findings resolved or accepted as documented risk.

---

## 11. Reference implementation handoff

This spec is the input to the **plan** doc at `docs/superpowers/plans/2026-05-08-hermes-config-v2-foundation-plan.md`. The plan converts each design block into a numbered, checkbox-tracked task list executable by `superpowers:executing-plans` or `superpowers:subagent-driven-development`.
