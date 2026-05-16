# OC sandbox subsystem — baseline survey (Milestone 1 recon)

Date: 2026-05-16 · Task T1.1 · Feeds T1.2–T1.5, T1.7, T1.8.
Companion plan: `docs/superpowers/specs/2026-05-16-oc-parity-with-hermes-openclaw/PART-2-plan-and-plan-audit.md`.

Read end-to-end in worktree `feat+oc-sandbox-scope-loop-detection-2026-05-16`. This file
replaces an earlier stub of the same name (the stub predated `policy.py` landing on disk).

---

## 1. Sandbox backends — all REAL implementations, no stubs

Every backend in `opencomputer/sandbox/` is a working implementation: spawns a real
subprocess via `asyncio.create_subprocess_exec`, enforces a wall-clock cap via
`asyncio.wait_for`, returns a populated `SandboxResult`. None are stubs or TODOs.

| Backend | File | What it actually does |
|---|---|---|
| `MacOSSandboxExecStrategy` | `macos.py` | Builds a per-call TinyScheme profile (`macos.py:85` `_build_profile`), wraps argv in `sandbox-exec -p <profile>`. deny-default + global `file-read*` + writes restricted to tmp dir + `write_paths`. No memory cap (documented; macOS `sandbox-exec` has no rlimit). |
| `LinuxBwrapStrategy` | `linux.py` | Wraps argv in `bwrap` — `--ro-bind` system roots, `--unshare-pid/ipc/uts`, `--unshare-net` when network denied, `--die-with-parent`. Memory cap via `prlimit --as=` when present (`linux.py:91`). |
| `DockerStrategy` | `docker.py` | `docker run --rm -i` with Hermes-parity hardening (`docker.py:50` `_SECURITY_ARGS`: `--cap-drop ALL` + 3 re-adds, `no-new-privileges`, `pids-limit`, tmpfs trio). `--memory`, `--cpus`, `--network none`, `-v` binds, profile-credential `:ro` mounts. Two-step kill on timeout. |
| `SSHSandboxStrategy` | `ssh.py` | Runs argv on a remote host over `ssh -o BatchMode=yes`. Regex-validated host (`ssh.py:51`), `shlex.join` argv. **Explicitly NOT a containment sandbox** — isolation-by-separation only (documented). |
| `NoneSandboxStrategy` | `none_strategy.py` | Runs argv directly, no containment. Logs WARNING per call. Intentional opt-out for tests/CI. |

**Shared interface** — `plugin_sdk/sandbox.py::SandboxBackend` (ABC). Abstract members:
`is_available() -> bool`, `async run(argv, *, config, stdin, cwd) -> SandboxResult`,
`explain(argv, *, config) -> list[str]`; plus `name: ClassVar[str]`. `SandboxStrategy` is a
backward-compat alias for `SandboxBackend` (`sandbox.py:179`) — all 5 classes still
`class XStrategy(SandboxStrategy)`. `_common.py` holds shared helpers (`filtered_env`,
`decode_stream`, `TIMEOUT_STDERR`/`TIMEOUT_EXIT_CODE`).

`auto.py::auto_strategy()` picks Darwin→macOS, Linux→bwrap, then Docker as universal
fallback; raises `SandboxUnavailable` if none. Its `config` arg is accepted but unused.

**Gating verdict (PART-1 flagged T1.1 as gating on stub-vs-real): NOT stubs.** M1 builds
*on top of* a functional layer; it does not rebuild it. T1.1's stub-risk is retired.

## 2. `runner.py` — responsibilities + the cache-key situation

`opencomputer/sandbox/runner.py` (91 lines) exposes one public coroutine
`run_sandboxed(argv, *, config, stdin, cwd)`:
- `config.strategy == "auto"` → `auto_strategy(cfg)`; else `_named_strategy(name)` (`runner.py:36`)
  maps the `SandboxStrategyName` literal to a concrete class and checks `is_available()`,
  raising `SandboxUnavailable` otherwise.
- Then calls `strategy.run(...)` and returns the result.

**There is NO container/process cache-key in `runner.py` today.** Each call resolves a
strategy and runs one transient invocation. The Docker backend mints a fresh random
container name **per call** at `docker.py:205` (`f"oc-sandbox-{uuid.uuid4().hex[:12]}"`) —
the only "key"-like construct, and it deliberately guarantees *no* reuse. No `lru_cache`, no
container pool, no process cache anywhere in `opencomputer/sandbox/`.

T1.3 implication: there is no existing cache key to "add scope to" — T1.3 must **introduce**
the keyed-resolution path. The intended key already exists as `scope_key()` in `policy.py`
(see §4); the Docker per-call uuid at `docker.py:205` is what a scope-aware path overrides.

## 3. `cli_sandbox.py` — ALREADY EXISTS (plan says "new file" — wrong)

`opencomputer/cli_sandbox.py` (4,546 bytes) is a live Typer subapp `sandbox_app`
(`name="sandbox"`, `no_args_is_help=True`), registered into the root CLI at
`opencomputer/cli.py:4889` via `app.add_typer(sandbox_app, name="sandbox")`. Three commands:
- `sandbox status` — table of available strategies + what `auto` picks.
- `sandbox run -- <argv>` — runs argv through `run_sandboxed`, exits with the wrapped code.
- `sandbox explain -- <argv>` — prints the wrapped command without running it (argv dry-run).

`oc sandbox` is already a real command group. T1.4 must **extend this file**, not create it.
Note the name overlap: a `sandbox explain` already exists (argv dry-run); T1.4's `explain`
is a *policy* inspector — recommend making `explain` dual-mode (bare = policy explain;
`-- <argv>` = keep the existing wrapped-command behavior).

## 4. `policy.py` — ALREADY EXISTS as untracked WIP (plan + this task brief both wrong)

`opencomputer/sandbox/policy.py` **exists** (untracked, `git status` `??`, 195 lines). Its
docstring cites this exact M1 spec dir. It already contains everything T1.2 plans to "design":
- `SandboxScope(str, Enum)` — values `none` / `tool` / `session` / `agent` / `shared`
  (`policy.py:41`). Note **5** values — plan's T1.2 lists only 4 (`none`/`agent`/`session`/`tool`);
  `shared` is an extra (matches OpenClaw).
- `SandboxPolicy` — `@dataclass(frozen=True, slots=True)` with `scope`, `tools_allow`,
  `tools_deny`; `.enabled` property, `.tool_allowed(name)` (deny-beats-allow, non-empty
  allow blocks the rest), `from_mapping()` / `to_mapping()` config round-trip (`policy.py:72`).
- `SandboxScopeContext` (`session_id`, `agent_id`) + `scope_key(policy, ctx) -> str`
  (`policy.py:166`) — Docker-name-safe container key (≤20 chars). `none`/`tool` → fresh uuid
  (no sharing); `shared` → constant `"shared"`; `session`/`agent` → `sha256` of the id.

`policy.py` is **not yet imported anywhere** (not in `sandbox/__init__.py`, not in
`plugin_sdk/`) — a self-contained, unwired module importing only stdlib. T1.2 is effectively
done-on-disk; the open work is wiring (export, runner integration, CLI, config schema).

## 5. `plugin_sdk/sandbox.py` — ALREADY EXISTS (plan's framing is wrong)

`plugin_sdk/sandbox.py` (7,304 bytes) is the public contract. Exports (re-exported via
`plugin_sdk/__init__.py:153` + `__all__`):
- `SandboxResult` — frozen/slots dataclass (exit_code, stdout, stderr, duration, wrapped_command, strategy_name).
- `SandboxConfig` — frozen/slots **per-invocation** config (strategy, cpu/memory caps,
  network, container_persistent, read/write paths, allowed_env_vars, image, ssh_host).
- `SandboxStrategyName` — `Literal["auto","macos_sandbox_exec","linux_bwrap","docker","ssh","none"]`.
- `SandboxUnavailable` — exception. `SandboxBackend` (ABC) + `SandboxStrategy` (alias).

Critical distinction: the **existing** `SandboxConfig` here is the *per-call execution*
config (one invocation's caps + paths). M1's **`SandboxPolicy`** in `policy.py` is the
*per-profile persisted* policy (scope + tool allow/deny). Different objects, different
lifetimes — not a conflict, but the names are close; do not conflate.

## 6. `cli_consent.py` — the style T1.4 should mirror

`opencomputer/cli_consent.py` is the reference Typer-subapp pattern:
- Module-level `consent_app = typer.Typer(name="consent", help=..., no_args_is_help=True)`.
- Registered: `from opencomputer.cli_consent import consent_app` then
  `app.add_typer(consent_app, name="consent")` (`cli.py:4875`/`4889` area).
- Profile config access via `_home()` (`from opencomputer.agent.config import _home`) →
  `_home() / "sessions.db"`; opens sqlite + `apply_migrations`.
- Commands are plain `@consent_app.command("name")` functions with `Annotated[..., typer.Option/Argument]`.
- `cli_sandbox.py` already follows this exact shape — T1.4 adds commands consistently.

T1.4 profile-config write path: `oc sandbox enable/disable` edits the `sandbox:` block of
`<profile>/config.yaml`. The config-store round-trip lives in
`opencomputer/agent/config_store.py` (`_apply_overrides` load + an `_encode`/save path);
`SandboxPolicy.from_mapping`/`to_mapping` (`policy.py:108`/`135`) are purpose-built for this.

## 7. Config schema — NO `plugin_sdk/settings.py` (plan T1.5 is wrong)

**`plugin_sdk/settings.py` does not exist.** There is no standalone "settings schema" module.
The config schema IS the dataclass tree in `opencomputer/agent/config.py`:
- Root `Config` (`config.py:1604`) — `@dataclass(frozen=True, slots=True)` composing focused
  sub-configs (`model`, `loop`, `session`, `memory`, `mcp`, `tools`, …) each as a
  `field(default_factory=...)`.
- YAML → dataclass is fully generic: `config_store.py::_apply_overrides` (`config_store.py:75`)
  recursively walks the dataclass tree, applying a parsed-YAML dict over it. A new nested
  dataclass field on `Config` auto-parses from a matching YAML key with **zero** loader code.

T1.5 implication: add a `sandbox: SandboxConfigSection = field(default_factory=...)` field to
`Config`, where the section dataclass carries `scope` + a nested `tools` dataclass with
`allow`/`deny`. No `plugin_sdk/settings.py` to touch. `opencomputer/settings_variants/sandbox.yaml`
is a bundled config *variant* — naming coincidence, it does not define schema.

## 8. `BaseTool` field pattern + `StepOutcome` shape

**`BaseTool`** — `plugin_sdk/tool_contract.py:59`. A plain `ABC` (NOT a dataclass, NOT
Pydantic). Class-level fields are annotated class attributes with defaults:
```python
parallel_safe: bool = False
max_result_size: int = 100_000
capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = ()
strict_mode: ClassVar[bool] = False
```
T1.6's `loop_safe` opt-out is a one-line addition here — e.g. `loop_safe: ClassVar[bool] = True`
(default `True` so only tools that explicitly opt OUT are exempt; the plan wrote `loop_safe`
as an opt-out, so pick the default deliberately). Defaulted class attr → every existing tool
subclass is unchanged; per `plugin_sdk/CLAUDE.md` rule 4 this is a minor-version-safe addition.

**`StepOutcome`** — `opencomputer/agent/step.py:15`. `@dataclass(frozen=True, slots=True)`:
`stop_reason: StopReason`, `assistant_message: Message`, `tool_calls_made: int = 0`, plus
token-count ints, and a `should_continue` property. `stop_reason` is typed as `StopReason`
(an enum in `plugin_sdk/core.py`), **not** a free string — so the plan's literal
`StepOutcome(stop_reason="tool_loop")` will NOT type-check. T1.7 must add a `TOOL_LOOP`
member to the `StopReason` enum (or widen the field).

## 9. The agent loop — tool dispatch + where loop-detection lives

`opencomputer/agent/loop.py` is 333 KB. Landmarks:
- Tool dispatch: `_dispatch_tool_calls` is defined at `loop.py:5622` and called from the
  main run loop at `loop.py:3670` (`tool_results = await self._dispatch_tool_calls(...)`).
- **Two repetition guards already exist and are already fully wired**:
  1. `LoopDetector` — `opencomputer/agent/loop_safety.py`. Per-`(session_id, depth)`
     sliding-window deque. Imported at `loop.py:37`, instantiated at `loop.py:1081`
     (`self._loop_detector = LoopDetector()`), `push_frame` at `loop.py:1744`,
     `record_tool_call` at `loop.py:3757`, `must_stop`→`raise LoopAbortError` at
     `loop.py:3760`, `pop_frame` in `finally` at `loop.py:3849`. Default thresholds:
     `max_tool_repeats=3`, `window_size=10`, `max_consecutive_flags=2`. It hashes
     `(tool_name, sha256(json.dumps(args, sort_keys=True))[:16])` — exactly the plan's recipe.
  2. `ToolLoopGuard` — `opencomputer/agent/tool_guardrails.py`. Streak-counter (warn_at=10,
     stop_at=25). Instantiated at `loop.py:1090`, `.observe()` per call inside
     `_dispatch_tool_calls` at `loop.py:5988`, raises `ToolLoopGuardrailError`.
- On loop-abort the loop catches `LoopAbortError` at `loop.py:3810` and returns a clean
  `ConversationResult` with `stop_reason=StopReason.ERROR`.

T1.6/T1.7 implication: the plan's new `loop_detector.py` with a `LoopDetector` class
**directly collides** with `loop_safety.py::LoopDetector` (same class name, same
sliding-window design, same args hash) and overlaps `ToolLoopGuard`. The plan's 3-in-8 is a
re-spec of the existing 3-in-10. T1.6/T1.7 should **extend `loop_safety.py`** (e.g. add
audit-logging on trip + the `loop_safe` opt-out check), not add a third detector. See §11 m4.

`config.loop.tool_guardrail` is read via `getattr(config.loop, "tool_guardrail", None)` at
`loop.py:1089` — but **no `tool_guardrail` field exists on `LoopConfig`**, so it always
falls back to defaults. Any new loop-detector config should add a real field on `LoopConfig`
(`config.py:368`), not rely on getattr.

## 10. `audit.db` write path + adding `tool_loop_trips`

There is no module literally named after `audit.db`. The audit log is the F1 HMAC-chained
consent log: `opencomputer/agent/consent/audit.py::AuditLogger`.
- **DB-file ambiguity**: `cli_consent.py:58` opens the audit log on `_home()/"sessions.db"`,
  while `consent/gate.py:183` rebinds to `_home()/"audit.db"`. Both run `apply_migrations`
  and both get the `audit_log` table — schema shared, file location differs by caller. T1.7
  must pick which DB it writes `tool_loop_trips` into (the running gateway's gate uses
  `audit.db`; the consent CLI uses `sessions.db`). The agent loop's session state itself
  lives in `sessions.db` via `SessionDB`, so `sessions.db` is the natural home for a
  per-session loop-trip table.
- **Schema + migrations**: `opencomputer/agent/state.py`. `SCHEMA_VERSION = 19`
  (`state.py:137`). `MIGRATIONS` dict (`state.py:356`) maps `(N, N+1)` → a
  `_migrate_vN_to_vN+1` function name; `apply_migrations` (`state.py:1114`) loops them.
- **Pattern to add a table** (mirror `_migrate_v9_to_v10` at `state.py:714`, which adds
  `policy_audit_log`): bump `SCHEMA_VERSION` to 20, add `(19, 20): "_migrate_v19_to_v20"`
  to `MIGRATIONS`, write `_migrate_v19_to_v20(conn)` that `conn.executescript`s
  `CREATE TABLE IF NOT EXISTS tool_loop_trips (...)`.
- **HMAC-chain constraint — important**: the immutable HMAC chain is **per-table**, not a
  property of the DB file. `audit_log` carries its own chain (`prev_hmac`/`row_hmac` columns
  + append-only `BEFORE UPDATE`/`BEFORE DELETE` triggers, `state.py:299-330`). A new
  `tool_loop_trips` table is a **separate, additive** table — adding it does **not** touch
  the `audit_log` chain, so it **cannot break chain verification**. If T1.7 wants
  `tool_loop_trips` itself tamper-evident, ship its own chain columns + append-only triggers,
  exactly as `policy_audit_log` did (`policy_audit_log.py` is the template: genesis HMAC,
  `_canonicalize`, per-row `hmac_prev`/`hmac_self`, `verify_chain`). If plain logging is
  acceptable, a flat table with no chain is fine and simpler.

## 11. SDK boundary rules (for new-code placement)

From `AGENTS.md:41-48` + `plugin_sdk/CLAUDE.md`:
1. Extensions import only from `plugin_sdk/*`; never `opencomputer/**` (frozen-inventory CI test).
2. `plugin_sdk/` must never import `opencomputer/*` (test `test_phase6a.py`).
3. Core (`opencomputer/**`) must not import extensions.
4. Removing/renaming a `plugin_sdk/__init__.py::__all__` name is a major break; adding names
   or adding *defaulted* dataclass fields is minor-safe.

Placement consequences:
- `SandboxPolicy`/`SandboxScope` in `opencomputer/sandbox/policy.py` (where they already
  are) is **core-internal** — fine for the runner + CLI. If the T1.5 config schema dataclass
  or any `BaseTool`/plugin must reference `SandboxScope`, it would have to move to /
  be re-exported from `plugin_sdk/sandbox.py` (rule 2: `plugin_sdk` can't import
  `opencomputer`). `policy.py` currently imports only stdlib, so it is SDK-boundary-clean and
  could move with zero churn. M2's T2.4 already plans to add a resolver to
  `plugin_sdk/sandbox.py`; if `SandboxPolicy` will be referenced there, putting it in
  `plugin_sdk/sandbox.py` now avoids a later move.

---

## OpenClaw scope-policy design (reference)

From `sources/openclaw/docs/gateway/sandboxing.md` + `docs/cli/sandbox.md`. OpenClaw's
sandbox is configured under `agents.defaults.sandbox` (per-agent override
`agents.list[].sandbox`). Four orthogonal axes:

- **mode** (*when* to sandbox): `off` / `non-main` (sandbox only non-main sessions) / `all`.
- **scope** (*how many containers*): `agent` (one per agent, default) / `session` (one per
  session) / `shared` (one container for all sandboxed sessions). OC's `policy.py` keeps
  these three and adds `none` + `tool` (one transient container per call — OC's pre-scope
  behavior, no OpenClaw equivalent).
- **backend**: `docker` / `ssh` / `openshell`.
- **workspaceAccess** (*what the sandbox sees*): `none` / `ro` / `rw`.

**Tool-policy interaction** (`sandbox-vs-tool-policy-vs-elevated`): tool allow/deny is
applied *before* sandbox rules — a globally-denied tool is not resurrected by sandboxing.
`deny` always beats `allow`; a non-empty allow-list implicitly denies everything else. This
is exactly the semantics `SandboxPolicy.tool_allowed()` (`policy.py:94`) implements (OC keeps
exact-name matching; OpenClaw's `group:*` shorthands are explicitly out of M1).
`tools.elevated` is an explicit escape hatch that runs `exec` outside the sandbox.

**CLI**: `openclaw sandbox explain` inspects the *effective* mode/scope/workspaceAccess +
tool policy (with provenance: agent vs global vs default) + fix-it config keys; flags
`--session`, `--agent`, `--json`. `sandbox list` / `sandbox recreate` manage runtimes; prune
at idle 24h / age 7d. OC's planned `oc sandbox explain` mirrors the explain command.
Container reuse / `list` / `recreate` / prune are explicitly **Milestone 2** in OC, not M1.

OpenClaw's `dist/` is bundled hash-named JS; the prose docs above are the authoritative
design source and were used in place of reverse-engineering minified `sandbox-*.js`.

---

## Plan-vs-reality mismatches (read before scoping M1)

1. **`cli_sandbox.py` is NOT a new file** — it exists (4.5 KB, registered at `cli.py:4889`)
   with 3 commands (`status`/`run`/`explain`). T1.4 must extend it. Plus: `oc sandbox
   explain` already exists as an argv dry-run — name overlaps T1.4's policy `explain`.
2. **`plugin_sdk/sandbox.py` is NOT new** — it exists (7.3 KB, the public contract). Its
   `SandboxConfig` is per-invocation; M1's `SandboxPolicy` is per-profile — separate objects.
3. **`opencomputer/sandbox/policy.py` ALREADY EXISTS** (untracked WIP, 195 lines) and already
   contains the *complete* `SandboxScope` enum, `SandboxPolicy` dataclass, `SandboxScopeContext`,
   and `scope_key()`. T1.2 is essentially done-on-disk; the gap is wiring (no importer yet).
   It also has a 5th scope value `shared` that the plan's T1.2 enum (`none/agent/session/tool`)
   omits. (NB: this task's own brief stated `policy.py` "does NOT exist" — that is wrong.)
4. **A new `loop_detector.py` would COLLIDE** — `opencomputer/agent/loop_safety.py` already
   defines a `LoopDetector` class (sliding-window, 3-in-10, per-`(session,depth)` frame,
   `(name, sha256(json args))` hash), fully wired into `loop.py`. `tool_guardrails.py`
   defines a *second* `ToolLoopGuard` streak detector, also wired. The plan's "new
   `loop_detector.py` with a `LoopDetector`" duplicates both. T1.6/T1.7 should extend
   `loop_safety.py` (add audit-DB logging on trip + `loop_safe` opt-out), not add a third.
5. **`StepOutcome(stop_reason="tool_loop")` will not type-check** — `StepOutcome.stop_reason`
   is typed `StopReason` (enum in `plugin_sdk/core.py`), not `str`. T1.7 needs a new enum
   member (`StopReason.TOOL_LOOP`) or a field-type widening.
6. **`plugin_sdk/settings.py` does not exist** — T1.5's "add to `plugin_sdk/settings.py`
   schema" has no target. The schema is the `Config` dataclass tree in
   `opencomputer/agent/config.py`; YAML parsing is generic (`config_store.py::_apply_overrides`).
7. **`tool_loop_trips` table does not exist** anywhere yet — no DB table, no writer. T1.7
   adds it via a `_migrate_v19_to_v20` (current `SCHEMA_VERSION=19`, `state.py:137`).
8. **`runner.py` has no cache key to extend** — T1.3's "container key includes the scope"
   presupposes an existing key; there is none. The Docker per-call uuid at `docker.py:205`
   is the only key-like value and is deliberately non-reusing. `scope_key()` in `policy.py`
   is the intended replacement; T1.3 introduces the keyed-resolution path.
9. **`config.loop.tool_guardrail` is read via `getattr` but no such field exists** on
   `LoopConfig` (`config.py:368`) — it always falls back to defaults. New loop-detector
   config should add a real `LoopConfig` field, not a getattr.
