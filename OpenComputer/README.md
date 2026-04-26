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

Requires **Python 3.13+**.

**One-line install** (macOS / Linux / Termux):

```bash
curl -fsSL https://raw.githubusercontent.com/sakshamzip2-sys/opencomputer/main/scripts/install.sh | bash
```

The installer auto-detects pipx / pip --user / venv and falls back gracefully on PEP 668 ("externally managed") Python distributions. Pass `--dry-run` to preview, `--dev` to install from a local clone in editable mode, `--use-pipx` to force pipx.

**Manual install:**

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

OpenComputer ships with a `coding-harness` plugin that turns the agent into a
coding agent. v2 adds Kimi-style content-hashed checkpoints + rewind,
Claude-Code-style hook events, permission scopes, in-chat slash commands, and
auto-activating skills.

### How it works (the mental model)

**You don't invoke plugins directly.** You chat with the agent; the agent
chooses tools the harness has registered. Your job is to describe a task —
"fix this bug", "add a test for X", "refactor this function" — and the agent
picks the right tool (`Edit`, `MultiEdit`, `RunTests`, …) from the harness's
capability set. Slash commands are the one exception: `/plan`, `/undo`, etc.
execute directly and deterministically.

List what's installed with `opencomputer plugins`.

### Tools the harness adds

| Tool | What it does |
|---|---|
| `Edit`, `MultiEdit` | Precise string-replace edits. Fail-closed if target isn't unique. |
| `TodoWrite` | Create / update a harness-managed skill in-session. |
| `StartProcess` / `CheckOutput` / `KillProcess` | Run long-lived processes (dev servers, watchers) and poll them asynchronously. |
| `Rewind` | Restore files to a previous checkpoint (`steps=N`, `checkpoint_id=...`, or `list_checkpoints=true`). |
| `Diff` | Unified diff between the working tree and the most recent checkpoint. |
| `RunTests` | Auto-detect pytest / vitest / jest / cargo / go; run and surface results. |

### Modes

Every mode is a `DynamicInjectionProvider` that conditionally appends guidance
to the system prompt. Plan mode also registers a PreToolUse hook that
hard-blocks `Edit` / `Write` / `Bash` — belt + suspenders.

```bash
opencomputer chat --plan       # describe what you'd do; destructive tools refused
```

```
/plan              # toggle plan mode on mid-session
/plan-off          # turn it off
/accept-edits      # auto-accept minor edits (use /undo to revert)
/accept-edits off  # explicit off
```

Subagents spawned via `delegate` inherit the parent's mode flags through
`RuntimeContext`.

### Checkpoints + Rewind

The harness snapshots every tracked file **before** each destructive tool
call via a PreToolUse hook. Snapshots are content-hashed, stored at
`~/.opencomputer/harness/<session_id>/rewind/<checkpoint_id>/`, and shielded
from `Ctrl-C` (can't corrupt mid-save).

```
/checkpoint before-refactor   # manual named snapshot
/undo                         # rewind the last checkpoint
/undo 3                       # rewind 3 checkpoints back
/diff                         # unified diff vs latest checkpoint
/diff <checkpoint_id>         # diff against a specific snapshot
```

Subagents get their own isolated checkpoint store keyed on `subagent_id`, so
a reviewer subagent's edits never pollute your `/undo` chain.

### Permission scopes

A scope-check hook refuses destructive tool calls against paths outside your
workspace (`/etc/**`, `/sys/**`, `/var/log/**`, …) and dangerous Bash
commands (`rm -rf /`, fork bombs, `dd if=*`, …). Defaults live in
`extensions/coding-harness/permissions/default_scopes.py`.

### Skill auto-activation

Bundled skills (`extensions/coding-harness/skills/<name>/SKILL.md`) carry YAML
frontmatter with a `description:` that's matched against your last message.
If ≥ 2 tokens overlap, the full SKILL.md body is injected into this turn's
system prompt — no manual invocation needed.

| Skill | Triggered by |
|---|---|
| `code-reviewer` | "review this PR", "code review", "check my diff" |
| `test-runner`   | "run the tests", "pytest", "verify the build" |
| `refactorer`    | "refactor", "clean up", "extract a function" |

Add your own by dropping a `SKILL.md` in `skills/<your-name>/`.

### Remove the harness

Remove or rename `extensions/coding-harness/` to opt out. The core agent
stays fully functional — it's a pure chat agent without the harness. Plugins
are additive.

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

### Deeper memory — Honcho is now the default overlay

A `MemoryProvider` plugin interface layers deeper cross-session understanding on top of the always-on baseline (MEMORY.md + USER.md + SQLite FTS5). **As of Phase 12b1, [Honcho](https://github.com/plastic-labs/honcho) is the default provider when Docker is available** — the setup wizard detects Docker, pulls the image, and brings up the 3-container stack (api + postgres+pgvector + redis + deriver) automatically. No prompt, no opt-in toggle.

When Docker is **not** installed, the wizard prints a one-line notice with the Docker install URL and persists `provider=""` so subsequent runs don't retry. Baseline memory keeps working — you just don't get the semantic/dialectic overlay.

```bash
opencomputer memory doctor      # 5-layer health report: baseline / episodic / docker / honcho / provider
opencomputer memory setup       # bring the Honcho stack up manually (idempotent)
opencomputer memory status      # just the Honcho stack (compose ps)
opencomputer memory reset       # blow away the stack + data
```

The agent loop passes `RuntimeContext.agent_context` through to the provider. Context `"chat"` (default) injects Honcho's recall into the per-turn system block (the frozen snapshot is NOT modified, preserving the Anthropic prefix cache) and calls `sync_turn` after each response. Context `"cron"` or `"flush"` short-circuits both — batch jobs don't spin the external stack.

Alternative providers (Mem0, Cognee, etc.) can be selected by editing `~/.opencomputer/config.yaml` field `memory.provider`. Only one provider is active at a time. See `docs/plugin-authors.md` when present.

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
- `single_instance: true` (for plugins owning a bot token, etc.) is enforced at load time via an atomic PID lock at `~/.opencomputer/.locks/<plugin-id>.lock`. A second profile attempting to load the same plugin raises `SingleInstanceError`; stale locks from crashed processes are detected via `os.kill(pid, 0)` and atomically stolen.

### Known limitations

These are current edge cases. Each has a planned fix; the workaround below is the officially-supported approach until that fix ships.

- **Single-instance channels across profiles.** Bundled channel plugins (Telegram, Discord) hold a single bot token each; the Bot APIs don't support multiple pollers on one token. Set `single_instance: true` in the plugin manifest (Telegram/Discord already do) and core enforces the constraint with an atomic PID lock — the second profile that tries to load the same plugin fails fast with `SingleInstanceError`, and `PluginRegistry.load_all` logs a WARNING and continues loading the rest of the registry. Crashed-process locks are detected and stolen atomically on the next load attempt.

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
