# OpenComputer

A personal AI agent framework — plugin-first, self-improving, multi-channel.

A synthesis of the best ideas from [Claude Code](https://github.com/anthropics/claude-code), [Hermes Agent](https://github.com/NousResearch/hermes-agent), [OpenClaw](https://github.com/openclaw/openclaw), and [Kimi CLI](https://github.com/MoonshotAI/kimi-cli).

## What it does

- **Chat agent** with tool calling (file ops, bash, grep, glob, subagents, skills).
- **Three-pillar persistent memory:** declarative (MEMORY.md), procedural (skills/), episodic (SQLite + FTS5 full-text search).
- **Self-improvement loop:** the agent saves complex workflows as skills that auto-activate next time.
- **Strict plugin SDK boundary:** third-party plugins never import core internals, so the core can evolve without breaking plugins.
- **Multi-channel gateway:** run as a daemon; chat via Telegram and Discord today, Slack coming.
- **Multiple providers:** Anthropic (native + proxy-compatible), OpenAI, any OpenAI-compatible endpoint (OpenRouter, Ollama, etc.).
- **MCP integration:** plug in any [Model Context Protocol](https://modelcontextprotocol.io) server — its tools become native tools.

## Status

Pre-alpha (0.1.0). Core architecture stable. 114 tests passing. Adding features incrementally.

## Install

Requires **Python 3.12+**.

```bash
pip install opencomputer
```

### For development

```bash
git clone https://github.com/sakshamzip2-sys/opencomputer.git
cd opencomputer
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Quickstart

```bash
# 1. Run the setup wizard — picks provider, saves config
opencomputer setup

# 2. Export your API key (setup tells you which)
export ANTHROPIC_API_KEY=sk-ant-...
# or: export OPENAI_API_KEY=sk-...

# 3. Verify the install
opencomputer doctor

# 4. Chat
opencomputer
```

## Commands

```bash
opencomputer                 # start a chat session (alias for `chat`)
opencomputer chat            # interactive REPL with tools
opencomputer gateway         # run the daemon — listens on configured channels
opencomputer search QUERY    # full-text search past conversations
opencomputer sessions        # list recent sessions
opencomputer skills          # list available skills (bundled + user)
opencomputer plugins         # list installed plugins
opencomputer setup           # first-run wizard
opencomputer doctor          # diagnose config/env issues
opencomputer config show     # print effective config
opencomputer config get KEY  # read one config value (e.g. model.provider)
opencomputer config set KEY VALUE
```

## Coding mode

OpenComputer ships with a `coding-harness` plugin that adds Claude-Code-style
coding tools (Edit, MultiEdit, TodoWrite, background process management) plus
a formal "plan mode" that refuses destructive tools while you review the plan.

```bash
# Normal mode — Edit/Write/Bash work, agent can modify files directly
opencomputer

# Plan mode — agent describes what it would do, Edit/Write/Bash are refused
# Useful for big refactors where you want to review before committing
opencomputer chat --plan

# Disable automatic context compaction (debugging long sessions)
opencomputer chat --no-compact
```

In plan mode, plan-mode guidance is injected into the system prompt AND a
PreToolUse hook hard-blocks destructive tools — belt + suspenders. Subagents
spawned via the `delegate` tool inherit plan mode automatically.

Remove the coding harness any time by removing or renaming
`extensions/coding-harness/`. The core agent stays fully functional.

## Memory

OpenComputer remembers what matters across sessions. Three pillars, all on by default, zero API keys needed:

### Declarative — `~/.opencomputer/MEMORY.md`

Agent observations, learned conventions, project facts. The agent curates this via the `Memory` tool (`add`/`replace`/`remove`/`read`). You can also hand-edit it with any text editor.

### User profile — `~/.opencomputer/USER.md`

Your preferences: communication style, workflow habits, expectations. Kept separate from MEMORY.md so agent-learned observations don't muddle your own preferences.

### Episodic — SQLite + FTS5

Every message is indexed. The agent searches prior conversations via the `SessionSearch` tool, and you can too from the CLI.

### CLI commands

```bash
opencomputer memory show               # print MEMORY.md
opencomputer memory show --user        # print USER.md
opencomputer memory edit [--user]      # open in $EDITOR
opencomputer memory search "kafka"     # FTS5 search across all sessions
opencomputer memory stats              # chars used / limits / backup age
opencomputer memory prune              # clear MEMORY.md (keeps .bak)
opencomputer memory restore            # promote .bak back to live
```

### Safety

Every write is atomic (temp file + `os.replace`) and locked (`fcntl` on Unix, `msvcrt` on Windows). Every mutation backs up the current state to `<file>.bak` first. Character limits (4000 for MEMORY.md, 2000 for USER.md by default) prevent unbounded growth; over-limit writes return an error with a hint.

### Plugging in deeper memory backends

An optional `MemoryProvider` plugin interface lets you add Honcho-style user modeling, Mem0-style fact extraction, or Cognee-style knowledge graphs as overlays on top of the baseline. Only one provider is active at a time; the baseline above always runs. See `docs/plugin-authors.md` when present.

## Profiles

One user, many personas. A profile is a separate data dir with its own memory, config, and plugin selection. Switch with `-p`:

```bash
opencomputer                     # default profile (~/.opencomputer/)
opencomputer -p coder            # ~/.opencomputer/profiles/coder/
opencomputer -p stocks           # ~/.opencomputer/profiles/stocks/
```

Each profile has its **own** `MEMORY.md`, `USER.md`, `config.yaml`, `sessions.db`, `skills/`, and `plugins/`. The agent cannot see across profiles.

### Managing profiles

```bash
opencomputer profile list                        # table of all profiles + active marker
opencomputer profile create coder                # creates ~/.opencomputer/profiles/coder/
opencomputer profile create coder2 --clone-from coder          # copy config from coder
opencomputer profile create staging --clone-from coder --clone-all  # full copy
opencomputer profile use coder                   # set sticky default (no -p needed after)
opencomputer profile rename coder coder-main     # move directory + update sticky
opencomputer profile delete staging --yes        # remove
opencomputer profile path [<name>]               # print the filesystem path
```

The sticky active profile lives in `~/.opencomputer/active_profile`. `-p <name>` always overrides the sticky for one invocation.

### Presets and workspace overlays (plugin activation)

Each profile's `profile.yaml` controls which plugins to load. Two shapes:

```yaml
# Option A: reference a named preset
preset: coding

# Option B: explicit list (mutually exclusive with preset)
plugins:
  enabled: [anthropic-provider, coding-harness]
```

Named presets live in `~/.opencomputer/presets/<name>.yaml`:

```bash
opencomputer preset create coding --plugins anthropic-provider,coding-harness,dev-tools
opencomputer preset list
opencomputer preset edit coding
opencomputer preset show coding
opencomputer preset where coding
```

Per-project overrides via `.opencomputer/config.yaml` walked up from CWD:

```yaml
# ./my-project/.opencomputer/config.yaml
preset: coding
plugins:
  additional: [extra-tool]   # unions into the preset
```

The walk stops before `$HOME/.opencomputer/config.yaml` (that's the agent's main config, not an overlay).

### Plugin install / uninstall

Custom plugins can live in two places:

```bash
# Profile-local (default) — only this profile sees it
opencomputer plugin install ./my-tool                       # → <active profile>/plugins/
opencomputer plugin install ./my-tool --profile coder       # → explicit profile

# Global — all profiles see it
opencomputer plugin install ./my-tool --global              # → ~/.opencomputer/plugins/

opencomputer plugin uninstall my-tool --yes
opencomputer plugin where my-tool                            # print filesystem path
```

Discovery order: **profile-local → global → bundled**. Profile-local shadows global shadows bundled on id collision.

Plugins can declare compatibility in their manifest:

```json
{
  "id": "coding-harness",
  "profiles": ["coder", "default"],
  "single_instance": false
}
```

- `profiles: ["*"]` (or omitted) means "any profile" — default.
- `profiles: ["coder"]` skips the plugin in any profile not named "coder".
- `single_instance: true` (for plugins owning a bot token, etc.) is honoured by a future lock (14.F).

### Known limitations

These are current edge cases. Each has a planned fix; the workaround below is the officially-supported approach until that fix ships.

- **Single-instance channels across profiles.** Bundled channel plugins (Telegram, Discord) hold a single bot token each. Running the same channel concurrently in two profiles will cause duplicate message delivery or message loss — the Bot APIs don't support multiple pollers on one token. **Workaround:** pick ONE profile as the "telegram home" and run `opencomputer gateway` only from it; in the other profile's `profile.yaml`, exclude the channel plugin via `plugins.enabled: [...]` (omit `telegram`). Planned fix: 14.F single-instance lock.

- **Cloud-synced `~/.opencomputer/`.** SQLite's WAL mode does not compose with file-sync tools (Dropbox, iCloud Drive, Syncthing, OneDrive). If your home directory syncs, **add `~/.opencomputer/` (or at minimum `~/.opencomputer/sessions.db*`) to your sync tool's ignore list** or you will silently corrupt your session history. MEMORY.md and USER.md are safe to sync; `sessions.db*` are not. Planned fix: 14.I automatic sync-ignore marker seeding.

- **Honcho host key per profile.** Fixed in 14.J — each profile now gets its own Honcho AI peer model (`opencomputer.<profile>`). The default profile uses the bare `opencomputer` host key.

## Messaging channels

### Telegram

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → get a token
2. `export TELEGRAM_BOT_TOKEN=123:ABC...`
3. `opencomputer gateway` — the bundled Telegram plugin auto-connects
4. DM your bot on Telegram

## MCP servers

Plug any MCP server into OpenComputer. Edit `~/.opencomputer/config.yaml`:

```yaml
mcp:
  servers:
    - name: my-server
      transport: stdio
      command: python3
      args:
        - /path/to/mcp_server.py
      enabled: true
```

The server's tools become available to the agent on next run (namespaced `my-server__tool_name`).

## Architecture

```
opencomputer/   (core — agent loop, state, memory, tools, hooks, gateway, plugin discovery)
plugin_sdk/     (public contract — stable types plugins import)
extensions/     (bundled plugins — telegram, discord, anthropic-provider, openai-provider, coding-harness)
```

**Key design rule:** extensions import only from `plugin_sdk/*`, never from `opencomputer/*`. The core can be refactored freely without breaking plugins.

## Writing a plugin

Plugins are separate folders with a manifest and an entry module. Minimal channel plugin:

```
extensions/my-channel/
├── plugin.json           # { "id": "my-channel", "version": "0.1.0", "entry": "plugin", "kind": "channel" }
├── plugin.py             # exports register(api) — registers the adapter
└── adapter.py            # your BaseChannelAdapter subclass
```

See `extensions/telegram/` for a working reference.

## License

MIT — see `LICENSE.md`.

## Credits

Architectural ideas synthesized from:
- **Claude Code** — plugin primitives vocabulary (commands/skills/agents/hooks/MCP), lifecycle events
- **Hermes Agent** — Python core patterns, three-pillar memory, agent loop shape
- **OpenClaw** — plugin-first architecture, manifest-first discovery, strict SDK boundary
- **Kimi CLI** — dynamic injection, fire-and-forget hooks, deferred MCP, wire-protocol UI decoupling
