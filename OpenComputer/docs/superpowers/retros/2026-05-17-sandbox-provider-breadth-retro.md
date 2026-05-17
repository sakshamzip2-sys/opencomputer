# Retro — Sandbox Provider Breadth (M1–M5 + Phase-7 fixes)

**Date:** 2026-05-17
**Branch:** `sandbox-provider-breadth`
**Workflow:** 8-phase Senior Engineer flow — brainstorm → audit-design →
plan → audit-plan → execute → tdd → review → retro.

---

## 1. What shipped

A native breadth expansion of OpenComputer's sandbox layer, built on the
existing `SandboxBackend` ABC + per-tool-call resolver (crabbox was
investigated and rejected as wrong-shaped — OC already had the resolver).

| Milestone | Deliverable |
|---|---|
| M1 | `SandboxBackend` conformance harness (`tests/sandbox_conformance.py`) — `FakeSandboxBackend` + `assert_conforms`, runs with zero host deps |
| M2 | Native **Daytona** + **Modal** cloud backends; cost-guard rates; `[daytona]`/`[modal]` extras; docs |
| M3 | Docker **container-reuse pool** (`pool.py`) + scope-key threading loop → runtime → bash |
| M4 | `oc sandbox list` / `oc sandbox prune` + an atexit container reaper |
| M5 | `PythonExec(mode="plain")` routed through the resolved backend; PTC/ExecuteCode deliberately not routed (UDS-RPC host-coupling) |

13 milestone commits + 7 Phase-7 fix commits; ~3,000 insertions across
the sandbox package, the two tools, the loop, the CLI, and ~10 test
files.

## 2. What went well

- **The conformance harness (M1) was a real contract test**, not a
  tautology — it plants a non-allowlisted env var and asserts it does
  NOT reach the child, asserts the timeout sentinel, exit-code fidelity,
  `strategy_name`. M2's cloud backends were validated against it.
- **The design audit's P6 finding held up** — "a pooled container's
  flags are fixed at creation, so differing config must key a distinct
  container" was correctly implemented as `_pool_key`'s config digest,
  and Phase 7 only had to *extend* it (credential mounts), not rebuild
  it.
- **Error handling was strong end-to-end** — no silent `except: pass`;
  every broad catch logs at WARNING+; `asyncio.CancelledError` is
  re-raised before broad catches; cost telemetry follows the three-tier
  swallow. The review's axis-3 pass was clean.
- **The phased workflow with per-commit boundaries** made the Phase-7
  fix cycle clean: each fix was its own red→green→commit unit, trivially
  reviewable and revertable.

## 3. What the Phase-7 review caught (the honest part)

The design audit (9 lenses), the plan audit, and per-task TDD all passed
— and still **shipped two blockers**. That is the headline.

- **B1 — `runtime.custom` parallel race.** `_resolve_sandbox_backend`
  publishes the resolved backend on the *shared* `runtime.custom` right
  before dispatch; the tool reads it back inside `execute()`. `BashTool`
  is safe only because it is already never-parallel. `PythonExec` is
  `parallel_safe=True` — two concurrent plain-mode calls clobber each
  other's backend; worst case, a sandbox-required call lands on the bare
  host. **A containment escape.** Every M5 TDD test ran a *single* tool
  call in a *single* `asyncio.run()` — the race was structurally
  invisible to per-unit tests.
- **B2 — Modal backend non-functional.** `Sandbox.create`'s `app` kwarg
  defaults to `None` in the signature; the M2 spike read that and
  concluded "optional". It is *runtime-required* outside a Modal
  container. The backend would raise on every real call — and
  `test_sandbox_modal.py`'s wholesale `modal.Sandbox` mock made every
  test green against it.

Plus: the **pool's `asyncio.Lock` was cached in a process-wide
singleton** and would raise `RuntimeError: bound to a different event
loop` across chat turns — found by the executor during the review,
*missed* by the review agent. And the review agent *hallucinated* one
finding (a "garbled docstring" that did not exist).

## 4. Durable learnings

1. **A signature default is not a runtime contract.** `app: X | None =
   None` does not mean the argument is runtime-optional. Verify API
   *requirements* against docs/behavior, never against signature
   defaults. This shipped a non-functional backend.
2. **Per-call data on shared mutable state assumes sequential
   dispatch.** The `_resolve_sandbox_backend → runtime.custom → tool`
   pattern is only correct when resolve+consume are one sequential unit.
   A `parallel_safe=True` consumer breaks it. Either scope the published
   value by `call.id`, or make the consumer never-parallel (the fix
   taken — consistent with `BashTool`).
3. **`asyncio.Lock` in a process-wide singleton is event-loop-fragile.**
   It binds to the loop it is first *contended* on; OC runs each chat
   turn in its own `asyncio.run()`, so a cached lock breaks on the next
   turn. Singletons holding asyncio primitives must be loop-aware.
4. **Wholesale SDK mocks hide non-functional code.** A mock that accepts
   any kwargs makes a broken call shape green. Mocks must assert the
   call shape the *real* API requires.
5. **TDD per-unit misses cross-unit races.** Concurrency bugs need
   concurrency tests — dispatch two calls, assert no cross-contamination.
6. **Reviews — agent or self — need verification both ways.** The review
   agent missed a real bug and invented a fake one. Treat every review
   finding as a lead to verify, not a fact.

## 5. Known limitations / honest follow-ups

- **Modal cannot be integration-tested here** — no `MODAL_TOKEN_ID`. The
  B2 fix is doc-verified and the mock now asserts the `app` shape, but a
  real-token smoke test is still owed before Modal is trusted in prod.
- **Daytona `network_allowed=False` is still not enforced** — its
  `network_block_all` lives on a params object (more plumbing than
  Modal's one-kwarg `block_network`). Warn-and-proceed; a follow-up.
- **Pooled-container timeout zombies** — a timed-out command's
  in-container process is not reaped; repeated timeouts can exhaust
  `--pids-limit`. `oc sandbox prune` is the remedy (documented inline).
- **`oc sandbox set --backend none`** is accepted (routes Bash through
  the host with a WARNING). Judged intended — `none` is a legitimate
  `SandboxStrategyName`. Not changed.
