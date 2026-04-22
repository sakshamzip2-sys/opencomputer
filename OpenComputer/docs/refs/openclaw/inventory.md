# Openclaw inventory (for Phase 11)

Source repo: `/Users/saksham/Vscode/claude/sources/openclaw/`
Walked: `src/tools/`, `src/plugins/`, `src/plugin-sdk/`, `src/mcp/`, `src/channels/`, `src/connectors/`, `src/gateway/`, `src/agents/`, `extensions/`.
Date: 2026-04-22.

Openclaw is TypeScript — we're using its **API shapes** as inspiration for Python equivalents, not literal ports. RxJS-streaming patterns and NestJS-DI containers are skipped (the underlying concept may map, the implementation does not).

| Item | Kind | OC equivalent | Value if ported | Cost | Verdict | Destination |
|---|---|---|---|---|---|---|
| **Agent / Tool framework** | | | | | | |
| cron-tool (scheduled agent execution) | tool | covered by hermes cron port | high | L | merge | core |
| sessions-spawn-tool / sessions-send-tool | tool | Delegate covers single spawn; multi-agent dispatch missing | high | M | port | core |
| gateway-tool (Gateway client integration) | tool | partial — gateway exists | high | L | already-have-partial | core |
| nodes-tool (Canvas integration) | tool | missing | med | M | port | new:extensions/canvas |
| **Media tools** | | | | | | |
| image-generate-tool / image-tool | tool | missing | high | M | port | new:extensions/media-tools |
| pdf-tool (PDF read) | tool | covered by Read (PDF mode) | — | — | already-have | core |
| tts-tool | tool | missing | med | S | port | new:extensions/media-tools |
| music-generate-tool / video-generate-tool | tool | missing | med | M each | skip | n/a (RxJS-heavy) |
| **Web tools** | | | | | | |
| web-fetch / web-search | tool | already-have | — | — | already-have | core |
| web-fetch-visibility (SSRF + auth guards) | tool | missing | high | S | port | core |
| web-search-provider-* (multi-provider dispatch) | tool | partial — single DDG provider | high | S | port | core |
| **Message & session tools** | | | | | | |
| message-tool (channel message dispatch) | tool | partial — channel adapters call send() directly | high | M | port | core |
| sessions-list-tool / sessions-history-tool | tool | partial — `opencomputer sessions` exists, no tool | high | S | port | core |
| session-status-tool | tool | missing | med | S | port | core |
| **Channel adapters (20+)** | | | | | | |
| discord / telegram | channel | already-have | — | — | already-have | core |
| slack | channel | missing — covered by hermes slack port | high | M | port | new:extensions/slack |
| whatsapp / signal / matrix / line / irc | channel | missing | high | M each | port-later | new:extensions/channels (Phase 11e) |
| msteams / googlechat / mattermost | channel | missing | high | M each | port-later | new:extensions/channels (Phase 11e) |
| imessage / bluebubbles | channel | missing | med | M | skip | n/a (macOS-specific) |
| synology-chat / nextcloud-talk | channel | missing | low | M | skip | n/a |
| twitch / voice-call / phone-control / zalo | channel | missing | low | L | skip | n/a |
| **Provider plugins (14)** | | | | | | |
| anthropic / openai | provider | already-have | — | — | already-have | core |
| google / azure | provider | missing | high | M | port-later | new:extensions/providers (Phase 11e) |
| ollama / lmstudio | provider | missing | high | S | port | new:extensions/local-providers |
| openrouter / together | provider | missing | med | S | port | new:extensions/providers |
| groq / mistral / xai / deepseek / qwen / moonshot | provider | missing | med | M each | port-later | new:extensions/providers (Phase 11e) |
| **Search providers** | | | | | | |
| google / duckduckgo / brave (bundled) | search | partial — DDG only | high | S | port | core (Phase 11c MCP path or dev-tools) |
| tavily / exa / firecrawl | search | missing | med | M | port | new:extensions/search-tools |
| searxng (self-hosted) | search | missing | low | S | skip | n/a |
| **Knowledge / memory** | | | | | | |
| memory-core (episodic memory) | plugin | covered by Phase 11d | high | L | port (via 11d) | core |
| memory-lancedb (vector DB) | plugin | missing | high | M | port | new:extensions/memory-vector |
| memory-wiki (wiki/docs memory) | plugin | missing | high | M | port | new:extensions/memory-wiki |
| **Media understanding** | | | | | | |
| image-generation-core / media-understanding-core | plugin | missing | high | M | port | new:extensions/media-tools |
| speech-core / talk-voice | plugin | missing | high | L | port-later | new:extensions/media-tools (Phase 11e) |
| **Execution / sandbox** | | | | | | |
| fal (serverless ML) | plugin | missing | med | S | port | new:extensions/dev-tools |
| openshell (CLI execution) | plugin | covered by Bash with security risk | — | — | skip | n/a |
| comfy / runway (specialty media gen) | plugin | missing | low | M | skip | n/a |
| **Developer tools** | | | | | | |
| diffs (git diff understanding) | plugin | missing | high | S | port | new:extensions/dev-tools |
| webhook / browser (Playwright) | plugin | missing | high | M | port | new:extensions/dev-tools |
| **Diagnostics / observability** | | | | | | |
| diagnostics-otel (OpenTelemetry) | plugin | missing | med | M | skip | n/a (ops, not user-facing) |
| qa-lab / qa-matrix / qa-channel | plugin | missing | med | M | skip | n/a |
| **MCP integration patterns** | | | | | | |
| channel-bridge (MCP server) | MCP | partial | high | S | already-have | core |
| plugin-tools-serve (MCP tools export) | MCP | missing | high | S | port | core (Phase 11c) |
| channel-server (MCP channel proto) | MCP | missing | high | S | port | core (Phase 11c) |
| **Plugin SDK / manifest** | | | | | | |
| bundle-manifest / bundled-plugin-metadata | subsystem | already-have | — | — | already-have | core |
| bundled-capability-runtime | subsystem | already-have | — | — | already-have | core |
| plugin-sdk (public contract) | subsystem | already-have | — | — | already-have | core |
| **Gateway protocol** | | | | | | |
| typed Gateway protocol (per-domain schema files) | subsystem | architecture-review §4.10 (parked) | high | L | port-later | core (when triggered) |
| auth.ts (auth flow) | subsystem | partial — channels handle their own | high | M | port | core |
| call.ts / stream handling | subsystem | partial — providers handle stream | high | M | port | core |

## Notes

Openclaw bundles **107 extensions** spanning 20+ channels, 14 providers, and 50+ specialty plugins. Most are realistic ports but only a handful unblock new capabilities for OpenComputer:

1. **`web-fetch-visibility` (SSRF + auth guards)**. OpenComputer's WebFetch trusts any URL — the openclaw pattern adds redirect-loop checks, internal-network rejection, and auth-aware request signing. Effort: S. Belongs in core.
2. **Multi-provider WebSearch dispatch**. OpenComputer ships DuckDuckGo only. Openclaw routes across DDG / Google / Brave / Tavily / Exa / Firecrawl with priority ordering. Effort: S for the dispatcher; each provider is a small additional port.
3. **`message-tool` + `sessions-list-tool` / `sessions-history-tool` / `session-status-tool`**. Tool-shaped wrappers around OpenComputer's existing channel + session systems. Without them the agent can't programmatically introspect or send to other sessions. Effort: S each.
4. **Local-inference providers (ollama, lmstudio)**. Cheap port (the API surface is OpenAI-compat already). Worth doing in Phase 11d alongside the rest of the provider plugins.
5. **`memory-lancedb` and `memory-wiki`**. Once Phase 11d's episodic memory ABC lands, these become demonstrably-useful reference plugins. Defer to post-11d.

**Skip list rationale**: RxJS-streaming async tools (music/video gen) need a Python-native rewrite; openshell is an eval-risk we already dodge with our scoped Bash; Asia-region and self-hosted-only channels are dogfood-gated; OTel + QA harness are ops infra, not end-user surface.

**The openclaw plugin SDK boundary work is fully extracted** (already-have). The typed gateway protocol is parked in architecture-review §4.10 with a trigger condition — defer until that fires.
