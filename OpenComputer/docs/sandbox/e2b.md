# E2B cloud sandbox backend

OpenComputer can route a tool's shell command into an **ephemeral E2B
cloud sandbox** instead of running it on the host or in a local
container. E2B (https://e2b.dev) boots a transient cloud VM per call,
runs the command, and tears the VM down — so the command touches no
local filesystem and no local process namespace.

This is the **`e2b`** backend, added in Milestone 2 of the Hermes +
OpenClaw parity work. It sits behind the same per-tool-call resolver as
the local backends — see [sandbox-and-scope.md](../sandbox-and-scope.md)
for the resolver and the `sandbox.backend` / `sandbox.fallback` keys.

## When to use `e2b` vs `docker`

| Use `e2b` when… | Use `docker` (or `linux_bwrap` / `macos_sandbox_exec`) when… |
|---|---|
| The host has no Docker daemon and no local sandbox binary. | The host already runs Docker — local containment is free and faster. |
| You want the command to run on infrastructure fully separate from the host. | The command **must run network-denied** — `e2b` cannot enforce that (see below). |
| The workload is bursty and you don't want a always-on container host. | The command needs `stdin`, host bind-mounts, or a specific base image — `e2b` ignores those. |
| You accept a per-second cloud cost for the isolation. | Cost must be exactly `$0` — the local backends are free. |

`e2b` is the only **paid** backend. `docker`, `linux_bwrap`,
`macos_sandbox_exec`, `ssh`, and `none` cost nothing.

## Install

The `e2b` SDK is an **optional extra** — the core install stays
dependency-light and only users who want the cloud backend pull it:

```bash
pip install opencomputer[e2b]
```

This installs the `e2b` Python package (the core SDK, pinned `>=2.21,<3`).

## Authentication — `E2B_API_KEY`

The E2B SDK authenticates with a single API key, read from the
`E2B_API_KEY` environment variable:

```bash
export E2B_API_KEY=e2b_...
```

Issue a key from the E2B dashboard (https://e2b.dev/dashboard); keys are
prefixed `e2b_`. Like every other provider credential, put it in the
active profile's `.env` (`~/.opencomputer/<profile>/.env`) so it is
profile-scoped.

The `e2b` backend's `is_available()` check returns `False` — cleanly, no
exception — when **either** the `e2b` package is not installed **or**
`E2B_API_KEY` is unset. With `e2b` configured as `sandbox.backend` but
unavailable, the [`sandbox.fallback`](#the-fallback-policy) policy
decides what happens.

## Configuration

Set `e2b` as the backend a tool call routes to:

```bash
oc sandbox set --backend e2b
```

or, by hand, in `~/.opencomputer/<profile>/config.yaml`:

```yaml
sandbox:
  backend: e2b
  fallback: error   # or: local
```

`oc sandbox explain` then shows the configured backend, whether it is
available on this host, the fallback policy, and what an ordinary tool
call resolves to.

## The cost model — per-second billing + a session cap

**E2B bills per running second** of sandbox wall-clock time (a CPU
component plus a RAM component). The local backends are free; only `e2b`
accrues cost.

### Per-second rate

The per-second USD rate is **config-driven, never hard-coded**. It lives
in the profile's sandbox cost-guard file at
`~/.opencomputer/<profile>/sandbox_cost_guard.json` (a file the sandbox
guard owns outright — distinct from the provider cost guard's
`cost_guard.json`), under a `sandbox` section:

```json
{
  "sandbox": {
    "rates": { "e2b": 3.25e-05 },
    "session_cap_usd": 1.0,
    "sessions": { "<session-id>": { "spend_usd": 0.0123, "updated": 1700000000.0 } }
  }
}
```

`rates.e2b` is seeded on first use from E2B's published figure
(CPU ≈ `$0.000028/s` + RAM ≈ `$0.0000045/GiB/s` for the default 2-vCPU
sandbox — see [the SDK survey](../refs/e2b/2026-05-16-sdk-survey.md)).
E2B adjusts its rates over time, so **edit the persisted `rates.e2b`
value** rather than expecting the seed default to track E2B's pricing.
A backend with no entry in `rates` (every local backend) costs `$0`.

The E2B SDK does **not** report duration or cost in its call metadata —
OpenComputer measures wall-clock time itself (`SandboxResult.duration_seconds`,
the same `time.monotonic()` delta every backend records) and multiplies
by the configured rate.

### Session cap

Each session has a **sandbox spend cap** — `session_cap_usd` in the same
`sandbox_cost_guard.json`, defaulting to **`$1` per session**. After every sandboxed `Bash`
run, `duration_seconds × rate(backend)` is recorded against the
session's running total. Before a sandboxed run, if the session has
**already** crossed its cap, the run is refused with a clear error
result *instead of* running (a sandbox would only run up more cost).

Local-backend runs cost `$0` and can never push a session over the cap,
so an over-cap session still runs fine on `docker` / `bwrap` / etc. —
the cap gates **paid** spend only.

To change the cap, edit `session_cap_usd` in `sandbox_cost_guard.json`. A `$0`
cap means no paid sandboxed run is ever in budget — a way to fully
disable paid sandboxing for a profile while leaving the backend
configured.

A cost-guard read/write failure (corrupt file, disk error) never breaks
tool execution — it is logged at WARNING and the command proceeds; the
cost guard fails open so a telemetry hiccup can't wedge the agent.

## `network_allowed=False` is the default — and `e2b` cannot enforce it

OpenComputer's `SandboxConfig` **defaults to `network_allowed=False`**
(deny outbound network). The local backends honor this — `docker` and
`linux_bwrap` actually block the network; the sandboxed command has no
outbound access.

**E2B sandboxes are cloud VMs that are always networked.** There is no
per-call network-deny switch in the E2B SDK. So when a command is routed
to `e2b`, the `e2b` backend logs a loud **WARNING** that network
containment was requested and could not be enforced, and runs the
command anyway (**with** outbound network access).

This is a deliberate Milestone-2 policy decision: `e2b` warns rather
than refusing the default config. **If a command must run
network-denied, use a local backend** (`docker` / `linux_bwrap` /
`macos_sandbox_exec`) — those enforce it; `e2b` does not.

## The `sandbox.fallback` policy

`sandbox.fallback` governs what happens when `e2b` is configured as the
backend but is **unreachable** — the `e2b` package is missing, the API
key is unset, or an `AsyncSandbox.create()` call fails (network / auth
error):

| `sandbox.fallback` | Behavior when `e2b` is unreachable |
|---|---|
| `error` (default) | The tool call **fails loud** with an error result. OpenComputer never silently downgrades containment. |
| `local` | The command runs **on the host** (no containment) with a logged WARNING. An explicit opt-in to host fallback. |

Set it with `oc sandbox set --fallback <error|local>`. The default is
`error` on purpose: running un-sandboxed should be a conscious choice.

## Known limitation — only `Bash` routes through a sandbox

Only the **`Bash`** tool routes its command through a resolved sandbox
backend. The `ExecuteCode` / `PythonExec` tools do **not** — their
implementation talks to a per-tool-call Python runtime over a local
Unix-domain-socket RPC (`run_ptc`), and that UDS-RPC channel cannot
cross into a cloud sandbox: the socket is local to the host. Routing
`ExecuteCode` through `e2b` would require a different transport and is
out of Milestone-2 scope.

So with `sandbox.backend: e2b` configured:

- `Bash` commands → run inside an E2B cloud sandbox (cost-metered, cap-gated).
- `ExecuteCode` / `PythonExec` → run on the host, exactly as before.

If you need Python execution inside a sandbox, write the Python to a
file and run it via `Bash` (`python3 script.py`) — that `Bash` call
*does* route through the sandbox.

## See also

- [sandbox-and-scope.md](../sandbox-and-scope.md) — the resolver, the
  `backend` / `fallback` / `scope` config keys, and the local backends.
- [refs/e2b/2026-05-16-sdk-survey.md](../refs/e2b/2026-05-16-sdk-survey.md)
  — the full E2B SDK survey M2 was built on.
