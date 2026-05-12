# OpenComputer — Development Guide

Instructions for AI coding assistants and developers working on OpenComputer.

For session-context (what's been built, where files live, how to run, gotchas) read [CLAUDE.md](CLAUDE.md) first — that's the primary brief. This file holds the dev-vocabulary and the strict boundary rules.

## Core Concepts (plan vocab)

- **Agent loop** — single while loop in `opencomputer/agent/loop.py`: model call → tool dispatch → continue/break.
- **StepOutcome** — dataclass capturing one loop iteration's result (stop reason + assistant message).
- **Three-pillar memory** (per-profile, rooted at `~/.opencomputer/<profile>/`):
  - **Declarative** — `MEMORY.md`, `USER.md`, `SOUL.md`, `DREAMS.md`
  - **Procedural** — `skills/*/SKILL.md`
  - **Episodic** — `sessions.db` (SQLite + FTS5 full-text search)
- **Hooks** — lifecycle event intercepts. **25+ events** defined in `plugin_sdk/hooks.py::HookEvent` (see that enum for the canonical list). Settings-based hooks (in `<profile>/config.yaml`) coexist with plugin-declared ones. Wire protocol is stdout-JSON preferred, exit-code fallback, fail-open on timeout. Details in CLAUDE.md §6.
- **Subagents** — spawn a fresh mini-agent in isolated context via the `delegate` tool. Parent/child lineage tracked in SessionDB; view with `oc sessions tree`.
- **Dynamic injection** — cross-cutting modes (plan, yolo, skill-mode) inject system reminders without scattering `if` checks. Implemented in `opencomputer/agent/injection.py` via `DynamicInjectionProvider`.
- **Plugin SDK** — public contract at `plugin_sdk/` — stable. `opencomputer/**` — internal, can refactor freely.
- **Profile** — a self-contained workspace at `~/.opencomputer/<profile>/`. Switch with `oc -p <name>`. Holds config, credentials, memory, sessions, browser-profile, evolution state — everything.

## Development

```bash
# Setup
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Run
oc chat

# Test
pytest

# Lint (ruff only — no black)
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

After any worktree-add or merge: `pip install -e . --no-cache-dir --no-deps && hash -r` to refresh the editable shim. The `oc` binary shim goes stale otherwise.

## Boundary Rules (strict, test-enforced)

1. **Extensions must only import from `plugin_sdk/*`.** Never from `opencomputer/**`. Enforced by `tests/test_plugin_extension_boundary.py` (frozen-inventory; new violators fail CI).
2. **`plugin_sdk/` must never import from `opencomputer/*`.** Enforced by `tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer`.
3. **Core (`opencomputer/**`) must not import from extensions.** Extensions register via `register(api)` + manifests + hooks.
4. **Never break the SDK without a major version bump.** Third-party plugins depend on it.
5. **Hooks must be fire-and-forget for post-actions.** Never block the main loop. Fail-open on timeout.
6. **Tool names are PascalCase + globally unique.** `ToolRegistry` raises `ValueError` on collision at load.

## Runtime context quirks

**`RuntimeContext.agent_context`** (values `"chat" | "cron" | "flush" | "review"`, default `"chat"`) controls the cron/flush guard in `MemoryBridge.prefetch` and `MemoryBridge.sync_turn`. When the context is `cron` or `flush`, the external memory provider (Honcho) is bypassed so batch jobs don't spin a Docker stack.

Today **no production entry point sets `agent_context` to anything but the default** — the only `RuntimeContext(...)` construction on the production path is `cli.py::_cmd_chat` which passes `plan_mode=plan`. If you add a batch runner (cron job, PreCompact flush, review pass), construct `RuntimeContext(agent_context="cron")` at that entry point — otherwise the guard silently no-ops and every batch turn spins Honcho. The guard is unit-tested against the bridge directly; end-to-end with a real cron caller is NOT exercised.

**`AgentLoop._runtime` aliases the module-shared `DEFAULT_RUNTIME_CONTEXT` at `__init__`.** Writes from methods called BEFORE `run_conversation` leak across instances. `run_conversation` rebinds per call, so production is fine; unit tests that exercise helper methods directly must rebind: `loop._runtime = RuntimeContext()`.

## Reference repos

Cloned at `../sources/` (gitignored):

- `claude-code/` — plugin primitives vocabulary
- `hermes-agent/` — Python patterns, three-pillar memory, channel adapters, BrowserHarness
- `openclaw/` — plugin-first architecture, manifest-first discovery, typed wire protocol
- `kimi-cli/` — dynamic injection, fire-and-forget hooks, deferred MCP loading
- `hermes-workspace/` — Node SSR webui consumed via `oc workspace`

When in doubt, check these for proven patterns before inventing new ones. Extraction notes live in `docs/refs/<repo-name>/`.

## Version

Versioning is date-calver: `YYYY.MM.DD[.postN]`. Source of truth: `pyproject.toml` `version`. Read at runtime via `opencomputer.__version__` (loads from installed metadata).

## Where to look next

- **Session context for fresh sessions** — [CLAUDE.md](CLAUDE.md)
- **Shipped feature history** — `~/.claude/projects/-Users-saksham-Vscode-claude/memory/MEMORY.md` (auto-loaded in Claude sessions)
- **Architecture & contracts** — `docs/sdk-reference.md`, `docs/memory-architecture.md`, `docs/databases.md`, `docs/acp.md`
- **Active specs / design docs** — `docs/superpowers/specs/`
- **Plugin authoring** — `docs/plugin-authors.md`
