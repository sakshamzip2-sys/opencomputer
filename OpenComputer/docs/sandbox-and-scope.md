# Sandbox & scope policy

OpenComputer can run tool commands inside a containment **sandbox** rather
than directly on the host. Milestone 1 of the Hermes + OpenClaw parity work
adds a per-profile **scope policy** on top of the existing backends: it
controls how sandbox containers are keyed and which tools may run inside
them.

## Backends

A sandbox *backend* is the mechanism that provides containment. `oc sandbox
status` lists the backends and shows which one `auto` selects on this host:

| Backend | Mechanism | Availability | Cost |
|---|---|---|---|
| `macos_sandbox_exec` | `sandbox-exec` profile | macOS | free |
| `linux_bwrap` | `bwrap` (bubblewrap) namespaces | Linux + `bwrap` installed | free |
| `docker` | transient `docker run` container | any host with a Docker daemon | free |
| `ssh` | runs argv on a remote host (isolation by separation, **not** containment) | any host with `ssh` | free |
| `e2b` | ephemeral E2B cloud sandbox VM | `pip install opencomputer[e2b]` + `E2B_API_KEY` set | **billed per second** |
| `none` | no containment — runs directly, logs a warning | always | free |

`auto` prefers the host-native backend (`macos_sandbox_exec` / `linux_bwrap`),
then falls back to `docker`. The cloud `e2b` backend is **never** picked by
`auto` — it is a paid backend and must be chosen explicitly via
`sandbox.backend` (see below). It has its own page:
[sandbox/e2b.md](sandbox/e2b.md).

## Scope

`sandbox.scope` answers *how many containers exist, and what shares one* —
ported from OpenClaw's `agents.defaults.sandbox.scope`:

| Scope | Meaning |
|---|---|
| `none` | sandboxing is off — tool commands run on the host. **Default.** |
| `tool` | one fresh, transient container per tool call (no sharing). |
| `session` | one container per session. |
| `agent` | one container per agent. |
| `shared` | one container shared by every sandboxed call. |

The default is `none`, so an upgrading installation sees **zero behavior
change** until sandboxing is explicitly enabled.

## The per-tool-call resolver (Milestone 2)

Milestone 1 ships the *scope* — how containers are keyed. Milestone 2 adds
the **per-tool-call backend resolver**: the piece that decides, for each
tool invocation, *which backend* (if any) the call routes to.

`opencomputer/sandbox/resolver.py::resolve_backend` is called by the agent
loop just before each tool dispatch. It looks at the tool, the active
config, and answers either "route this call through backend X" or "no
sandbox — run the tool exactly as it would run with sandboxing off". Two
new keys of the `sandbox:` config block steer it:

| Key | Meaning |
|---|---|
| `sandbox.backend` | The default backend a tool call routes to — a concrete strategy name (`docker`, `e2b`, `linux_bwrap`, …). **Unset** (the default) means sandboxing is *not opted into*: every ordinary tool runs un-sandboxed, byte-identical to pre-M2 behavior. |
| `sandbox.fallback` | What happens when the chosen backend is **unreachable**. `error` (default) fails the call loud — OC never silently downgrades containment. `local` runs the call on the host with a logged WARNING. |

The resolver's decision order, briefly: a tool that opts out
(`sandbox_preference="skip"`) is never sandboxed; with no `sandbox.backend`
configured an ordinary tool runs un-sandboxed (the no-op default path); a
tool's `sandbox_backend_hint` is honored when that backend is available;
otherwise the configured `sandbox.backend` is used. A tool that declares
`sandbox_preference="required"` is the one case that can hard-fail when no
backend is reachable and `fallback=error`.

**Only the `Bash` tool currently routes through a resolved backend.** The
`ExecuteCode` / `PythonExec` tools do not — their `run_ptc` local
UDS-RPC transport cannot cross into a cloud sandbox. See
[sandbox/e2b.md](sandbox/e2b.md#known-limitation--only-bash-routes-through-a-sandbox).

The cloud [`e2b`](sandbox/e2b.md) backend is the headline M2 addition: an
ephemeral cloud sandbox, billed per second, gated by a per-session cost
cap.

## The `oc sandbox` CLI

```bash
oc sandbox status                            # backends available + what `auto` picks
oc sandbox enable --scope session            # turn sandboxing on at a given scope
oc sandbox disable                           # turn sandboxing off (scope → none)
oc sandbox set --backend e2b                 # set the default backend a tool call routes to
oc sandbox set --scope session --fallback local   # set scope + fallback in one call
oc sandbox explain                           # print the effective policy + what a call resolves to
oc sandbox explain -- echo hi                # print the wrapped command (dry-run)
oc sandbox run -- pytest -x                  # run a command through the active policy
```

`oc sandbox enable` / `disable` write the `sandbox.scope` key; `oc sandbox
set` (M2) writes the `sandbox.backend` / `sandbox.scope` / `sandbox.fallback`
keys — each flag is optional and only the keys you pass are changed.
Existing tool allow/deny lists are preserved across every write, and the
config file is written atomically. `--backend` / `--scope` / `--fallback`
values are validated before anything is written; an unknown value is
rejected with the accepted set.

`oc sandbox explain` with no argument is the policy inspector. M2 extends
it to show the configured backend, whether it is available on this host,
the fallback policy, and a one-line summary of what an ordinary tool call
resolves to:

```
$ oc sandbox explain
                         Sandbox policy
  scope         session  (one container per session)
  enabled       yes
  backend       e2b  (unavailable on this host)
  fallback      error  (fail the call loud — never silently downgrade containment)
  tools allow   (all tools)
  tools deny    Bash
  host backend  macos_sandbox_exec  (what `auto` would pick)
resolves to: e2b is unreachable here — sandbox.fallback=error, so a tool
that requires a sandbox fails loud; an ordinary tool runs un-sandboxed.
config keys: sandbox.backend · sandbox.scope · sandbox.fallback · sandbox.tools.allow · sandbox.tools.deny
policy file: ~/.opencomputer/<profile>/config.yaml
```

## Configuration

The policy lives under the top-level `sandbox:` key of `config.yaml`:

```yaml
sandbox:
  scope: session
  backend: e2b            # M2 — default backend a tool call routes to; unset = sandboxing off
  fallback: error         # M2 — error (fail loud, default) | local (run on host with a WARNING)
  tools:
    allow: [Read, Bash]   # optional — empty means "all tools"
    deny:  [Bash]         # optional — empty means "none denied"
```

An unrecognised `scope` **or** `fallback` value fails loudly at config load
(a typo should never silently disable the sandbox or downgrade
containment). The M2 `backend` / `fallback` keys default to *unset* /
`error` — an upgrading config with no `sandbox:` block, or one carrying
only the M1 `scope` key, behaves exactly as before.

## Tool allow / deny

When sandboxing is enabled, `sandbox.tools.allow` / `deny` restrict which
tools may run inside the sandbox. The rules (OpenClaw semantics):

- `deny` always wins — a denied tool is blocked even if it is also allowed.
- A non-empty `allow` list makes everything **not** listed implicitly denied.
- An all-empty policy permits every tool.

Matching is by exact tool name; `group:*` shorthands are not in Milestone 1.

## What shipped in each milestone

**Milestone 1** ships the scope policy object, its persistence, the M1
`oc sandbox` management CLI (`status` / `enable` / `disable` / `explain`),
and the scope-key plumbing through `run_sandboxed` (the Docker backend
names its container from the scope key).

**Milestone 2** adds the per-tool-call **resolver**
(`opencomputer/sandbox/resolver.py`), the `sandbox.backend` /
`sandbox.fallback` config keys, `oc sandbox set`, the extended `oc sandbox
explain`, the ephemeral cloud [`e2b`](sandbox/e2b.md) backend, and the
per-session sandbox **cost guard** that meters `e2b`'s per-second billing.

Persistent-container **reuse** and lifecycle (`oc sandbox list` /
`recreate` / prune) remain out of scope — see the parity plan at
`docs/superpowers/specs/2026-05-16-oc-parity-with-hermes-openclaw/`.
