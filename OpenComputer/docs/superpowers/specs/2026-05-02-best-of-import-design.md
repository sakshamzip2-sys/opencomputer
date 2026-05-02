# Best-of-OpenClaw/Hermes/Claude-Code import — design

**Status:** Design approved (2026-05-02). Pending implementation plan + execution.

**Author:** brainstorm session, popularity-research-filtered, audit-doc-cross-referenced.

**Reference docs:**
- `docs/refs/openclaw/2026-04-28-major-gaps.md` — existing OpenClaw curation (4 picks)
- `docs/refs/openclaw/2026-04-28-deep-feature-survey.md` — 117-extension catalog
- `docs/refs/hermes-agent/inventory.md` — Hermes port verdicts (2026-04-22)
- `docs/refs/hermes-agent/2026-04-28-major-gaps.md` — Hermes curation
- 2026-05-02 web search of agent-tool popularity benchmarks (Firecrawl, AImultiple, Composio, Glukhov, Pinggy, DigitalApplied)

---

## 1. Why

OpenComputer ships with 31 extensions today (12 channels, 5 providers, 7 agent surfaces, 1 memory backend, 4 system, 1 device, 1 example). The four reference codebases (OpenClaw, Hermes-agent, Claude Code, Kimi-CLI) collectively expose ~135 plugin-shaped capabilities. Most are redundant, niche, or outside OC's positioning. A focused subset of **12 items** passes both filters:

- **Existing audits** (2026-04-28): the OpenClaw and Hermes audits independently selected these as "actually worth porting."
- **Popularity research** (2026-05-02): web-search benchmarks of agent-tool adoption confirm the picks are widely-used in the broader 2026 ecosystem, not just internal favorites.

**Items NOT in this scope (explicitly cut):**
- 50+ redundant LLM providers (covered by `openrouter-provider` proxy or niche)
- 25+ niche channel adapters (irc, twitch, qqbot, line, zalo, nostr — not OC's audience)
- Image/video generation (CLAUDE.md §5 wont-do for canvas)
- SIP voice-call, mobile companion apps (CLAUDE.md §5 wont-do)
- External-CLI-as-harness bridges (codex, opencode — anti-pattern; OC IS the harness)
- Hermes "skip" items per inventory.md (mixture_of_agents, image_generation_tool, vision_tools, etc.)
- LM Studio + vLLM (GUI-focused / production-serving — not personal-agent shape)
- Mistral, Brave, DuckDuckGo (covered by openrouter / WebSearch chain / lower priority)

## 2. What — the 12 items

### Phase A — Architectural ports (3 items, OpenClaw)

#### A1 — Block streaming chunker + humanDelay
- **Source:** `sources/openclaw-2026.4.23/src/streaming/` + OpenClaw streaming docs
- **Lands at:** `opencomputer/gateway/streaming_chunker.py` (new) + adapter integrations in `extensions/telegram/`, `extensions/discord/`, `extensions/slack/`, etc.
- **What it does:** Buffers token-deltas; splits at paragraph → newline → sentence → whitespace boundaries; never inside code fences; idle-coalesces (configurable `idleMs`); emits with randomized 800–2500ms `humanDelay` between blocks. Per-channel opt-in via plugin config.
- **Why:** Channels today get raw token-stream → robotic "Telegram feels mechanical" UX. OpenClaw is uniquely sophisticated here; web research confirms this is the standard chat-channel UX pattern in 2026.
- **Tests:** chunker unit tests (boundary preference, code-fence safety, coalescing windows); 2-3 channel integration tests (telegram + discord) verifying chunks emit at expected boundaries.

#### A2 — Active Memory (blocking pre-reply recall)
- **Source:** `sources/openclaw-2026.4.23/extensions/active-memory/`
- **Lands at:** `opencomputer/agent/active_memory.py` (new) + one hook in `opencomputer/agent/loop.py` before reply emission
- **What it does:** Bounded sub-agent runs on every eligible reply turn; queries `memory_search` + `memory_get`; injects result as hidden untrusted prefix to the assistant message. Token budget cap; eligibility filters (skip empty turns, tool-result-only turns); opt-in flag.
- **Why:** OC has reactive `RecallTool` (LLM decides to call it) and `reviewer.py` (post-response). Neither is the proactive blocking gate that makes relevant memories surface in casual chat by default. Materially different recall profile.
- **Tests:** active-memory unit tests (search execution, prefix injection, budget cap, eligibility filter); 1 agent-loop integration test confirming recall fires before reply lands.

#### A3 — Standing Orders (text-contract for autonomous program authority)
- **Source:** `sources/openclaw-2026.4.23/src/agents/` + AGENTS.md `## Program:` examples
- **Lands at:** `opencomputer/agent/standing_orders.py` (new parser) + AGENTS.md schema doc + `opencomputer/agent/loop.py` (apply standing orders as system context)
- **What it does:** Declarative `## Program: <name>` blocks in AGENTS.md grant the agent permanent operating authority for autonomous programs. Each block defines: `scope`, `triggers`, `approval_gates`, `escalation_rules`. Combined with OC's existing cron, fills the "I want the agent to OWN this program" gap.
- **Why:** Hermes has cron jobs (job-execution-as-syntax) and OC has the same. OpenClaw's text-contract DSL is the higher-level abstraction Hermes/OC lack.
- **Tests:** parser unit tests (well-formed + malformed AGENTS.md); 1 integration test (cron trigger fires + scope check enforced + approval gate respected).

### Phase B — Provider plugins (2 items, OpenClaw)

#### B1 — ollama provider
- **Source:** `sources/openclaw-2026.4.23/extensions/ollama/` (HTTP client to localhost:11434)
- **Lands at:** `extensions/ollama-provider/` — new bundled extension following the shape of `extensions/anthropic-provider/`
- **What it does:** OpenAI-compatible HTTP client to local Ollama daemon. Streaming chat completions, tool calling, multimodal (vision-language). Default base URL `http://localhost:11434/v1`; configurable.
- **Why:** Per 2026 popularity benchmarks, Ollama is the #1 local-LLM tool for individual developers. OpenRouter doesn't help (cloud-only). Fills privacy/offline gap.
- **Tests:** mocked HTTP responses for streaming + tool-call paths; live test marked `@pytest.mark.benchmark` (opt-in, requires running ollama).

#### B2 — groq chat provider
- **Source:** `sources/openclaw-2026.4.23/extensions/groq/`
- **Lands at:** `extensions/groq-provider/` — new bundled extension
- **What it does:** OpenAI-compatible client to `api.groq.com/openai/v1`. Default models: `groq/llama-4-70b`, `groq/mixtral-8x7b`. Reads `GROQ_API_KEY` env var (already set up for the existing Groq STT integration at `opencomputer/voice/groq_stt.py`).
- **Why:** Per 2026 inference benchmarks, Groq delivers 276–1500+ tokens/sec — up to 20x faster than GPU APIs. Real value-add for user-facing chat speed.
- **Tests:** mocked HTTP; live opt-in benchmark.

### Phase C — Search tool plugins (3 items, OpenClaw)

#### C1 — firecrawl tool
- **Source:** `sources/openclaw-2026.4.23/extensions/firecrawl/`
- **Lands at:** `extensions/firecrawl/` — new bundled tool
- **What it does:** LLM-callable tool wrapping Firecrawl's `/search` (LLM-friendly results) and `/scrape` (clean markdown from any URL). Free tier: 500 credits.
- **Why:** Per 2026 web-research benchmarks, Firecrawl is the "starting recommendation" for agent web research — search + content extraction in one platform.
- **Tests:** mock HTTP layer; live opt-in benchmark.

#### C2 — tavily tool
- **Source:** `sources/openclaw-2026.4.23/extensions/tavily/`
- **Lands at:** `extensions/tavily/` — new bundled tool
- **What it does:** LLM-callable tool wrapping Tavily's search API. Agent-focused, framework-integration-friendly. Free tier available.
- **Why:** Tavily is positioned specifically as a search layer for agents; complementary to Firecrawl (different relevance profile).
- **Tests:** mock + opt-in benchmark.

#### C3 — exa tool
- **Source:** `sources/openclaw-2026.4.23/extensions/exa/`
- **Lands at:** `extensions/exa/` — new bundled tool
- **What it does:** LLM-callable tool wrapping Exa's neural/embedding-based semantic search. Best for company/people queries. Free tier.
- **Why:** Different from Firecrawl/Tavily (semantic vs keyword); leads on company/people benchmarks per Exa's published metrics.
- **Tests:** mock + opt-in benchmark.

### Phase D — Hermes tool ports (4 items)

#### D1 — memory_tool
- **Source:** `sources/hermes-agent-2026.4.23/tools/memory_tool.py`
- **Lands at:** `opencomputer/tools/memory.py` — new tool registered in the global tool registry
- **What it does:** Wraps `MemoryManager`'s declarative + skills + episodic surfaces with LLM-callable verbs: `write`, `append`, `search`, `list`, `delete`. Today MEMORY.md is a plain file the agent reads as system prompt context — the LLM cannot edit it as a tool action.
- **Why:** Per Hermes inventory (audited 2026-04-22), `memory_tool` was tagged "high value, port to core."
- **Tests:** unit tests for each verb (5+ tests).

#### D2 — session_search_tool
- **Source:** `sources/hermes-agent-2026.4.23/tools/session_search_tool.py`
- **Lands at:** `opencomputer/tools/session_search.py` — new tool
- **What it does:** Wraps `SessionDB.search` (FTS5) as an LLM-callable tool. Today the FTS5 engine works but only the CLI calls it (`opencomputer search QUERY`).
- **Why:** Per Hermes inventory, "high value, port to core." Makes semantic session history available to the LLM mid-conversation.
- **Tests:** unit tests for query + filter (limit, before/after) cases.

#### D3 — send_message_tool
- **Source:** `sources/hermes-agent-2026.4.23/tools/send_message_tool.py`
- **Lands at:** `opencomputer/tools/send_message.py` — new tool
- **What it does:** Cross-platform "send message to platform X, chat Y" tool. Uses `OutgoingQueue` + `ChannelDirectory` already in OC (already gateway-wired). Useful for cron jobs / standing orders / scheduled agents that need to send proactively without a live `MessageEvent`.
- **Why:** Per Hermes inventory, "high value, port to core." Load-bearing for scheduled/autonomous workflows.
- **Tests:** mock channel adapter; verify enqueue + delivery path.

#### D4 — mcp_oauth (OAuth 2.1 client for MCP servers)
- **Source:** `sources/hermes-agent-2026.4.23/mcp/oauth.py`
- **Lands at:** `opencomputer/mcp/oauth.py` (new) + integration into `opencomputer/mcp/client.py`
- **What it does:** Implements OAuth 2.1 authorization code flow with PKCE for MCP servers. Today OC's MCP client only handles unauthenticated/static-token MCP servers.
- **Why:** Per Hermes inventory, "high value, port to mcp-bundle." Many MCP integrations (GitHub, Notion, Drive, Slack OAuth-flow servers) require OAuth.
- **Tests:** OAuth flow against mock authorization server (httpx mock); state + nonce validation; PKCE challenge correctness.

## 3. Phase structure + ordering

```
Phase D (Hermes ports — 4 items, 1 PR)        ← FIRST: smallest + most isolated
  ├─ D1 memory_tool
  ├─ D2 session_search_tool
  ├─ D3 send_message_tool
  └─ D4 mcp_oauth

Phase B (Providers — 2 items, 1 PR)            ← SECOND: small, self-contained
  ├─ B1 ollama provider
  └─ B2 groq chat provider

Phase C (Search tools — 3 items, 1 PR)         ← THIRD: small, self-contained
  ├─ C1 firecrawl
  ├─ C2 tavily
  └─ C3 exa

Phase A (Architectural — 3 items, 3 PRs)       ← LAST: biggest, deepest changes
  ├─ A1 Block streaming chunker (1 PR)
  ├─ A2 Active Memory (1 PR)
  └─ A3 Standing Orders (1 PR)
```

**6 PRs total.** Phase D + B + C bundle related items into 1 PR each (small, mechanical ports). Phase A's 3 items each get their own PR (substantial; each touches different subsystems).

**Ordering rationale:** D + B + C are small wins that ship plugins quickly. Phase A's architectural items are deeper changes; doing them last means easier wins land first and small wins aren't gated on big changes.

## 4. Cross-cutting decisions

**Source-of-truth:** READ from `sources/openclaw-2026.4.23/...` and `sources/hermes-agent-2026.4.23/...` directly. Code is more accurate than my paraphrase. Implementer subagents must consult source files, not just this spec.

**Per-PR pattern (reused across all 6 PRs):**
1. Branch from `origin/main` (in worktree at `~/.config/superpowers/worktrees/claude/phase-3` to avoid parallel-session contention)
2. TDD — failing test first, implementation, green
3. ruff clean
4. Full pytest suite (voice-excluded) green vs main baseline
5. Push + open PR
6. Watch CI to green
7. Merge with `--delete-branch`
8. Pull main; clean local branch

**Subagent model selection:**
- **Opus** for: A1 (streaming logic), A2 (agent loop integration), A3 (parser + scope/trigger semantics), D4 (OAuth flow correctness). Judgment-heavy.
- **Sonnet** for: B1, B2, C1, C2, C3, D1, D2, D3. Mechanical port from sources.
- **NEVER haiku** (per standing user preference).

**Worktree:** all work happens in `~/.config/superpowers/worktrees/claude/phase-3`. NEVER touch `/Users/saksham/Vscode/claude`. Precise `git add` (no `-A`).

**Plugin SDK boundary:** any new plugin under `extensions/` MUST NOT import from `opencomputer/*` (per existing test enforcement). Use `plugin_sdk/*` only.

## 5. Error handling

| Item | Failure mode | Behavior |
|---|---|---|
| Streaming chunker | Buffer logic raises | Safe-mode fallback: emit raw token-stream; log ERROR once |
| Active Memory | Recall sub-agent fails or times out | Skip injection; do not block reply emission; log WARN |
| Standing Orders | Malformed `## Program:` block | Log ERROR + skip that program; do not crash gateway |
| ollama / groq providers | Connection refused / 401 | Raise `RuntimeError` with context; surface to user |
| Search tools | Rate limit (429) | Tenacity retry with backoff; surface error after retries exhausted |
| memory_tool / session_search_tool | DB locked / corrupt | Surface as `ToolResult` error; do not crash agent loop |
| send_message_tool | OutgoingQueue is None (CLI/test path) | Return `ToolResult` error with clear message |
| mcp_oauth | Invalid state / nonce / PKCE | Raise; do not silently fall back to anonymous |

## 6. Testing

**Per-item:** unit tests for each verb/path. Mock external APIs. Live calls behind `@pytest.mark.benchmark` (opt-in).

**Cross-cutting:**
- Full pytest suite (voice-excluded) must be green vs origin/main baseline at every merge.
- ruff check must be clean.
- New plugin's `register(api)` must not violate plugin_sdk boundary (existing test catches this).
- Each PR's test plan documented in PR description.

## 7. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Parallel session interference (recurring this past week) | Worktree usage from start; explicit branch verification before push; precise `git add` |
| AGENTS.md schema collision with project-meta content | Standing Orders parser only matches `## Program:` headers; ignores everything else |
| `OutgoingQueue` not present in CLI/test paths | `send_message_tool` fails gracefully when queue is None |
| Search tool rate limits during test runs | Mock external calls; opt-in benchmarks for live tests |
| OAuth 2.1 spec drift | Implement per RFC 9700 (Best Current Practice); test against mock authorization server |
| Streaming chunker breaks code blocks | Explicit code-fence-tracking unit test; safe-mode fallback if logic raises |
| Active Memory adds latency to every turn | Token-budget cap + 2s timeout; configurable opt-in flag; skip on tool-result-only turns |

## 8. Out of scope

- 50+ redundant providers (alibaba, byteplus, fireworks, perplexity, etc. — covered by openrouter)
- 25+ niche channels (irc, twitch, qqbot, line, zalo, nostr — not OC audience)
- Image/video generation (canvas wont-do)
- LM Studio, vLLM (GUI / production-serving — not personal-agent shape)
- Mistral, Brave, DuckDuckGo, ChromaDB, LanceDB (covered or lower priority — can add later)
- Hermes "skip" items (mixture_of_agents_tool, image_generation_tool, vision_tools, etc.)
- 1500+ `claude-code-plugins-plus` SKILL.md files (mostly LLM-generated boilerplate)

## 9. Approval

Brainstorm decisions locked 2026-05-02:
- 12 items selected via popularity research + audit cross-reference
- 4 phases, 6 PRs total
- Order: D → B → C → A
- Subagent split: opus for 4 judgment-heavy, sonnet for 8 mechanical
- All work in worktree; precise `git add`; no haiku

Next step: implementation plan via `superpowers:writing-plans`.
