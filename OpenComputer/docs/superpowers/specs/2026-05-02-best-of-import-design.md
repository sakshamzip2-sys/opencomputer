# Best-of-OpenClaw/Hermes/Claude-Code import — design (rev 2)

**Status:** Rev 2 (2026-05-02). Pass-2 adversarial audit revealed 5 of 12 items in rev 1 already exist as production code at well-designed implementations. Plan rewritten to amend / cut / extend rather than replace. Adds `oc update` per user request.

**Author:** brainstorm + popularity research + Pass-1 self-audit + Pass-2 adversarial subagent + on-disk verification.

---

## 1. What changed from rev 1 (audit findings)

Pass-2 audit caught and on-disk verification confirmed:

| Rev 1 item | Status | Reason |
|---|---|---|
| D1 MemoryTool | **CUT** — duplicate | `opencomputer/tools/memory_tool.py` exists with `add/replace/remove/read` verbs over MEMORY.md/USER.md, atomic writes via MemoryManager, threat-scan, locking. Plan would overwrite. |
| D3 SendMessageTool | **CUT** — duplicate | `opencomputer/tools/send_message.py` exists with body validation, MAX_BODY_CHARS=10k, thread_hint, profile-aware OutgoingQueue. Plan would overwrite. |
| A2 ActiveMemoryInjector | **CUT** — duplicate | `opencomputer/agent/active_memory.py` exists, wired into `loop.py:1022` behind `config.memory.active_memory_enabled`. Plan would overwrite. |
| C1 firecrawl | **CUT** — duplicate | `opencomputer/tools/search_backends/firecrawl.py` exists; backed by `WebSearchTool`. Adding `extensions/firecrawl/` creates competing tool. |
| C2 tavily | **CUT** — duplicate | Same as C1 — `search_backends/tavily.py` already exists. |
| C3 exa | **CUT** — duplicate | Same — `search_backends/exa.py` already exists. |
| D2 SessionSearchTool | **AMEND** | `SessionDB.search()` returns `list[dict]` not objects; rev 1 used `h.session_id`. Switch to `dict["session_id"]`; use `search_messages()` for full content. |
| D4 MCPOAuth | **AMEND** | Hermes' `mcp_oauth.py` actually wraps the MCP Python SDK's `OAuthClientProvider` (httpx.Auth subclass). Rev 1 reimplemented from scratch — would miss dynamic registration, discovery, refresh, callback server. Use the SDK. |
| A1 Block streaming chunker | **AMEND** | Rev 1 source path `sources/openclaw-2026.4.23/src/streaming/` does not exist. Real chunker lives per-channel (`extensions/{discord,telegram,slack,line,feishu,msteams,qqbot}/src/chunk.ts`). Pick one as canonical reference. Also fix sync-context `loop.create_task()` race. |
| A3 Standing Orders | **AMEND** | Rev 1 regex `(?!^## )` lookahead is wrong in Python `re`; will eat adjacent H2 sections. Replace with line-state-machine parser. |
| B1 ollama provider | **KEEP** with fixes | Fix dir name (Python imports need underscore); copy `openai-provider`'s `stream_complete` impl rather than leaving abstract method as `NotImplementedError`. |
| B2 groq provider | **KEEP** | OpenAI-compatible. Mostly mechanical. |

**Newly added per user request (2026-05-02):**

| New item | Source |
|---|---|
| `oc update` + banner | `sources/hermes-agent-2026.4.23/hermes_cli/{banner.py:126-183, main.py:5425+}` |

**Final scope: 7 items, 4 PRs.**

---

## 2. The 7 items

### Phase D' — Hermes tool ports (revised, 1 PR)

#### D2 — session_search_tool (AMEND)
- **Source:** `sources/hermes-agent-2026.4.23/tools/session_search_tool.py`
- **Lands at:** `opencomputer/tools/session_search.py` (new)
- **What it does:** LLM-callable wrapper around `SessionDB.search_messages()` (FTS5 over message body). Returns `[session_id, role, ts, content[:200]]` per hit. NO Gemini Flash summarization (rev 1 had it; lower priority — wrap raw FTS5 output).
- **API correctness:** consume hits via `dict["key"]`. Use `search_messages()` not `search()` (search returns highlight snippets, not full body).
- **Why:** OC has `RecallTool` already (manual call), but SessionSearch is a different shape (history search vs episodic recall). Hermes inventory tagged "high value, port to core."
- **Tests:** mock SessionDB returning real-shape `list[dict]`; verify no AttributeError; verify result formatting; verify error path on DB locked.

#### D4 — mcp_oauth (AMEND — use MCP SDK)
- **Source:** `sources/hermes-agent-2026.4.23/mcp/oauth.py` — wraps `mcp.client.session.OAuthClientProvider` (httpx.Auth subclass).
- **Lands at:** `opencomputer/mcp/oauth.py` (new) + integration into `opencomputer/mcp/client.py`.
- **What it does:** Adapts the MCP Python SDK's `OAuthClientProvider` to OC's MCP client. Caches tokens to `~/.opencomputer/<profile>/mcp/tokens.json`. Provides callback server for the authorization code redirect. Uses ContextVar-scoped profile home.
- **Why:** Many MCP servers (GitHub, Notion, Drive, OAuth-flow Slack) require OAuth 2.1. SDK handles dynamic client registration, discovery, refresh, PKCE, step-up auth — re-implementing is wasteful.
- **Tests:** mock httpx Authorization Server; verify token exchange + cache write/read; verify callback server lifecycle.

### Phase B — Provider plugins (2 items, 1 PR)

#### B1 — ollama provider (KEEP with fixes)
- **Source:** `sources/openclaw-2026.4.23/extensions/ollama/`
- **Lands at:** `extensions/ollama_provider/` (UNDERSCORE — Python module imports require it)
- **What it does:** OpenAI-compatible HTTP client to local Ollama daemon (`http://localhost:11434/v1`). Streaming chat + tool calling.
- **Critical fix:** `stream_complete()` MUST be implemented (not `NotImplementedError`) — `BaseProvider.stream_complete` is `@abstractmethod`. Copy `extensions/openai-provider/provider.py:stream_complete` since Ollama's protocol is OpenAI-compatible.
- **Caveat:** the existing `openai-provider` already supports `OPENAI_BASE_URL=http://localhost:11434/v1` for local Ollama. The new dedicated provider gives a cleaner config UX (no env var fiddling, defaults right) and a logical home for Ollama-specific extensions later (modelfile management, native tool calling, etc.).
- **Tests:** mocked HTTP for streaming + tool-call paths; live test marked `@pytest.mark.benchmark`.

#### B2 — groq chat provider (KEEP)
- **Source:** `sources/openclaw-2026.4.23/extensions/groq/`
- **Lands at:** `extensions/groq_provider/` (UNDERSCORE)
- **What it does:** OpenAI-compatible client to `api.groq.com/openai/v1`. Reads `GROQ_API_KEY` (already declared by `voice/groq_stt.py`).
- **Why:** 276–1500 t/s, real chat-speed value-add.
- **Tests:** mocked HTTP; live opt-in benchmark.

### Phase A' — Architectural ports (2 items, 1 PR)

#### A1 — Block streaming chunker + humanDelay (AMEND)
- **Source (corrected):** `sources/openclaw-2026.4.23/extensions/discord/src/chunk.ts` as the canonical reference. (Rev 1's `src/streaming/` path was wrong; OpenClaw's chunkers are per-channel.)
- **Lands at:** `opencomputer/gateway/streaming_chunker.py` (new) + opt-in wiring to one channel adapter (telegram first; document the pattern for others).
- **What it does:** Buffers token-deltas; emits at paragraph → newline → sentence → whitespace boundary; never inside code fences (track ```` ``` ```` pairs); idle-coalesces (configurable `idleMs`); randomized 800–2500ms `humanDelay` between blocks.
- **Critical fix:** `feed()` is sync but uses `asyncio.create_task()`. Document that `feed` must be called from coroutine context. Use `asyncio.get_running_loop()` (not deprecated `get_event_loop()`); fallback to immediate flush in tests where no loop is running.
- **Tests:** chunker unit tests (boundary preference, code-fence safety, coalescing windows); 1 telegram integration test verifying chunks emit at expected boundaries; 1 timing test using monotonic-time mock to make humanDelay testable.

#### A3 — Standing Orders (AMEND — line-state-machine parser)
- **Source:** `sources/openclaw-2026.4.23/src/agents/` + AGENTS.md `## Program:` examples
- **Lands at:** `opencomputer/agent/standing_orders.py` (new) + AGENTS.md schema doc + one hook in `opencomputer/agent/loop.py` to inject parsed orders as system context.
- **What it does:** Parses `## Program: <name>` blocks from AGENTS.md. Each block has fields `Scope:`, `Triggers:`, `Approval Gates:`, `Escalation:` (multi-line values supported). Loop applies them as additional system context per turn.
- **Critical fix:** parser is line-state-machine, not regex. State enum: `OUTSIDE → IN_HEADER → IN_FIELD → IN_BODY`. H2 transition out, blank line + new H2 ends body. Field values can span multiple lines until next `Key:` or end of block.
- **Tests:** parser unit tests (well-formed, malformed, adjacent H2 sections, multi-line field values, empty file); 1 integration test (apply-orders-to-system-context).

### Phase E — Update command (NEW, 1 PR)

#### E1 — `oc update` + banner integration
- **Source:** `sources/hermes-agent-2026.4.23/hermes_cli/banner.py:126-183` (background prefetch + cache + check), `main.py:5425+` (cmd_update + ff-only pull).
- **Lands at:** `opencomputer/cli/update.py` (new command) + `opencomputer/cli/banner.py` (modified — add behind-count display) + reuse `~/.opencomputer/.update_check` for cache.
- **What it does (4 layers per Hermes' design):**
  1. Background prefetch on CLI startup — runs `git fetch origin` in a daemon thread; writes cache `~/.opencomputer/.update_check` JSON `{"ts": ..., "behind": N}` with 6h TTL.
  2. Banner integration — if `behind > 0`, banner shows `⚠ N commits behind — run oc update to update`.
  3. `oc update` command — git fetch origin, count `HEAD..origin/main`, print `Found N new commit(s)`, run `git pull --ff-only origin main`. Stash uncommitted work first if not on main; restore after.
  4. Pip-install detection — if `(repo_dir / ".git").exists()` is False, the install was via pip (no source); print "pip install --upgrade opencomputer to update" and exit cleanly.
- **Why:** OC has no self-update mechanism. Users on dev installs (most users today) cannot easily know how many commits behind main they are. Hermes' design is the standard (clean separation: prefetch → cache → banner → command).
- **Tests:** unit tests for `check_for_updates()` (cached hit, expired cache, fetch failure, count parse, non-git checkout). Unit tests for `cmd_update` (ff-only success, divergent (rev-only-fast-forward fails), branch-switch + stash + restore, already-up-to-date short-circuit). Mock subprocess.run for all git invocations.

---

## 3. PR structure + ordering

```
PR 1 — Phase D' (Hermes tools — 2 items, amended)
  ├─ D2 session_search_tool
  └─ D4 mcp_oauth (uses MCP SDK)

PR 2 — Phase B (Providers — 2 items)
  ├─ B1 ollama provider (with stream_complete + underscore dir name)
  └─ B2 groq provider

PR 3 — Phase A' (Architectural — 2 items)
  ├─ A1 streaming chunker (per-channel chunk.ts source)
  └─ A3 standing orders (line-state-machine parser)

PR 4 — Phase E (Update command — 1 item, NEW)
  └─ E1 oc update + banner
```

**4 PRs total.** Smaller than rev 1 (6 PRs / 12 items) because half the items were duplicates of existing code.

**Ordering rationale:**
- D first: smallest, tools-only, builds confidence and exercises the worktree pipeline.
- B second: providers — additive, no integration with agent loop.
- A third: architectural — touches loop.py + gateway. Highest risk; do after wins.
- E last: orthogonal to others; can land independently. Putting it last avoids blocking earlier PRs.

---

## 4. Cross-cutting decisions

**Source-of-truth:** READ from `sources/openclaw-2026.4.23/` and `sources/hermes-agent-2026.4.23/`. Implementer subagents must consult source files.

**Discovery before implementation:** every implementer must run `find opencomputer extensions -name "<item-name>*"` BEFORE writing code, to verify no duplicate exists. Rev 1's collision disasters were caused by skipping this step.

**Per-PR pattern:**
1. Fresh branch from `origin/main` (in worktree)
2. TDD red → green
3. ruff clean
4. Full pytest suite (voice-excluded) green vs main baseline
5. Push + open PR
6. Watch CI to green
7. Merge with `--delete-branch`
8. Pull main; clean local branch

**Subagent model:**
- **Opus** for: D4 (OAuth correctness — easy to mis-port); A1 (streaming logic + race-free `feed()`); A3 (parser semantics + scope/trigger contract); E1 (subprocess + git edge cases).
- **Sonnet** for: D2 (mechanical wrapping), B1 + B2 (mechanical port from openai-provider).
- **NEVER haiku** (per standing user preference).

**Worktree:** all work in `~/.config/superpowers/worktrees/claude/phase-3/OpenComputer`. NEVER touch `/Users/saksham/Vscode/claude`.

**Plugin SDK boundary:** plugins under `extensions/` MUST NOT import from `opencomputer/*`.

---

## 5. Error handling

| Item | Failure | Behavior |
|---|---|---|
| D2 SessionSearch | DB locked / corrupt | Return `ToolResult(is_error=True)`; do not crash agent loop |
| D4 mcp_oauth | Discovery fails / invalid grant / refresh fails | Surface to user; tokens.json untouched on partial failure |
| B1 ollama | Connection refused / 401 | Raise `RuntimeError`; surface to user |
| B2 groq | Rate limit (429) / auth error | Surface to user with key advisory |
| A1 chunker | Buffer logic raises / no event loop | Safe-mode fallback: emit raw token-stream; log ERROR once |
| A3 standing-orders | Malformed `## Program:` block | Log ERROR, skip that program, parse rest of file; do not crash |
| E1 update | Network error / fetch fails | Print error to stderr; exit non-zero; do not crash CLI on banner-mode prefetch |

---

## 6. Testing

**Per-item:** unit tests for each verb/path. Mock external APIs. Live calls behind `@pytest.mark.benchmark`.

**Cross-cutting:**
- Full pytest suite (voice-excluded) green vs origin/main baseline at every merge.
- ruff check clean.
- Plugin SDK boundary test (existing) catches `from opencomputer` imports inside `extensions/`.
- Each PR's test plan documented in PR description.
- Integration tests for: A1 (telegram chunks), A3 (loop applies parsed orders), E1 (cmd_update happy path).

---

## 7. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Parallel session interference | Worktree usage; explicit branch verification before push; precise `git add` |
| Plugin import path mismatch (B1/B2) | Use underscore dir name; verify load via `python -c "import extensions.ollama_provider"` before push |
| `OutgoingQueue` not present in CLI/test paths | Existing `send_message` already handles this; D4 doesn't touch it |
| MCP SDK version drift on OAuth signatures | Pin `mcp>=1.X` in pyproject; test against the pinned version |
| Banner update check on offline machines | 6-hour cache + soft-fail subprocess + try/except in banner.py |
| Standing-orders authority leak (parser eats next H2) | Line-state-machine parser + tests for adjacent H2 |
| Streaming chunker breaks code blocks | Code-fence-tracking unit test + safe-mode fallback |

---

## 8. Out of scope

- Items already shipped: MemoryTool, SendMessageTool, ActiveMemoryInjector, all 5 search backends.
- 50+ redundant providers (covered by openrouter or niche).
- Niche channels (irc, twitch, qqbot, line, zalo, nostr).
- Image/video generation (CLAUDE.md §5 wont-do).
- Hermes "skip" items per inventory.md.

---

## 9. Approval

Rev 2 locked 2026-05-02 after Pass-2 adversarial audit + on-disk verification. Audit-rejected rev 1 (which would have overwritten 3 production files and duplicated 5 search backends).

**Lesson encoded for future plans:** before writing a port plan, run discovery sweep — `find . -name "<each-target-filename>*"` — for every item. Audit doc + plan doc both depend on this step.

Next: implementation plan via `superpowers:writing-plans`, then execution via `superpowers:subagent-driven-development`.
