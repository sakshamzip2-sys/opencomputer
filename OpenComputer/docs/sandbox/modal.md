# Modal cloud sandbox backend

OpenComputer can route a tool's shell command into an **ephemeral Modal
cloud sandbox** instead of running it on the host or in a local
container. [Modal](https://modal.com) creates a transient cloud sandbox
per call, runs the command as the sandbox's entrypoint, and tears the
sandbox down — so the command touches no local filesystem and no local
process namespace.

This is the **`modal`** backend, added in Milestone 2.5 of the
*sandbox-provider-breadth* feature. It sits behind the same
per-tool-call resolver as the local backends, [`e2b`](e2b.md), and
[`daytona`](daytona.md) — see [sandbox-and-scope.md](../sandbox-and-scope.md)
for the resolver and the `sandbox.backend` / `sandbox.fallback` keys.

## When to use `modal` vs other backends

| Use `modal` when… | Pick another backend when… |
|---|---|
| Per-vCPU cost matters — Modal is meaningfully cheaper (`$0.01667/vCPU-hr` vs `$0.0504` for `e2b` / `daytona`). | The host already runs Docker — local containment is free (`docker`). |
| You want separately-captured stdout AND stderr in `SandboxResult` (Daytona merges them via `2>&1`). | The command **must run network-denied** — `modal` cannot enforce that in M2 (see below). |
| The workload tolerates an always-networked sandbox. | Cost must be exactly `$0` — the local backends are free. |
| You're already a Modal user with credentials set up. | The command needs **stdin** — Modal's sandbox-as-entrypoint pattern has no stdin channel. |

`modal` is a **paid** backend (along with `e2b` / `daytona`); `docker`,
`linux_bwrap`, `macos_sandbox_exec`, `ssh`, and `none` cost nothing.

## Install + auth

```sh
pip install opencomputer[modal]
# Either:
export MODAL_TOKEN_ID=... MODAL_TOKEN_SECRET=...
# or:
modal token set     # writes ~/.modal.toml
oc sandbox set --backend modal
```

`oc sandbox status` then shows `modal` as the active backend. The
backend is dormant until you opt in — a default install neither imports
nor calls the SDK.

## How the backend works (one call, end to end)

For each tool call routed to `modal`:

1. `await modal.Sandbox.create.aio(*argv, env=…, timeout=…, workdir=…)` —
   create a fresh sandbox; Modal runs argv as the sandbox's entrypoint
   process. `app=None` is fine (Modal uses an implicit app context).
2. `await sandbox.wait.aio()` → the process's returncode.
3. `await sandbox.stdout.read.aio()` + `await sandbox.stderr.read.aio()`
   — read both streams (Modal captures them separately; no `2>&1` wrap
   needed).
4. `finally:` — `await sandbox.terminate.aio()`. A failed terminate is
   logged at WARNING and never masks the command's result.

The whole sequence is bounded by `asyncio.wait_for(..., timeout=cap)` so a
hanging call surfaces as a `SandboxResult` with `exit_code =
TIMEOUT_EXIT_CODE` and `TIMEOUT_STDERR` in `stderr`.

## Quirks (verified against `modal==1.4.2`)

- **argv is passed positionally.** Modal's `Sandbox.create(*args: str, ...)`
  takes the command as varargs, NOT a single shell string. The backend
  passes argv directly — no `shlex.join` (the e2b / daytona pattern).
- **stdout and stderr are captured separately.** Modal's Sandbox has
  distinct `stdout` and `stderr` `StreamReader`s, so `SandboxResult.stderr`
  carries the real stderr (unlike Daytona, which only captures stdout).
- **Non-zero exit does NOT raise.** `sandbox.wait.aio()` returns the
  returncode; the backend reads it directly. No exception-handling for
  normal command failures.
- **`stdin` is not supported.** Modal's create-and-wait pattern runs argv
  as the sandbox entrypoint; there's no channel to feed input. If a
  caller supplies `stdin`, the backend logs a WARNING and proceeds.
- **`network_allowed=False` is not enforced in M2.** Modal's `create.aio`
  exposes `block_network=True` — enforceable in principle. M2 follows the
  approved warn-and-proceed pattern (`e2b` / `daytona` parity). Use a
  local backend when network-deny must be enforced.
- **The async interface is `.aio`-suffixed.** Modal uses the
  [synchronicity](https://github.com/modal-labs/synchronicity) wrapper, so
  every async call goes through the `.aio` attribute on the bound method
  (e.g. `await Sandbox.create.aio(...)`, `await sandbox.wait.aio()`).

## Cost guard

Modal's published rates (2026): **`$0.01667 / vCPU-hour`** +
**`$0.00833 / GiB-hour`**, billed per second. The seed default in
`opencomputer/cost_guard/sandbox.py` assumes a 2-vCPU + 1 GiB sandbox:

```python
DEFAULT_MODAL_RATE_USD_PER_SECOND = 2 * (0.01667 / 3600) + (0.00833 / 3600)
# ≈ $0.0000116 / second  — about 1/3 of e2b/daytona's per-second rate
```

Per-session sandbox spend is tracked by `SandboxCostGuard`; the default
cap is **`$1 / session`** (`DEFAULT_SESSION_CAP_USD`). Operators tune
both via the persisted `<profile>/sandbox_cost_guard.json`.

## Tests + conformance

`tests/test_sandbox_modal.py` covers the backend against a mocked SDK:
availability combinations (env + `~/.modal.toml` fallback), happy-path
mapping (argv passed positionally is asserted), non-zero exit, wait
raises → terminate-still-called, missing-creds `SandboxUnavailable`, and
the shared `assert_conforms` contract (via the mock's `create.aio`
delegating to `tests.sandbox_conformance.interpret_probe`). Modal passes
the conformance suite without the lenient-stream caveat that applies to
Daytona — Modal's separate stderr capture satisfies the strict assertion
directly.
