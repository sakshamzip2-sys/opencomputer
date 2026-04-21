# OpenComputer

Personal AI agent framework — plugin-first, self-improving, multi-channel.

A synthesis of the best ideas from [claude-code](https://github.com/anthropics/claude-code), [hermes-agent](https://github.com/NousResearch/hermes-agent), [openclaw](https://github.com/openclaw/openclaw), and [kimi-cli](https://github.com/MoonshotAI/kimi-cli).

## Status

Pre-alpha. See `docs/` and the plan file for the roadmap.

## What it does

- Chat agent with tool calling (Read, Write, Bash, Grep, Glob, etc.)
- Persistent three-pillar memory (declarative MEMORY.md + procedural skills + episodic SQLite session search with FTS5)
- Self-improvement loop — the agent autonomously saves new skills after complex tasks
- Plugin-first architecture — channels, providers, tools are all plugins with a strict SDK boundary
- Gateway mode — run as a daemon and chat via Telegram/Discord/Slack
- Dynamic injection providers — plan/yolo/custom modes without scattering `if` checks
- Fire-and-forget hooks — slow external hooks never block the loop

## Install (local development)

```bash
cd OpenComputer
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Quickstart

```bash
opencomputer                # interactive CLI
opencomputer --help         # commands
```

## License

MIT
