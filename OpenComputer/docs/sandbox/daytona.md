# Daytona cloud sandbox backend

OpenComputer can route a tool's shell command into an **ephemeral Daytona
cloud sandbox** instead of running it on the host or in a local
container. [Daytona](https://www.daytona.io) creates a transient cloud
sandbox per call, runs the command, and tears the sandbox down — so the
command touches no local filesystem and no local process namespace.

This is the **`daytona`** backend, added in Milestone 2.1 of the
*sandbox-provider-breadth* feature. It sits behind the same per-tool-call
resolver as the local backends and [`e2b`](e2b.md) — see
[sandbox-and-scope.md](../sandbox-and-scope.md) for the resolver and the
`sandbox.backend` / `sandbox.fallback` keys.

## When to use `daytona` vs other backends

| Use `daytona` when… | Pick another backend when… |
|---|---|
| You want a cloud sandbox alternative to `e2b` (multi-vendor for portability or pricing). | You already use `e2b` and don't need a second cloud provider — they have the same per-vCPU rate. |
| The host has no Docker daemon and no local sandbox binary. | The host already runs Docker — local containment is free and faster (use `docker`). |
| The workload tolerates an always-networked sandbox. | The command **must run network-denied** — `daytona` cannot enforce that in M2 (see below). |
| You accept a per-second cloud cost for isolation. | Cost must be exactly `$0` — the local backends are free. |
| You need stdout from the command. | The command relies on a **separately-captured stderr** — Daytona's SDK only captures stdout; the backend merges stderr in via `2>&1` (see below). |

`daytona` is a **paid** backend (same as `e2b`). `docker`, `linux_bwrap`,
`macos_sandbox_exec`, `ssh`, and `none` cost nothing.

## Install + auth

```sh
pip install opencomputer[daytona]
export DAYTONA_API_KEY=...   # from https://app.daytona.io/dashboard
oc sandbox set --backend daytona
```

`oc sandbox status` will then show `daytona` as the active backend. The
backend is dormant until you opt in via `oc sandbox set --backend …` — a
default install neither imports nor calls the SDK.

## How the backend works (one call, end to end)

For each tool call routed to `daytona`:

1. Open `async with AsyncDaytona() as client:` — the SDK's async context
   manager handles client teardown (close) on exit.
2. `await client.create()` — get a fresh `AsyncSandbox` on Daytona's
   infrastructure.
3. `await sandbox.process.exec("(" + shlex.join(argv) + ") 2>&1", cwd=…,
   env=…, timeout=…)` — execute the wrapped command.
4. `finally:` — `await client.delete(sandbox)`. A failed delete is logged
   at WARNING and never masks the command's result.

The whole sequence is bounded by `asyncio.wait_for(..., timeout=cap)` so a
hanging call surfaces as a `SandboxResult` with `exit_code = TIMEOUT_EXIT_CODE`
and `TIMEOUT_STDERR` in `stderr`.

## Quirks (verified against `daytona==0.176.0`)

- **stderr is merged into stdout via a `2>&1` wrap.** Daytona's
  `ExecuteResponse` only populates `result` (the SDK docstring is explicit:
  *"result: Standard output from the command"*). To preserve stderr content
  for callers (`Bash`-tool users see stderr in their output), the backend
  wraps every command as `(<cmd>) 2>&1`. `SandboxResult.stderr` is therefore
  always `""`; the combined output is in `stdout`.
- **Non-zero exit does NOT raise.** Unlike `e2b`'s `CommandExitException`,
  Daytona returns `ExecuteResponse(exit_code=N, result=...)` and the backend
  reads `exit_code` directly. No exception-handling for normal command
  failures.
- **`stdin` is not supported.** Daytona's `process.exec` has no stdin
  channel. If a caller supplies `stdin`, the backend logs a WARNING and
  proceeds; the input does NOT reach the wrapped command.
- **`network_allowed=False` is not enforced in M2.** Daytona's
  `CreateSandboxFromSnapshotParams` exposes `network_block_all`, so this is
  enforceable in principle — M2 follows the approved warn-and-proceed
  pattern (same as `e2b`'s M-7) and logs a WARNING. A future milestone may
  honor it; until then, use a local backend (`docker` / `linux_bwrap` /
  `macos_sandbox_exec`) when network-deny must be enforced.

## Cost guard

Daytona's published rates (2026): **`$0.0504 / vCPU-hour`** +
**`$0.0162 / GiB-hour`**, billed per second. The seed default in
`opencomputer/cost_guard/sandbox.py` assumes a 2-vCPU + 1 GiB sandbox:

```python
DEFAULT_DAYTONA_RATE_USD_PER_SECOND = 2 * (0.0504 / 3600) + (0.0162 / 3600)
# ≈ $0.0000325 / second
```

Per-session sandbox spend is tracked by `SandboxCostGuard`; the default
cap is **`$1 / session`** (`DEFAULT_SESSION_CAP_USD`). Operators tune
both via the persisted `<profile>/sandbox_cost_guard.json`.

## Tests + conformance

`tests/test_sandbox_daytona.py` covers the backend against a mocked SDK:
availability combinations, happy-path mapping, non-zero exit, exception →
teardown-still-called, missing-key `SandboxUnavailable`, the `2>&1` wrap
assertion, and the shared `assert_conforms` contract (via the mock's
`process.exec` delegating to `tests.sandbox_conformance.interpret_probe`).
The conformance suite's `stderr` / timeout-sentinel assertions are
lenient on the stream (stderr OR stdout) to accommodate the merged
output — see `tests/sandbox_conformance.py`.
