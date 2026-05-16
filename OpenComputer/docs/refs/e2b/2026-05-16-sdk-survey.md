# E2B SDK Survey — gating reconnaissance for Milestone 2

**Task:** T2.1 (M2 gating recon). **Date:** 2026-05-16. **Author:** Claude (read-only survey).

This document is the single source of truth that M2's implementation
(`opencomputer/sandbox/e2b.py`, `opencomputer/sandbox/resolver.py`) and the
Tier-3 pre-mortem are built on. It has two halves:

- **Half A** — the external E2B Python SDK (what `e2b.py` will call).
- **Half B** — OC's existing internal sandbox interface (what `e2b.py` must implement).

> Method note: Half A is sourced from the context7 MCP (`/e2b-dev/e2b`,
> `/e2b-dev/code-interpreter`), PyPI JSON metadata, and e2b.dev docs +
> web search (May 2026). Half B is read directly from this worktree's
> source and is fully verified. Where the E2B docs are ambiguous, this is
> flagged inline rather than guessed.

---

## Half A — The E2B Python SDK (EXTERNAL)

### A.0 — Two packages: which one M2 should use

E2B publishes **two** distinct PyPI packages. They are *not* interchangeable:

| Package | Import root | Purpose | Latest version (May 2026) | `requires-python` |
|---|---|---|---|---|
| **`e2b`** | `from e2b import Sandbox, AsyncSandbox` | The **core SDK** — generic sandbox: `commands.run()`, `files.read/write()`, lifecycle. This is the general-purpose "run a shell command in a cloud container" surface. | **2.21.1** | `>=3.10,<4.0` |
| **`e2b-code-interpreter`** | `from e2b_code_interpreter import Sandbox, AsyncSandbox` | A **thin wrapper on top of `e2b`** that adds a Jupyter-style `run_code()` (stateful REPL, returns an `Execution` with rich results / charts). Depends on `e2b>=2.20.3,<3.0.0`. | **2.6.2** | `>=3.10,<4.0` |

**Recommendation for M2: pin `e2b` (the core SDK), not `e2b-code-interpreter`.**

Rationale: OC's `SandboxStrategy` contract is *argv-based* — `run(argv: list[str], ...)` executes a shell command and returns `stdout/stderr/exit_code`. That maps **exactly** onto `e2b`'s `Sandbox.commands.run(cmd, ...)`. `e2b-code-interpreter`'s headline feature (`run_code()` — a stateful Python/JS REPL returning charts and a return value) does **not** match the argv contract and would be unused. `e2b-code-interpreter` *also* re-exports `commands`/`files`, so it would technically work, but it drags in an extra dependency layer for nothing. Use `e2b`.

**pyproject.toml pin (proposed):**

```toml
# E2B ephemeral cloud sandbox backend (M2). Optional extra so the core
# install stays dependency-light; only users who want the e2b backend pull it.
[project.optional-dependencies]
e2b = ["e2b>=2.21,<3"]
```

`e2b` follows semver-shaped tags; `2.x` is the current major. A `>=2.21,<3` floor/ceiling is the safe pin. Note `requires-python` for `e2b` is `>=3.10` — **looser** than OC's own `3.12+` floor, so there is no Python-version conflict. Making it an **optional extra** (rather than a hard core dependency) matches OC's pattern for backend-specific deps and means the `docker`/`linux`/`macos` strategies keep working with zero new install weight.

---

### A.1 — `Sandbox` lifecycle: create, timeout/auto-kill, destroy

**Create (sync):**

```python
from e2b import Sandbox

# Default base template
sandbox = Sandbox.create()

# With options
sandbox = Sandbox.create(
    template="my-custom-template",   # optional; defaults to E2B base image
    timeout=600,                     # SECONDS — sandbox auto-stops after this
    metadata={"project": "my-app"},  # arbitrary string tags
    envs={"API_KEY": "secret123"},   # env vars available inside the sandbox
)

# Context-manager form — auto-kills on exit (preferred for one-shot use):
with Sandbox.create() as sandbox:
    result = sandbox.commands.run('echo hi')
# sandbox is killed here
```

**Create (async)** — see A.6. Same shape: `await AsyncSandbox.create(...)`.

**The auto-kill / timeout parameter:**

- `Sandbox.create(timeout=<seconds>)` sets the sandbox's lifetime. **Default is 300 s (5 minutes)** when omitted. (Sourced from e2b.dev docs + web search; the docs phrase this as "the default timeout of a sandbox — 5 minutes".)
- **Maximum lifetime is plan-gated:** **1 hour on the Base/Hobby tier, 24 hours on Pro.** A `timeout` larger than the plan cap will be rejected/clamped by the API.
- **What happens at timeout — IMPORTANT NUANCE / ambiguity flagged:** E2B's docs describe the timeout as triggering an **automatic *pause*** ("sandboxes can automatically pause to save resources — preserving their full state so you can resume"), *not* necessarily an immediate hard kill. For M2's purposes this is fine — a paused sandbox stops accruing per-second compute cost — but **M2 must not rely on the timeout alone to free resources.** `e2b.py` should always call `kill()` explicitly in a `finally` block. The exact pause-vs-kill behavior at the timeout boundary is **not crisply documented for the core `e2b` SDK** (the pause/resume language appears in higher-level docs); treat "the sandbox is gone after `kill()`" as the only guarantee and do not depend on auto-pause semantics.
- `sandbox.set_timeout(<seconds>)` extends/changes the timeout of a live sandbox at runtime.

**Destroy:**

- `sandbox.kill()` — explicit teardown. Returns when the sandbox is destroyed.
- Context-manager exit calls `kill()` automatically.
- `Sandbox.list()` enumerates running sandboxes; `Sandbox.connect(sandbox_id)` reconnects to one by id (useful if a process is orphaned and needs cleanup). There is also an `e2b sandbox kill <id>` CLI.

**Lifecycle helpers** (relevant to a robust backend): `sandbox.sandbox_id` (str id), `sandbox.is_running()` (bool), `sandbox.get_info()` (template + `started_at`), `sandbox.get_metrics()` (see A.5).

---

### A.2 — Command execution

```python
result = sandbox.commands.run(
    'npm install',
    cwd='/home/user/app',          # working directory
    envs={'NODE_ENV': 'production'},
    user='root',                   # which user to run as
    timeout=120,                   # SECONDS — per-command timeout
    on_stdout=lambda data: ...,    # optional streaming callback
    on_stderr=lambda data: ...,
    background=False,              # True → returns a handle, doesn't block
)
print(result.exit_code, result.stdout, result.stderr)
```

The method is **`sandbox.commands.run(cmd, ...)`** (note: `commands` is a sub-object on the sandbox, and `cmd` is a **shell command string**, not an argv list — see mismatch M-1 in §E). It blocks until the command completes (unless `background=True`) and returns a **`CommandResult`**.

**`CommandResult` fields** (confirmed via E2B SDK docs / web search):

| Field | Type | Notes |
|---|---|---|
| `exit_code` | `int` | `0` on success. |
| `stdout` | `str` | Full standard output. |
| `stderr` | `str` | Full standard error. |
| `error` | `str \| None` | Execution-error message (set when the command itself failed to run). |

> **No `duration` field on `CommandResult`.** Confirmed by web search against the official Python SDK reference — `CommandResult` exposes exactly the four fields above. See §A.5 + mismatch M-2 for the cost/duration implications.
>
> Note also: a **non-zero exit code raises `CommandExitException`** in the E2B SDK rather than returning a `CommandResult` with a non-zero `exit_code`. `e2b.py` must wrap `commands.run()` in `try/except` and synthesize a `SandboxResult` with the non-zero `exit_code` from the exception. (The exception object carries `exit_code`, `stdout`, `stderr`, `error`.) This is a real behavioral divergence from OC's docker/none strategies, which return the result object regardless of exit code — see mismatch M-3.

`background=True` returns a `CommandHandle` (for long-running processes); `sandbox.commands.list()` and `sandbox.commands.kill(pid=...)` manage running processes. M2's argv contract is one-shot, so the foreground (blocking) form is what `e2b.py` will use.

---

### A.3 — Filesystem operations

`files` is a sub-object on the sandbox (parallel to `commands`):

```python
# Write text
sandbox.files.write('/home/user/hello.txt', 'Hello, World!')

# Write many at once
sandbox.files.write_files([
    {'path': '/home/user/a.txt', 'data': 'A'},
    {'path': '/home/user/b.txt', 'data': 'B'},
])

# Read as text (default) or bytes
text  = sandbox.files.read('/home/user/hello.txt')
blob  = sandbox.files.read('/home/user/image.png', format='bytes')

# Other ops
sandbox.files.list('/home/user', depth=3)     # recursive listing
sandbox.files.exists('/home/user/hello.txt')  # bool
sandbox.files.get_info('/home/user/hello.txt')# size, permissions
sandbox.files.make_dir('/home/user/newdir')
sandbox.files.rename('/home/user/a', '/home/user/b')
sandbox.files.remove('/home/user/b')
sandbox.files.watch_dir('/home/user', recursive=True)  # change events
```

This is richer than OC's `SandboxStrategy` contract needs — the contract has no file-transfer method (it executes argv only). Filesystem ops become relevant only if M2 later wants to stage `read_paths`/`write_paths` *content* into the remote sandbox (the local strategies bind-mount; E2B has no host to bind-mount from). See mismatch M-4.

---

### A.4 — Auth

**Mechanism: a single API key, via the `E2B_API_KEY` environment variable.**

```bash
E2B_API_KEY=e2b_***       # the SDK reads this automatically
```

The SDK picks up `E2B_API_KEY` from the process environment with no extra code. The key can also be passed explicitly to the constructor (`Sandbox.create(api_key="e2b_...")` — standard SDK pattern, supported across E2B's SDKs). Keys are issued from the E2B dashboard (https://e2b.dev/dashboard) and are prefixed `e2b_`.

**OC integration note:** `E2B_API_KEY` should be added to OC's env-passthrough / `required_credential_files` machinery and to `oc profile env-template` so it lands in `<profile>/.env` like other provider keys. The `e2b.py` strategy's `is_available()` should return `False` (cleanly, no exception) when `E2B_API_KEY` is unset — mirroring how `DockerStrategy.is_available()` returns `False` when the daemon is unreachable.

---

### A.5 — Pricing, and whether the SDK surfaces duration / cost

**Billing model:** E2B bills **per second of running-sandbox wall-clock time**, split into a CPU component and a RAM component:

- CPU: ~**$0.000028 / second** for the default **2 vCPU** sandbox (≈ $0.10 / vCPU-hour-pair, quoted as **$0.0504 / vCPU-hr**).
- RAM: **$0.0000045 / GiB / second**.
- Free **Hobby** tier: one-time **$100** usage credit, up to 20 concurrent sandboxes, 1-hour max sandbox lifetime.
- **Pro** tier: **$150 / month**, 24-hour max sandbox lifetime, up to 100 concurrent sandboxes (expandable).
- (Figures from e2b.dev/pricing + Northflank's 2026 AI-sandbox pricing comparison, May 2026. Treat as approximate — E2B adjusts rates; M2's cost-guard should read a rate from config, not hard-code these.)

**Does the SDK surface duration or cost in call metadata? — NO, not directly. This is the key finding for M2's cost-guard task.**

- `CommandResult` has **no `duration` field** (confirmed, §A.2).
- There is **no `cost` field** anywhere in the SDK's per-call return values.
- `sandbox.get_metrics()` returns **CPU % / memory-MiB / disk** samples — *resource utilization*, **not** billed duration and **not** a dollar figure.
- `sandbox.get_info()` returns `started_at` (a timestamp).

**Consequence for M2's cost-guard (`e2b.py` must compute cost itself):**
The only reliable way to attribute cost to a sandboxed call is to **measure wall-clock time on the OC side** — exactly what every existing strategy already does via `time.monotonic()` around the call (see Half B: `SandboxResult.duration_seconds`). M2's cost-guard should:

1. Time the sandbox's lifetime (`create` → `kill`) with `time.monotonic()`.
2. Multiply elapsed seconds by a configurable per-second rate (default ≈ the CPU+RAM figures above, surfaced in `cost_guard.json` / config — **not** hard-coded).
3. Optionally cross-check with `started_at` from `get_info()`.

`SandboxResult.duration_seconds` already exists in the OC contract and the E2B strategy will populate it the same way docker.py does — so the cost-guard has a clean hook with **no SDK change needed**, it just needs the rate constant. See mismatch M-2.

---

### A.6 — Async API

**Yes — E2B ships a first-class async API.** `from e2b import AsyncSandbox`. Every method has an `await`-able twin:

```python
import asyncio
from e2b import AsyncSandbox

async def main():
    sandbox = await AsyncSandbox.create(timeout=600)
    result  = await sandbox.commands.run('echo "Hello async!"')
    await sandbox.files.write('/home/user/test.txt', 'content')
    content = await sandbox.files.read('/home/user/test.txt')
    # concurrent commands
    await asyncio.gather(
        sandbox.commands.run('cmd1'),
        sandbox.commands.run('cmd2'),
    )
    await sandbox.kill()

asyncio.run(main())
```

This matters because **OC's `SandboxStrategy.run()` is `async`** (Half B). `e2b.py` should use **`AsyncSandbox`** throughout and `await` the lifecycle/command calls directly — no `asyncio.to_thread` wrapper, no event-loop juggling.

> Cross-loop caution (OC-specific, from `OpenComputer/CLAUDE.md` gotcha #5/#15): `AsyncSandbox` is built on `httpx.AsyncClient`. An `AsyncSandbox` created inside one `asyncio.run()` must not be awaited from a different loop. OC's per-turn chat path does `asyncio.run()` per turn — so `e2b.py` must create **and** kill the sandbox **within a single `run()` invocation** (i.e. inside one `SandboxStrategy.run()` call). The contract's per-call shape (`run(argv)` → `SandboxResult`) makes this natural: create → exec → kill, all inside the one `async def run`. Do **not** cache a long-lived `AsyncSandbox` on the strategy instance across calls.

---

## Half B — OC's existing sandbox interface (INTERNAL — fully verified)

Read end-to-end from this worktree:
`opencomputer/sandbox/{__init__,_common,auto,docker,runner,ssh,none_strategy}.py`
and `plugin_sdk/sandbox.py`.

### B.1 — The contract location

The **public contract** lives in **`plugin_sdk/sandbox.py`** and is re-exported via `plugin_sdk/__init__.py`. Concrete strategies live in `opencomputer/sandbox/` (internal, may evolve). `e2b.py` is a concrete strategy → it lives in `opencomputer/sandbox/e2b.py` and imports the contract from `plugin_sdk.sandbox`.

The ABC is **`SandboxBackend`**, with **`SandboxStrategy` as a module-level alias** (`SandboxStrategy = SandboxBackend` — same type object). Existing strategies write `class XStrategy(SandboxStrategy)`; new code may subclass either name. `e2b.py` should follow the existing pattern: `class E2BStrategy(SandboxStrategy)`.

### B.2 — The `SandboxStrategy` / `SandboxBackend` contract — EXACT interface `e2b.py` must implement

A subclass MUST provide one class attribute and three methods:

```python
class SandboxBackend(abc.ABC):

    name: ClassVar[str]
    # Short id. Existing values: "macos_sandbox_exec", "linux_bwrap",
    # "docker", "ssh", "none". e2b.py should set name = "e2b".

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Cheap, side-effect-free, cached capability check.
        Platform check + binary/credential presence. Heavy probes
        must be cached so the call is effectively constant-time."""

    @abc.abstractmethod
    async def run(
        self,
        argv: list[str],
        *,
        config: SandboxConfig,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        """Execute argv inside the sandbox; return the captured result.
        Raises SandboxUnavailable if the strategy can't run here."""

    @abc.abstractmethod
    def explain(self, argv: list[str], *, config: SandboxConfig) -> list[str]:
        """Return the wrapped command WITHOUT running it (for --dry-run
        / audit). For the 'none' strategy this is just the argv."""
```

**ABC docstring contract — implementations MUST:**

1. Spawn the wrapped command with `asyncio.create_subprocess_exec` — **never blocking `subprocess`** — so the event loop is not blocked.
   - *Adaptation for `e2b.py`:* there is no local subprocess; the intent ("don't block the loop") is satisfied by using **`AsyncSandbox`** and `await`-ing. The cost-guard / pre-mortem should note that `e2b.py` is the first strategy that satisfies clause 1 *in spirit but not literally* — it does network I/O, not a local exec.
2. Strip env vars not in `config.allowed_env_vars` before passing them through. → `e2b.py` builds the `envs=` dict for `commands.run()` / `Sandbox.create()` from `filtered_env(config)` (see B.6).
3. Enforce `config.cpu_seconds_limit` via timeout — kill the process and return a non-zero exit + sentinel stderr on overrun.
4. Set `SandboxResult.strategy_name` to `self.name`.

### B.3 — `SandboxConfig` — the input shape (frozen dataclass, `slots=True`)

Every field, with default, exactly as `e2b.py` will receive it:

| Field | Type | Default | Relevance to `e2b.py` |
|---|---|---|---|
| `strategy` | `SandboxStrategyName` (Literal) | `"auto"` | **Literal must gain `"e2b"`** — see mismatch M-5. |
| `cpu_seconds_limit` | `int` | `60` | Wall-clock cap. Map to E2B `commands.run(timeout=...)` AND/OR `Sandbox.create(timeout=...)`. Enforce with `asyncio.wait_for` too (clause 3). |
| `memory_mb_limit` | `int` | `512` | E2B memory is set by **template**, not a per-call flag. Likely **ignored** by `e2b.py` (like macOS ignores it) — document loudly. See M-6. |
| `network_allowed` | `bool` | `False` | **PROBLEM**: E2B sandboxes are cloud VMs that are *always networked*; there is no per-call "no network" switch. `network_allowed=False` (the **default**!) cannot be honored. See **blocker/mismatch M-7**. |
| `container_persistent` | `bool` | `True` | Docker-only today; E2B sandboxes are ephemeral per-call anyway. `e2b.py` ignores it. |
| `read_paths` | `tuple[str, ...]` | `()` | Local strategies bind-mount these. E2B has no host FS to mount — would need `files.write()` upload. See M-4. Likely ignored in M2 v1. |
| `write_paths` | `tuple[str, ...]` | `()` | Same as `read_paths`; output would need `files.read()` to retrieve. Likely ignored in M2 v1. |
| `allowed_env_vars` | `tuple[str, ...]` | `("PATH","HOME","LANG","LC_ALL")` | Feed through `filtered_env()` → E2B `envs=`. |
| `image` | `str` | `"alpine:latest"` | Docker-only naming. E2B's analog is a **template name**. `"alpine:latest"` is **not a valid E2B template** — `e2b.py` must map/ignore this and use the E2B base template or a config-supplied template id. See M-8. |
| `ssh_host` | `str \| None` | `None` | ssh-only; `e2b.py` ignores. |
| `container_key` | `str \| None` | `None` | Scope-based container reuse (docker-only). E2B could reuse via `Sandbox.connect(id)`, but M2 v1 should keep per-call ephemeral (matches `container_key=None` semantics). |
| `_reserved` | `tuple[str, ...]` | `()` | Reserved; ignore. |

`SandboxConfig` is `frozen=True, slots=True` — immutable; the runner uses `dataclasses.replace()` to derive variants.

### B.4 — `SandboxResult` — the output shape (frozen dataclass, `slots=True`)

`e2b.py`'s `run()` must return exactly this:

```python
@dataclass(frozen=True, slots=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float      # time.monotonic() delta — cost-guard hook
    wrapped_command: list[str]   # the argv "actually executed" (for audit)
    strategy_name: str           # == self.name, i.e. "e2b"
```

Notes for `e2b.py`:
- `duration_seconds` — measure with `time.monotonic()` around the create→exec→kill. **This is the cost-guard's input** (§A.5).
- `wrapped_command` — for docker it's the full `docker run ...` argv. E2B has no local argv; `e2b.py` should put something audit-meaningful here, e.g. `["e2b", "sandbox", "run", "--template", "<tpl>", "--", *argv]` or simply the `argv` prefixed with a marker. `explain()` returns the same list.
- On timeout: return `exit_code = TIMEOUT_EXIT_CODE` (`-1`), `stderr = TIMEOUT_STDERR` (`"[sandbox timeout]"`), `stdout = ""`. **These two sentinels are asserted by tests — keep them exact.** They are in `opencomputer/sandbox/_common.py`.

### B.5 — `SandboxUnavailable`

`class SandboxUnavailable(RuntimeError)` (in `plugin_sdk/sandbox.py`; note `# noqa: N818` — the name intentionally has no `Error` suffix). `e2b.py` raises this from `run()` when the strategy can't run (e.g. `E2B_API_KEY` missing and somehow `is_available()` was bypassed). `is_available()` itself returns `bool`, never raises.

### B.6 — Shared helpers (`opencomputer/sandbox/_common.py`)

Internal — only `opencomputer/sandbox/*` may import it. `e2b.py` should reuse:

- `filtered_env(config, *, extras=None) -> dict[str, str]` — builds the env dict from `config.allowed_env_vars` against `os.environ`, then overlays `extras`. `e2b.py` passes the result as E2B's `envs=`.
- `decode_stream(data) -> str` — best-effort UTF-8 decode (`errors="replace"`). E2B already returns `str` for `stdout`/`stderr`, so this is a defensive no-op for the happy path, but use it for symmetry / `None`-safety.
- `TIMEOUT_STDERR = "[sandbox timeout]"`, `TIMEOUT_EXIT_CODE = -1` — the timeout sentinels (B.4).

### B.7 — How `auto.py` selects a strategy — and how the M2 resolver extends it

`opencomputer/sandbox/auto.py::auto_strategy(config=None)` is the current selector:

```python
def auto_strategy(config: SandboxConfig | None = None) -> SandboxStrategy:
    del config  # accepted but NOT consulted today (reserved)
    sysname = platform.system()
    candidates: list[SandboxStrategy] = []
    if sysname == "Darwin":
        candidates.append(MacOSSandboxExecStrategy())
    elif sysname == "Linux":
        candidates.append(LinuxBwrapStrategy())
    candidates.append(DockerStrategy())          # universal fallback, tried last
    for s in candidates:
        if s.is_available():
            return s
    raise SandboxUnavailable("no sandbox strategy available ...")
```

Key facts the M2 resolver inherits / must respect:

- **Selection = first `is_available()` wins**, in a fixed platform-priority order. macOS/Linux native first, Docker last.
- `auto_strategy` **accepts a `config` argument but currently ignores it** (`del config`) — the docstring explicitly reserves it "to let callers blacklist a strategy". **M2's resolver is the feature that finally consumes `config`.**
- The named-strategy path is **separate**: `runner.py::_named_strategy(name)` is a hard-coded `if/elif` chain (`"none"`, `"macos_sandbox_exec"`, `"linux_bwrap"`, `"docker"`, `"ssh"`). **`e2b.py` must be added here too** — otherwise `SandboxConfig(strategy="e2b")` raises "unknown sandbox strategy".
- `runner.py::run_sandboxed(...)` is the one-call entry point: if `cfg.strategy == "auto"` it calls `auto_strategy`, else `_named_strategy`. Then `strategy.run(argv, config=cfg, stdin=, cwd=)`.

**Wiring checklist for the M2 resolver + `e2b.py`** (so `e2b` is reachable end-to-end):

1. `opencomputer/sandbox/e2b.py` — `class E2BStrategy(SandboxStrategy)` with `name = "e2b"`, implementing `is_available` / `run` / `explain`.
2. `plugin_sdk/sandbox.py` — add `"e2b"` to the `SandboxStrategyName` Literal (M-5). Adding a Literal member is a backwards-compatible additive change per `plugin_sdk/CLAUDE.md` rule 4.
3. `opencomputer/sandbox/runner.py::_named_strategy` — add `elif name == "e2b": s = E2BStrategy()` and include `"e2b"` in the "valid:" error string.
4. `opencomputer/sandbox/__init__.py` — add `E2BStrategy` to the imports + `__all__`.
5. `opencomputer/sandbox/resolver.py` (NEW, ~150 LOC) — the multi-backend resolver. It generalizes `auto_strategy`: it should *consume* `config` (the reserved arg `auto_strategy` ignores) to pick a backend per tool call — e.g. prefer `e2b` when `E2B_API_KEY` is set and the call is flagged ephemeral/untrusted, fall back to the local strategies otherwise. The resolver is the thing that decides "this call goes to the cloud, that one stays local". `auto_strategy` can either delegate to the resolver or stay as the legacy local-only path; M2 should decide and keep one of them authoritative to avoid two selectors drifting.
6. Tests: a strategy test mirroring `tests/` patterns for docker/ssh; `is_available()` gating on `E2B_API_KEY`; the timeout-sentinel assertions.

---

## Summary — answers to the six return questions

**(a) E2B SDK package + version to pin.**
Pin **`e2b`** (the core SDK), **`>=2.21,<3`** (latest release **2.21.1**, May 2026). Do **not** use `e2b-code-interpreter` (latest 2.6.2) — its headline `run_code()` REPL doesn't match OC's argv contract; the core `e2b` package's `commands.run()` does. Add it as an **optional extra** (`[project.optional-dependencies] e2b = [...]`) so the core install stays lean. `e2b` needs Python `>=3.10` — looser than OC's 3.12 floor, no conflict.

**(b) Create / exec / filesystem / destroy API in one paragraph.**
`from e2b import Sandbox` (sync) or `AsyncSandbox` (async — use this, OC's `run()` is async). `Sandbox.create(template=..., timeout=<seconds>, metadata=, envs=)` boots an ephemeral cloud VM (default timeout **300 s**, max **1 h Base / 24 h Pro**). Shell commands: `sandbox.commands.run(cmd, cwd=, envs=, user=, timeout=, background=)` → a `CommandResult(exit_code, stdout, stderr, error)`; **a non-zero exit raises `CommandExitException`** rather than returning a result. Files: `sandbox.files.write(path, data)` / `sandbox.files.read(path, format=)` plus `list/exists/remove/make_dir/rename`. Teardown: `sandbox.kill()` (or context-manager exit). Auth: `E2B_API_KEY` env var, read automatically.

**(c) Does E2B expose per-second duration / cost in metadata? — NO.**
`CommandResult` has **no `duration` field** and there is **no `cost` field** anywhere. `get_metrics()` gives CPU%/RAM/disk *utilization*, not billed time or dollars; `get_info()` gives only `started_at`. E2B bills **per second** (~$0.000028/s CPU for 2 vCPU + $0.0000045/GiB/s RAM) but the SDK never returns that number. **M2's cost-guard must compute cost itself** = OC-side `time.monotonic()` wall-clock × a configurable per-second rate. Good news: `SandboxResult.duration_seconds` already exists in the OC contract and `e2b.py` populates it the same way `docker.py` does — the cost-guard's hook needs **no SDK change**, only a rate constant in config.

**(d) The exact OC `SandboxStrategy` interface `e2b.py` must implement.**
Subclass `SandboxStrategy` (alias of `SandboxBackend`, in `plugin_sdk/sandbox.py`). Provide: class attr **`name: ClassVar[str] = "e2b"`**; **`is_available(self) -> bool`** (cheap, cached, never raises — gate on `E2B_API_KEY` presence); **`async run(self, argv, *, config: SandboxConfig, stdin=None, cwd=None) -> SandboxResult`**; **`explain(self, argv, *, config) -> list[str]`** (wrapped command, no execution). `run()` must return a frozen `SandboxResult(exit_code, stdout, stderr, duration_seconds, wrapped_command, strategy_name)`; on timeout return `exit_code=-1`, `stderr="[sandbox timeout]"` (the exact sentinels from `_common.py`, asserted by tests). Reuse `filtered_env()` / `decode_stream()` from `opencomputer/sandbox/_common.py`. Match `docker.py`'s structure as the closest template.

**(e) Mismatches between PART-2's M2 plan assumptions and reality.**

- **M-1 — argv vs command-string.** OC's contract passes `argv: list[str]`; E2B's `commands.run()` takes a **single shell-command string**. `e2b.py` must `shlex.join(argv)` before calling (exactly as `ssh.py` already does for its remote command). Minor, but must not be missed.
- **M-2 — no cost/duration in SDK metadata.** (See (c).) The plan's cost-guard task cannot read a cost off the E2B response — it must time the call OC-side. Not a blocker; the contract already carries `duration_seconds`.
- **M-3 — non-zero exit raises.** E2B raises `CommandExitException` on non-zero exit; OC's docker/none strategies *return* a result with the non-zero code. `e2b.py` must `try/except CommandExitException` and synthesize the `SandboxResult` from the exception's fields. A naive port that assumes `commands.run()` always returns will silently turn every failing command into an unhandled exception.
- **M-4 — no host filesystem to bind-mount.** `read_paths`/`write_paths` are bind-mounts for the local strategies. E2B is a remote VM with no host FS; honoring them would require `files.write()` upload / `files.read()` download. M2 v1 should **explicitly document `read_paths`/`write_paths` as ignored by `e2b.py`** (or implement upload/download as a follow-up) — silently dropping them is a correctness trap.
- **M-5 — `SandboxStrategyName` Literal must gain `"e2b"`.** Without it, `SandboxConfig(strategy="e2b")` is a type error and `_named_strategy` rejects it. Additive Literal change — backwards-compatible per `plugin_sdk/CLAUDE.md`.
- **M-6 — `memory_mb_limit` not honorable per-call.** E2B sets RAM via the **template**, not a per-call flag. `e2b.py` will ignore `memory_mb_limit` (like `macos.py` does for `sandbox-exec`). Document loudly.
- **M-7 — `network_allowed=False` cannot be honored — and it's the DEFAULT.** E2B sandboxes are cloud VMs with network access; there is no per-call network-off switch. The OC contract **defaults `network_allowed` to `False`**, so the *default* `SandboxConfig` asks for something `e2b.py` physically cannot provide. M2 must decide the policy: (i) `e2b.py` accepts the call but logs a loud WARNING that network containment was requested and not enforced, or (ii) `e2b.py` *refuses* (`SandboxUnavailable`) when `network_allowed=False` so the resolver falls back to a local strategy. Option (ii) is safer-by-default; either way this **must be a conscious decision in the M2 plan**, not an oversight. (E2B's docs do not surface a sandbox-level network-deny toggle in the SDK; flagged as the one place reality most diverges from the plan.)
- **M-8 — `image="alpine:latest"` is meaningless to E2B.** E2B uses **template ids**, not Docker image refs; `"alpine:latest"` is not a valid template. `e2b.py` must ignore `config.image` and use E2B's base template (or a separate config-supplied `e2b_template` id). The plan's "~200 LOC, same interface as docker.py" framing should account for this — the `image` field does *not* carry over.
- **M-9 — clause 1 of the ABC ("`asyncio.create_subprocess_exec`, never blocking `subprocess`") is satisfied only in spirit.** `e2b.py` does network I/O via `AsyncSandbox`, not a local exec. The intent (don't block the loop) holds, but a literal reading of the contract docstring doesn't. Worth a one-line note in `e2b.py`'s module docstring so a future reader doesn't "fix" it.

**(f) Blockers — what needs an E2B account / API key before M2 can be built or tested.**

- **BLOCKER-1 — an E2B account + API key is required to *run or integration-test* `e2b.py`.** `Sandbox.create()` makes a live API call to E2B's cloud; there is no local/offline E2B. **Implication: M2 can be fully *written* and *unit-tested with mocks* (mock `AsyncSandbox` exactly as OC mocks MCP sessions and other SDKs), but any *live* integration test needs `E2B_API_KEY` set.** Mirror the docker/ssh test pattern — those skip when the backend is unavailable. The E2B Hobby tier is free (one-time $100 credit, no card needed to start) so obtaining a key is low-friction, but it **is** a prerequisite for the live-test slice and should be called out in the M2 plan as a human-attended setup step.
- **BLOCKER-2 — decide the `network_allowed=False` policy before coding (M-7).** This is a design decision, not just code: because the default `SandboxConfig` requests network-deny and E2B can't honor it, the resolver/`e2b.py` behavior for that case must be specified up front. If unspecified, `e2b.py` will either silently violate the sandbox's stated policy or unexpectedly refuse the default config.
- **BLOCKER-3 (minor) — pin the cost rate somewhere.** The cost-guard needs a per-second $ rate; E2B doesn't supply it programmatically and the published rates drift. M2 must add a config knob (e.g. in `cost_guard.json`) with a sane default rather than hard-coding — decide the location before the cost-guard task starts.
- **No blocker on Python version, dependency conflict, or async support** — `e2b` is `>=3.10` (OC is 3.12+, compatible), and a first-class `AsyncSandbox` exists, so OC's async agent loop is well served.

---

## Appendix — sources

- context7 MCP: `/e2b-dev/e2b` (core SDK), `/e2b-dev/code-interpreter` (wrapper SDK) — lifecycle, `commands.run`, `files.*`, `AsyncSandbox`, `set_timeout`, `connect`, `get_metrics`.
- PyPI JSON metadata: `pypi.org/pypi/e2b/json` (v2.21.1, `requires-python >=3.10,<4.0`); `pypi.org/pypi/e2b-code-interpreter/json` (v2.6.2, depends on `e2b>=2.20.3,<3.0.0`).
- e2b.dev docs + web search (May 2026): default timeout 300 s; max lifetime 1 h Base / 24 h Pro; per-second billing (~$0.000028/s CPU @ 2 vCPU + $0.0000045/GiB/s RAM); Hobby ($100 credit) / Pro ($150/mo) tiers; `CommandResult` field set (`exit_code`/`stdout`/`stderr`/`error`, no `duration`); `E2B_API_KEY` auth. Northflank "AI Sandbox pricing comparison (2026)" for rate cross-check.
- OC internal (fully verified, this worktree): `plugin_sdk/sandbox.py`, `opencomputer/sandbox/{__init__,_common,auto,docker,runner,ssh,none_strategy}.py`.

### Items E2B's docs left ambiguous (not invented here)

- **Exact behavior at the `timeout` boundary** for the core `e2b` SDK — docs describe an automatic *pause* (state-preserving) in higher-level material but do not crisply state pause-vs-kill for a plain `Sandbox.create(timeout=...)`. M2 should treat `kill()` as the only teardown guarantee and not depend on auto-pause.
- **Per-call network isolation** — no sandbox-level network-deny toggle is documented in the SDK surface. Treated as "not available" (M-7); if E2B has an undocumented/template-level mechanism, M2 should verify against a live account before relying on it.
