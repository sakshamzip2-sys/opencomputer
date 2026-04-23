# OpenComputer — Development Guide

Instructions for AI coding assistants and developers working on OpenComputer.

## Project Structure

```
OpenComputer/
├── pyproject.toml            # package metadata + deps (hatchling)
├── README.md
├── AGENTS.md                 # this file
├── opencomputer/             # core package
│   ├── __init__.py
│   ├── cli.py                # entry point — `opencomputer` command
│   ├── agent/
│   │   ├── loop.py           # core while-loop — run_conversation
│   │   ├── step.py           # StepOutcome dataclass
│   │   ├── injection.py      # DynamicInjectionProvider (plan/yolo modes)
│   │   ├── memory.py         # three-pillar memory manager
│   │   ├── state.py          # SQLite + FTS5 session store
│   │   ├── prompt_builder.py # Jinja2 rendering + slot injection
│   │   ├── prompts/          # Jinja2 templates for system prompts
│   │   ├── middleware.py     # surrogate sanitize, rate limit, retry
│   │   └── config.py         # typed config (dataclasses)
│   ├── tools/                # built-in tool implementations
│   ├── gateway/              # WS daemon + platform dispatch
│   ├── hooks/                # lifecycle hook engine
│   ├── plugins/              # plugin discovery + loader
│   └── skills/               # bundled default skills
├── plugin_sdk/               # PUBLIC plugin contract
├── extensions/               # bundled plugins (Telegram, Anthropic, OpenAI, ...)
├── tests/
└── docs/
```

## Core Concepts (plan vocab)

- **Agent loop** — single while loop, model call → tool dispatch → continue/break
- **StepOutcome** — dataclass capturing one loop iteration's result (stop reason + assistant message)
- **Three-pillar memory:**
  - Declarative — `~/.opencomputer/MEMORY.md`, `USER.md`
  - Procedural — `~/.opencomputer/skills/*/SKILL.md`
  - Episodic — SQLite + FTS5 session search
- **Hooks** — lifecycle event intercepts (PreToolUse, PostToolUse, Stop, SessionStart, SessionEnd, UserPromptSubmit)
- **Subagents** — spawn fresh mini-agent in isolated context via `delegate` tool
- **Dynamic injection** — cross-cutting modes (plan, yolo) inject system reminders without scattering `if` checks
- **Plugin SDK** — public contract at `plugin_sdk/` — stable. `opencomputer/**` — internal, can refactor.

## Development

```bash
# Setup
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Run
opencomputer

# Test
pytest

# Lint / format
ruff check .
black .
```

## Boundary Rules (strict)

- **Extensions must only import from `plugin_sdk/*`.** Never from `opencomputer/**`.
- **Core (`opencomputer/**`) must not import from extensions.** Extensions register via manifests + hooks.
- **Never break the SDK without a major version bump** — third-party plugins depend on it.
- **Hooks must be fire-and-forget for post-actions.** Never block the main loop.

## Runtime context quirks

- **`RuntimeContext.agent_context`** (values `"chat" | "cron" | "flush" | "review"`, default `"chat"`) controls the cron/flush guard in `MemoryBridge.prefetch` and `MemoryBridge.sync_turn` — when the context is `cron` or `flush`, the external memory provider (Honcho) is bypassed so batch jobs don't spin a Docker stack. Today **no production entry point sets `agent_context` to anything but the default** — the only `RuntimeContext(...)` construction in the production path is `cli.py::_cmd_chat` which passes `plan_mode=plan`. If you ever add a batch runner (`opencomputer batch run-all`, cron tab, PreCompact flush job), construct `RuntimeContext(agent_context="cron")` at that entry point — otherwise the guard silently no-ops and every batch turn will spin Honcho. The guard itself is tested against the bridge directly; what's not exercised is an end-to-end path with a real cron caller.

## Reference repos (in parent folder)

- `../claude-code/` — plugin primitives vocabulary
- `../hermes-agent/` — Python patterns, three-pillar memory
- `../openclaw/` — plugin-first architecture, manifest-first discovery
- `../kimi-cli/` — dynamic injection, fire-and-forget hooks, wire protocol

When in doubt, check these for proven patterns before inventing new ones.

## Version

Current: 0.0.1 (pre-alpha — Phase 0 scaffolding)
