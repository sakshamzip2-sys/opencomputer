# OpenClaw-Parity Port — Design Spec

**Date:** 2026-05-03
**Author:** Saksham + Claude
**Status:** Approved (brainstorm phase complete, plan-of-record in flight)
**Source analysis:** Pasted into session 2026-05-03 ("dev-2") — deep code dive comparing OpenComputer's openclaw-derived modules against the upstream openclaw reference at `sources/openclaw-2026.4.23/`.

## 1. Background

CLAUDE.md identifies four reference projects; openclaw contributed *plugin-first architecture, strict SDK boundary, manifest-first two-phase discovery, typed wire protocol*. A side-by-side audit revealed that OC's port is correct in shape but shallow in fidelity — the data carriers exist (`plugin.json`, `WireRequest`) but several openclaw guardrails do not (manifest size cap, host-version pinning, frozen-inventory boundary tests, typed error codes, secret-ref primitive, `inspect-shape` debugging surface).

This spec ports the load-bearing pieces in one PR.

## 2. Goals

1. Stop a v1.0 → v1.1 SDK change from silently breaking installed plugins (`min_host_version`).
2. Make the plugin-SDK boundary load-bearing for *every* file under `extensions/`, not just `plugin_sdk/` (extension boundary test).
3. Make plugin activation *manifest-readable* — a user can grep `plugin.json` and know when the plugin loads (`activation` block).
4. Stop API keys / OAuth tokens flowing through the wire as `dict[str, Any]` (`SecretRef` primitive).
5. Give plugin authors a one-command introspection that compares manifest claims to actual registrations (`opencomputer plugins inspect`).
6. Replace opaque error strings on the wire with a typed enum (`ErrorCode`).
7. Tolerate human-friendly plugin manifests (JSON5 with comments + trailing commas).
8. Defend discovery against pathological manifests (256KB cap).
9. Lift wizard / CLI auth metadata out of code into manifest (`providerAuthChoices`).

## 3. Non-goals

- Splitting `plugin_sdk/__init__.py` into narrow subpaths (`plugin_sdk.channel`, `.provider`, …). Requires migrating 71 plugins; deserves its own PR. Out of scope.
- Cleaning up the 27 existing extensions that import from `opencomputer.*`. The boundary test ships in advisory mode with a frozen-inventory snapshot; cleanup is a separate concern.
- Migrating existing `protocol_v2.py` `params: dict[str, Any]` methods to use `SecretRef`. New methods only — opportunistic adoption.
- Per-channel adapter `auth_choices` parity (only providers in scope; channels stay on existing `setup.channels.env_vars`).
- Full openclaw `inspect-shape.ts` capability classification (4 shapes). OC's first-cut keeps it to two: `valid` (manifest claims match registrations) and `drift` (claims diverge). Hybrid/legacy buckets defer.

## 4. Scope (9 items, one PR)

### 4.1 `min_host_version` field in manifest

**Why:** Without it, `opencomputer` 1.0 → 1.1 schema or contract changes break user-installed plugins silently. Openclaw rejects-with-message at discovery time.

**Where:** `plugin_sdk.core.PluginManifest`, `manifest_validator.PluginManifestSchema`, `discovery._parse_manifest`, `loader.load_plugin`.

**Shape:**
```python
# plugin_sdk/core.py
@dataclass(frozen=True, slots=True)
class PluginManifest:
    ...
    min_host_version: str = ""  # e.g. "1.0.0", "" = no check
```

**Behavior:** When non-empty, compare against `opencomputer.__version__` using `packaging.version.Version`. Raise `PluginIncompatibleError` at load time with a clear message. Empty string = no check (back-compat).

**Test:** `tests/test_min_host_version.py` covers (a) empty string skips check, (b) host version >= manifest version loads, (c) host version < manifest version raises with message containing both versions, (d) malformed version string fails validation.

### 4.2 Extension boundary test (frozen inventory)

**Why:** Today's `tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer` only checks `plugin_sdk/*.py`. The actually-load-bearing rule — *extensions can't deep-import core* — has no enforcement. 27 of 71 extensions already violate it.

**Where:** New `tests/test_plugin_extension_boundary.py` + `tests/fixtures/plugin_extension_import_boundary_inventory.json`.

**Shape:**
1. Walk `extensions/*/**/*.py`.
2. For each file, AST-parse; collect every `import opencomputer.X` and `from opencomputer.Y import Z`.
3. Compare against the inventory file. Inventory is `{relative_path: [imported_module, ...]}`.
4. **Pass** if every violation in the live scan is also in the inventory.
5. **Fail** if (a) a new violation appears that's not in the inventory or (b) the inventory has stale entries (extension removed but inventory didn't know).

**Inventory regeneration:** A pytest fixture / CLI helper (`scripts/refresh_extension_boundary_inventory.py`) regenerates the file. Initial seed = current 27 violators. Reviewers must approve any inventory change. New extensions land with empty inventory entries.

**Test:** Self-test in `test_plugin_extension_boundary.py`:
- Synthetic extension with `from opencomputer.X import Y` + matching inventory entry → pass.
- Same synthetic extension + missing inventory entry → fail with ext path + module name.
- Inventory entry for non-existent file → fail with stale-entry message.

### 4.3 `activation` block in manifest

**Why:** Sub-project E (demand-driven activation, PR #26) infers activation from `tool_names` only. Other triggers (channel use, command invocation, model selection) are scattered across code paths. Openclaw declares them in manifest; the planner reads manifest only.

**Where:** `plugin_sdk.core.PluginManifest.activation` + new `opencomputer/plugins/activation_planner.py`.

**Shape:**
```python
# plugin_sdk/core.py
@dataclass(frozen=True, slots=True)
class PluginActivation:
    on_providers: tuple[str, ...] = ()  # e.g. ("anthropic", "openai")
    on_channels: tuple[str, ...] = ()
    on_commands: tuple[str, ...] = ()
    on_tools: tuple[str, ...] = ()       # superset of legacy tool_names
    on_models: tuple[str, ...] = ()      # complements model_support
```

**Back-compat:** `activation` field optional. When absent, planner falls back to current Sub-project E logic (`tool_names`-based inference). When present, `activation.on_tools ∪ tool_names` is the effective tool trigger list.

**Activation planner API:**
```python
# opencomputer/plugins/activation_planner.py
def plan_activations(
    candidates: list[PluginCandidate],
    triggers: ActivationTriggers,  # active providers/channels/commands/tools/model
) -> list[str]:  # plugin ids to activate, in deterministic order
```

**Tests:**
- Plugin with `activation.on_providers=["anthropic"]` activates when anthropic provider in triggers.
- Same plugin, no `activation` block, `tool_names=["X"]` — falls back to legacy path.
- Two plugins both with `on_commands=["foo"]` → command collision warning.

### 4.4 `SecretRef` typed wire primitive

**Why:** Today `WireRequest.params: dict[str, Any]` carries auth tokens for some methods. Whenever the wire crosses a process boundary (TUI ↔ gateway, IDE ↔ ACP), we leak. Openclaw's `primitives.secretref.test.ts` proves they treat secrets as a typed primitive that never serializes the raw value.

**Where:** New `plugin_sdk/wire_primitives.py` + adoption in `protocol_v2.py` for new methods only.

**Shape:**
```python
# plugin_sdk/wire_primitives.py
@dataclass(frozen=True, slots=True)
class SecretRef:
    """Opaque reference to a secret. The wire transport NEVER serializes
    the value; only the ref id. Resolution happens in-process via
    SecretResolver registry."""

    ref_id: str  # uuid4 hex; no semantic meaning
    hint: str = ""  # e.g. "anthropic-api-key"; safe to log

    def model_dump(self) -> dict[str, str]:
        return {"$secret_ref": self.ref_id, "hint": self.hint}


class SecretResolver:
    """Per-process registry mapping ref_id → actual value. Out-of-band
    from the wire. Intentionally not pickled / not persisted."""
    ...
```

**Adoption:** New methods only. Don't refactor existing call sites — that's a separate hardening pass. Document the pattern in `plugin_sdk/CLAUDE.md` so future methods reach for it.

**Tests:**
- `SecretRef.model_dump()` does NOT contain the raw value.
- JSON-roundtrip preserves ref_id + hint, drops value.
- `SecretResolver.resolve(ref)` returns value; unknown ref returns None.
- Concurrent resolver use (two sessions, same ref id different values) — namespaced per-session.

### 4.5 `opencomputer plugins inspect <id>` + shape classifier

**Why:** Today if `plugin.json` claims `tool_names=["X"]` but `register()` registers tool Y, no surface tells the author. Openclaw's `inspect-shape.ts` is the highest-utility per-LOC item in the audit (~150 LOC for huge plugin-author UX win).

**Where:** New `opencomputer/plugins/inspect_shape.py` + Typer subcommand `cli_plugin.plugin_inspect`.

**Shape:**
```python
# opencomputer/plugins/inspect_shape.py
@dataclass(frozen=True)
class PluginShape:
    plugin_id: str
    declared_tools: tuple[str, ...]      # from manifest
    actual_tools: tuple[str, ...]        # from ToolRegistry post-load
    declared_channels: tuple[str, ...]
    actual_channels: tuple[str, ...]
    declared_providers: tuple[str, ...]
    actual_providers: tuple[str, ...]
    declared_hooks: tuple[str, ...]
    actual_hooks: tuple[str, ...]
    drift: tuple[str, ...]               # human-readable diff messages
    classification: Literal["valid", "drift"]


def inspect_shape(plugin_id: str) -> PluginShape: ...
```

**CLI surface:**
```
$ opencomputer plugins inspect anthropic-provider
Plugin: anthropic-provider (v0.1.0)
Manifest: extensions/anthropic-provider/plugin.json
Status: valid

Declared tools (manifest):
  (none)
Actual tools (registered):
  (none)

Declared providers (manifest):
  - anthropic
Actual providers (registered):
  - anthropic ✓
```

**Drift example:**
```
$ opencomputer plugins inspect example
Plugin: example (v0.0.1)
Status: drift

DRIFT:
  - tool 'X' declared but not registered
  - tool 'Y' registered but not declared
```

**Tests:**
- Bundled `anthropic-provider` inspects clean.
- Synthetic plugin with mismatched tool_names → drift.
- Plugin id not in registry → return `PluginShape` with `classification="drift"` and `drift=("plugin not loaded",)`. Never raise.
- Plugin whose `register()` raised at load time → return shape with the load error captured in `drift`.

### 4.6 Typed `ErrorCode` enum + `WireResponse.code` field

**Why:** `WireResponse.error: str | None` is opaque text. Wire clients (TUI, IDE bridges) can't program against errors. Openclaw declares an enum.

**Where:** New `opencomputer/gateway/error_codes.py` + extend `WireResponse` with `code: ErrorCode | None`.

**Shape:**
```python
# opencomputer/gateway/error_codes.py
from enum import StrEnum

class ErrorCode(StrEnum):
    PLUGIN_NOT_FOUND = "plugin_not_found"
    PLUGIN_INCOMPATIBLE = "plugin_incompatible"
    PROVIDER_AUTH_FAILED = "provider_auth_failed"
    TOOL_DENIED = "tool_denied"            # consent / policy block
    CONSENT_BLOCKED = "consent_blocked"
    METHOD_NOT_FOUND = "method_not_found"
    INVALID_PARAMS = "invalid_params"
    INTERNAL_ERROR = "internal_error"
    RATE_LIMITED = "rate_limited"
    SESSION_NOT_FOUND = "session_not_found"
```

**Back-compat:** `WireResponse.error: str | None` stays. New optional `code: str | None` mirrors enum value. Old clients ignore `code`; new clients can `match` on it.

**Tests:**
- `ErrorCode.PLUGIN_NOT_FOUND.value == "plugin_not_found"`.
- `WireResponse(ok=False, error="...", code=ErrorCode.PLUGIN_NOT_FOUND).model_dump()` round-trips.
- Existing wire callers passing only `error` still parse.

### 4.7 JSON5 tolerance for `plugin.json`

**Why:** Manifests are config files humans edit. Comments + trailing commas matter for authorability.

**Where:** `discovery._parse_manifest`.

**Shape:** Two-tier parse — try `json.loads` first (fast path for compliant manifests), fall back to `json5.loads` on `JSONDecodeError`. Zero overhead for valid JSON.

**Dep:** Add `json5>=0.9` to `pyproject.toml`. ~30KB pure-Python.

**Tests:**
- Plain JSON manifest still parses.
- Manifest with `// comment` parses.
- Manifest with trailing comma parses.
- Manifest that's neither valid JSON nor valid JSON5 → existing warning path.

### 4.8 256KB manifest size cap

**Why:** Trivially defends discovery against pathological / malicious plugins shipping a 100MB `plugin.json` that DOSes the scan loop.

**Where:** `discovery._parse_manifest` size guard.

**Shape:**
```python
MAX_MANIFEST_BYTES = 256 * 1024
size = manifest_path.stat().st_size
if size > MAX_MANIFEST_BYTES:
    logger.warning("manifest %s exceeds %d bytes — skipping", manifest_path, MAX_MANIFEST_BYTES)
    return None
```

**Tests:**
- 1KB manifest parses normally.
- 257KB manifest skipped with warning.
- 256KB-exact boundary parses.

### 4.9 `providerAuthChoices` rich auth UI metadata

**Why:** Wizard reads `setup.providers[].auth_methods: list[str]` — opaque method strings ("api_key", "bearer"). Per-method UI labels, group hints, CLI flags are hand-coded in the wizard. Openclaw declares them in manifest so wizard + CLI flags are derived.

**Where:** `plugin_sdk.core.SetupProvider.auth_choices` + `manifest_validator.SetupProviderSchema` + setup wizard read path.

**Shape:**
```python
# plugin_sdk/core.py
@dataclass(frozen=True, slots=True)
class AuthChoice:
    method: str            # "api_key" | "bearer" | "oauth_device" | ...
    label: str             # e.g. "Anthropic API key"
    cli_flag: str = ""     # e.g. "--anthropic-key"
    option_key: str = ""   # internal config key, e.g. "anthropic.api_key"
    group: str = ""        # for grouping multiple providers' auth in wizard
    onboarding_priority: int = 0


@dataclass(frozen=True, slots=True)
class SetupProvider:
    ...
    auth_choices: tuple[AuthChoice, ...] = ()  # parallel to / overrides auth_methods
```

**Back-compat:** `auth_choices` optional. When absent, wizard falls back to current `auth_methods: list[str]` interpretation. When present, it takes precedence and provides richer UI.

**Tests:**
- Manifest with `auth_choices` parses + reaches wizard.
- Manifest without `auth_choices` keeps existing wizard behavior.
- Empty `auth_choices` tuple — wizard still falls back to `auth_methods`.

## 5. Architecture diagram

```
                         plugin.json (v4)
                         ┌──────────────────────────┐
                         │ id, name, version, entry │
                         │ kind, author, license    │
                         │ + min_host_version       │ ◄── NEW (4.1)
                         │ + activation:            │ ◄── NEW (4.3)
                         │   on_providers: [...]    │
                         │   on_channels: [...]     │
                         │   on_commands: [...]     │
                         │   on_tools: [...]        │
                         │   on_models: [...]       │
                         │ + setup.providers[]:     │
                         │   auth_methods: [...]    │     existing
                         │   + auth_choices: [...]  │ ◄── NEW (4.9)
                         └────────────┬─────────────┘
                                      │
                            JSON5 parser (4.7)
                            256KB size gate (4.8)
                                      │
                          discovery._parse_manifest
                                      │
                                      ▼
                       PluginManifestSchema (validator)
                                      │
                                      ▼
                          loader.load_plugin
                                      │
                            min_host_version check (4.1)
                                      │
                              register(api)
                                      │
                                      ▼
            ┌──────────────────────────────────────────────┐
            │  ToolRegistry / ChannelRegistry / Providers   │
            └──────────────────────────────────────────────┘
                                      ▲
                                      │
                         inspect_shape.py (4.5) reads both
                         manifest declarations + actuals
                                      │
                       opencomputer plugins inspect <id>


            activation_planner.py (4.3)
                  reads manifest only
                  derives activation list


            wire (gateway/protocol_v2.py)
            ┌─────────────────────────────────┐
            │ WireRequest                      │
            │   params: dict[str, Any]         │
            │   + SecretRef in new methods (4.4)│
            │ WireResponse                     │
            │   error: str | None              │
            │   + code: ErrorCode | None (4.6) │
            └─────────────────────────────────┘
```

## 6. Sequencing

Single PR, ordered commits:

1. `feat(plugin-sdk): add min_host_version + activation + auth_choices fields` — types only, schema validator, no consumers yet
2. `feat(manifest): JSON5 tolerance + 256KB size cap in discovery` — surface protections
3. `feat(plugins): activation_planner module + back-compat with tool_names` — wire the activation field through
4. `feat(plugins): inspect_shape + opencomputer plugins inspect <id>` — debug surface
5. `feat(plugin-sdk): SecretRef wire primitive + SecretResolver` — types only
6. `feat(gateway): typed ErrorCode enum + WireResponse.code field` — wire opt-in
7. `feat(plugins): min_host_version enforcement at load time` — gate
8. `test: extension boundary test + frozen inventory of 27 current violators` — gate
9. `chore: update CLAUDE.md + extensions/AGENTS.md + plugin-author guide` — docs

Each commit is independently green. Schema bump (v3 → v4) happens in commit 1; old manifests (no v4 fields) still parse because every field is optional.

## 7. Tests

- ~10 new test files (one per new module / behavior).
- Existing 885 → ~970 tests after this PR.
- Coverage gate: each new module ≥ 80% line coverage.
- One integration test that runs `opencomputer plugins inspect anthropic-provider` end-to-end.
- The boundary inventory snapshot test is itself a regression gate — fails on stale OR new violations, with a one-line `python scripts/refresh_extension_boundary_inventory.py` fix instruction in the failure message.

## 8. Risks + mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| JSON5 parse perf on hot discovery loop | low | low | Two-tier parse: `json.loads` first, `json5` only on failure. |
| `ErrorCode` enum forces enum.value strings everywhere | medium | low | `StrEnum` so values are strings; legacy callers passing arbitrary strings still validate. |
| `activation_planner` regression in Sub-project E | low | high | New code path enabled only when `activation` block present. All existing tool-name-driven activation paths remain on legacy path. |
| Inventory file gets stale when extensions deleted | medium | low | Snapshot test catches removed entries (delta both ways). |
| Plugin authors don't know when to use `min_host_version` | medium | medium | Bundled plugins set it; documented in plugin-author guide; `plugin new` scaffolder generates with current version. |
| Boundary test inventory hides real cleanup work | high | medium | Document inventory as *legacy debt list*, not *permanent allowlist*. Each entry annotated with reason. Follow-up PRs delete entries one extension at a time. |
| `SecretRef` adoption is opt-in → leaks linger in old methods | high | medium | Acceptable for this PR (scope creep otherwise). Track separately. |

## 9. Open questions

None at spec time. All design decisions are committed; remaining choices (e.g., specific log format strings, error message text) are implementation-time concerns.

## 10. Future work (out of scope)

- Subpath split: `plugin_sdk.channel`, `.provider`, `.tool`, `.hook` (item #10 from audit). Plugin-author-visible refactor; deserves its own PR + migration guide.
- Migrate existing wire methods to `SecretRef`. Audit `params: dict[str, Any]` callsites, identify token-passing ones.
- Cleanup of 27 boundary violators (bigger surface — touches dispatch, registry, agent.steer, security.scope_lock, awareness, …). Per-extension PRs over time.
- Channel `auth_choices` parity (currently only providers). Symmetric extension once provider side proves out.
- `inspect-shape` 4-shape classification (`plain-capability`, `hybrid-capability`, `hook-only`, `non-capability`). First-cut keeps `valid` / `drift` only.

## 11. Acceptance criteria

- All 9 items shipped in one PR, atomic.
- 885 → ≥ 970 tests, all passing.
- Ruff clean.
- Boundary inventory snapshot passes.
- `opencomputer plugins inspect anthropic-provider` returns `valid` for at least 5 of 7 currently-bundled core plugins (anthropic-provider, openai-provider, telegram, discord, coding-harness).
- CHANGELOG updated.
- CLAUDE.md updated with new manifest fields.
