# Sandbox Provider Breadth — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Tasks use checkbox (`- [ ]`) syntax. Every non-trivial task is implemented under TDD — red (failing test) → green (minimal code) → refactor.

**Goal:** Give OpenComputer native Daytona and Modal sandbox backends behind the existing per-tool-call resolver, a real Docker container-reuse pool, and `PythonExec` plain-mode sandbox routing — all gated by one shared `SandboxBackend` conformance suite.

**Architecture:** New backends are core `opencomputer/sandbox/*.py` files implementing the existing `plugin_sdk.SandboxBackend` ABC, wired through the `SandboxStrategyName` Literal + `runner._named_strategy()`. Container reuse is a new internal `opencomputer/sandbox/pool.py` keyed on the existing `container_key` / `scope_key` seam — **zero `plugin_sdk` contract change**. A parametrized conformance suite plus an always-available `FakeSandboxBackend` gate every backend in CI.

**Tech stack:** Python 3.12+, pytest, `daytona` SDK (PyPI `daytona`), `modal` SDK (PyPI `modal`), Docker CLI.

**Provenance:** brainstorm (7 approaches scored on Effort/Risk/Upside) → design audit (9 lenses) → plan → plan audit (5 questions). Chosen approach: *"Harness-first, leverage-ordered slices."*

**Environment:** worktree branch `worktree-sandbox-provider-breadth`. Run tests with `/Users/saksham/Vscode/claude/OpenComputer/.venv/bin/python3 -m pytest …` from the worktree's `OpenComputer/` directory — cwd-based import resolves `opencomputer` to the worktree; the `.venv` supplies dev deps (incl. `pytest-timeout`, which anaconda base lacks). Plan-time baseline: sandbox buckets 112 passed / 1 skipped.

---

## Definition of done

`oc sandbox set --backend daytona|modal` routes a tool call into a native cloud sandbox; two same-session, same-containment Docker-backed Bash calls reuse one container instead of minting a fresh one; `PythonExec` plain mode runs inside the resolved backend (PTC mode untouched); every backend — old and new — passes the shared conformance suite; the targeted sandbox + tools test buckets are green.

## File-structure map

| Action | Path | Responsibility |
|---|---|---|
| Create | `opencomputer/sandbox/daytona.py` | `DaytonaSandboxStrategy` |
| Create | `opencomputer/sandbox/modal.py` | `ModalSandboxStrategy` |
| Create | `opencomputer/sandbox/pool.py` | `ContainerPool` — Docker persistent-container reuse |
| Create | `tests/sandbox_conformance.py` | shared `FakeSandboxBackend` + conformance helpers |
| Create | `tests/test_sandbox_conformance.py` | parametrized contract suite |
| Create | `tests/test_sandbox_daytona.py`, `…_modal.py`, `…_pool.py`, `test_python_exec_sandbox_routing.py` | per-unit tests |
| Modify | `plugin_sdk/sandbox.py` | `SandboxStrategyName` Literal `+= "daytona","modal"` |
| Modify | `opencomputer/sandbox/runner.py` | `_named_strategy()` branches + error string |
| Modify | `opencomputer/sandbox/__init__.py` | export new strategies |
| Modify | `opencomputer/sandbox/docker.py` | pooled `docker exec` path when `container_key` set |
| Modify | `opencomputer/cost_guard/sandbox.py` | `DEFAULT_BACKEND_RATES_USD_PER_SECOND` += daytona, modal |
| Modify | `opencomputer/agent/loop.py` | publish `scope_key` onto `runtime.custom` |
| Modify | `opencomputer/tools/bash.py` | thread `container_key` into `SandboxConfig` |
| Modify | `opencomputer/tools/python_exec.py` | plain-mode resolver routing |
| Modify | `opencomputer/cli_sandbox.py` | `oc sandbox list` / `prune` |
| Modify | `pyproject.toml` | `[daytona]` / `[modal]` optional extras |

## Milestones

### M1 — `SandboxBackend` conformance harness · size M
Depends on: nothing. Exit gate: contract suite green via `FakeSandboxBackend` in CI; existing backends gated by availability.

- [ ] **T1.1 (M)** — `tests/sandbox_conformance.py`: a `FakeSandboxBackend(SandboxBackend)` (always `is_available()`, in-memory `run()` honoring timeout / exit-code / env-allowlist) + `assert_conforms(backend, config)` helpers checking ABC clauses 1–4 (env strip, timeout kill + `TIMEOUT_EXIT_CODE`/`TIMEOUT_STDERR`, exit-code fidelity, `strategy_name == backend.name`, `explain()` returns argv without side effects).
- [ ] **T1.2 (M)** — `tests/test_sandbox_conformance.py`: parametrize `assert_conforms` over `FakeSandboxBackend` (always) + `none`/`docker`/`bwrap`/`macos`/`ssh`/`e2b` (each `skipif(not is_available())`). Run; fix or document any drift in the existing backends.

### M2 — Native Daytona + Modal backends · size L · ⭐ MVP
Depends on: M1 *(preferred — conformance is M2's exit gate, not a hard entry blocker)*. Exit gate: `oc sandbox set --backend daytona|modal` resolves; both pass conformance; `rate_for("daytona"|"modal") > 0`.

- [ ] **T2.0 (S)** — SDK spike: from the installed `daytona` / `modal` SDKs confirm exact signatures — `daytona.create()` kwargs + `sandbox.process.exec()` return shape; `modal.Sandbox.create()` (does it need an `App`?) + `.exec()` / `ContainerProcess`. Record findings in the backend docstrings (the `e2b.py` M-1…M-9 pattern).
- [ ] **T2.1 (M)** — `opencomputer/sandbox/daytona.py`: `DaytonaSandboxStrategy`, `name = "daytona"`; lazy `import daytona`; `is_available()` = `importlib.util.find_spec("daytona")` + `DAYTONA_API_KEY`; `run()` = create → `process.exec(shlex.join(argv))` → teardown in `finally`; `explain()` synthetic argv. TDD vs a mock SDK + the conformance suite.
- [ ] **T2.2 (S)** — wire Daytona: `"daytona"` into `SandboxStrategyName` (`plugin_sdk/sandbox.py`); `_named_strategy()` branch + error-string update (`runner.py:54-58`); `__init__.py` export; **verify/update `cli_sandbox.py` valid-backends** (do not assume it auto-derives).
- [ ] **T2.3 (S)** — `DEFAULT_BACKEND_RATES_USD_PER_SECOND["daytona"]` = a documented conservative estimate with a cited pricing-page comment; test asserts `rate_for("daytona") > 0`.
- [ ] **T2.4 (S)** — `pyproject.toml` `[daytona]` extra; `docs/sandbox/daytona.md`.
- [ ] **T2.5 (M)** — `opencomputer/sandbox/modal.py`: `ModalSandboxStrategy`, `name = "modal"`; `modal.Sandbox.create(...)` → `.exec(*argv, timeout=…)` → read `ContainerProcess` streams → terminate in `finally`; `is_available()` = `find_spec("modal")` + Modal token env. Include an import-order test — `import opencomputer.sandbox.modal` must still reach the real `modal` SDK.
- [ ] **T2.6–T2.8 (S·S·S)** — Modal wiring + cost rate + extra/docs (mirror T2.2–T2.4).

### M3 — Docker reuse pool + scope-key threading · size L
Depends on: M1. Exit gate: integration test — two same-session, same-containment Bash calls reuse one container.

- [ ] **T3.1 (M)** — `opencomputer/sandbox/pool.py`: `ContainerPool`. `acquire(pool_key) -> container_id` — on miss, `docker run -d --name oc-pool-<pool_key> <image> sleep infinity`; liveness-probe (`docker inspect`) before reuse, recreate on miss; per-`pool_key` `asyncio.Lock`. **No persisted registry — Docker (`docker ps --filter name=oc-pool-`) is the source of truth**; the in-process dict is a latency cache only.
- [ ] **T3.2 (L)** — `docker.py`: `pool_key = scope_key + digest(image, network_allowed, memory_mb_limit, read_paths, write_paths, container_persistent)`. When `config.container_key` set + pooling enabled, `acquire()` then `docker exec` into the pooled container; keep the `--rm` transient path for keyless calls and config-mismatch fallback.
- [ ] **T3.3 (M)** — thread `scope_key`: `loop._resolve_sandbox_backend` computes it (verify session/agent ids on the runtime — `agent_id` may be absent; `scope_key()` already degrades to a per-call uuid) and publishes it on `runtime.custom`; `bash.py:_execute_in_sandbox` sets `SandboxConfig.container_key`. Tests must rebind `loop._runtime` (gotcha #9).
- [ ] **T3.4 (S)** — integration test: two same-session Bash calls reuse one `oc-pool-` container.

### M4 — `oc sandbox list` / `prune` + reaper · size S-M
Depends on: M3. Exit gate: `oc sandbox list` shows a pooled container; `prune` removes it.

- [ ] **T4.1 (M)** — `cli_sandbox.py`: `oc sandbox list` renders `docker ps --filter name=oc-pool-`; `oc sandbox prune` runs `docker rm -f` over that filter.
- [ ] **T4.2 (S)** — best-effort reaper: `atexit` hook + TTL sweep of stale `oc-pool-` containers.
- [ ] **T4.3 (S)** — CLI tests for `list` / `prune`.

### M5 — `PythonExec` plain-mode routing · size M
Depends on: M1 (the resolver pre-exists). Exit gate: `PythonExec` plain mode runs inside the resolved backend; ptc mode untouched.

- [ ] **T5.1 (M)** — `python_exec.py:_execute_plain`: consult the resolver (mirror `bash.py:_execute_in_sandbox`); when a backend is resolved, wrap `["python3","-c",code]`, run in the backend, `network_allowed=False` (Bash-consistent default); keep `is_safe_script()` denylist as defense-in-depth; host path unchanged when no backend resolves.
- [ ] **T5.2 (S)** — document, in `python_exec.py` + `execute_code.py` docstrings + `docs/sandbox/`, *why* PTC mode / `ExecuteCode` are deliberately not routed (UDS-RPC back to the host registry is incompatible with remote isolation).
- [ ] **T5.3 (S)** — `tests/test_python_exec_sandbox_routing.py`: plain mode routes; ptc mode does not; a Python-less Docker image fails cleanly.

## Audit resolutions

**Design audit (Phase 2) — F1–F17.** B rescoped to `PythonExec` plain mode only: `ExecuteCode` and `PythonExec` ptc mode call `run_ptc()` (UDS-RPC back to the host registry — cannot route to a remote sandbox; routing to a local one defeats the isolation). No `SandboxBackend` ABC change — pooling rides the existing `container_key`. Every paid backend must carry a cost rate or it silently bypasses the per-session cap (`rate_for()` returns `0.0` for unlisted backends).

**Plan audit (Phase 4) — P1–P11.** M1→M2 dependency softened to "preferred" (removes the schedule SPOF); M1 gains `FakeSandboxBackend` so the contract runs in CI regardless of platform/daemon/keys; pool key includes a containment-config digest (a persistent container cannot honor differing per-call config); no persisted pool registry (Docker itself is the registry — designs out registry/reality drift); `oc sandbox recreate` cut (YAGNI — `list` + `prune` suffice).

## MVP

**M1 + M2** — the conformance harness plus both native cloud backends. Delivers the headline ask ("more sandbox providers"). M3–M5 close the audited gaps.
