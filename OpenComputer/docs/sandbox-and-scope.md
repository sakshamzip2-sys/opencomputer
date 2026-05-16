# Sandbox & scope policy

OpenComputer can run tool commands inside a containment **sandbox** rather
than directly on the host. Milestone 1 of the Hermes + OpenClaw parity work
adds a per-profile **scope policy** on top of the existing backends: it
controls how sandbox containers are keyed and which tools may run inside
them.

## Backends

A sandbox *backend* is the mechanism that provides containment. `oc sandbox
status` lists the backends and shows which one `auto` selects on this host:

| Backend | Mechanism | Availability |
|---|---|---|
| `macos_sandbox_exec` | `sandbox-exec` profile | macOS |
| `linux_bwrap` | `bwrap` (bubblewrap) namespaces | Linux + `bwrap` installed |
| `docker` | transient `docker run` container | any host with a Docker daemon |
| `ssh` | runs argv on a remote host (isolation by separation, **not** containment) | any host with `ssh` |
| `none` | no containment — runs directly, logs a warning | always |

`auto` prefers the host-native backend (`macos_sandbox_exec` / `linux_bwrap`),
then falls back to `docker`.

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

## The `oc sandbox` CLI

```bash
oc sandbox status                 # backends available + what `auto` picks
oc sandbox enable --scope session # turn sandboxing on at a given scope
oc sandbox disable                # turn sandboxing off (scope → none)
oc sandbox explain                # print the effective scope policy
oc sandbox explain -- echo hi     # print the wrapped command (dry-run)
oc sandbox run -- pytest -x       # run a command through the active policy
```

`oc sandbox enable` and `disable` write the `sandbox.scope` key of the active
profile's `config.yaml`; existing tool allow/deny lists are preserved across
an `enable`. `oc sandbox explain` with no argument is the policy inspector:

```
$ oc sandbox explain
            Sandbox policy
  scope         session  (one container per session)
  enabled       yes
  tools allow   (all tools)
  tools deny    Bash
  host backend  macos_sandbox_exec
config keys: sandbox.scope · sandbox.tools.allow · sandbox.tools.deny
policy file: ~/.opencomputer/<profile>/config.yaml
```

## Configuration

The policy lives under the top-level `sandbox:` key of `config.yaml`:

```yaml
sandbox:
  scope: session
  tools:
    allow: [Read, Bash]   # optional — empty means "all tools"
    deny:  [Bash]         # optional — empty means "none denied"
```

An unrecognised `scope` value fails loudly at config load (a typo should
never silently disable the sandbox).

## Tool allow / deny

When sandboxing is enabled, `sandbox.tools.allow` / `deny` restrict which
tools may run inside the sandbox. The rules (OpenClaw semantics):

- `deny` always wins — a denied tool is blocked even if it is also allowed.
- A non-empty `allow` list makes everything **not** listed implicitly denied.
- An all-empty policy permits every tool.

Matching is by exact tool name; `group:*` shorthands are not in Milestone 1.

## Scope of Milestone 1

Milestone 1 ships the policy object, its persistence, the `oc sandbox`
management CLI, and the scope-key plumbing through `run_sandboxed` (the
Docker backend names its container from the scope key).

Persistent-container **reuse** and lifecycle (`oc sandbox list` / `recreate`
/ prune), the per-call backend **resolver**, and the ephemeral E2B backend
are Milestone 2 — see the parity plan at
`docs/superpowers/specs/2026-05-16-oc-parity-with-hermes-openclaw/`.
