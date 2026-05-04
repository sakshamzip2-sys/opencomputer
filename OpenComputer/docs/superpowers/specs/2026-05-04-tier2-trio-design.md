# Tier-2 Trio — Design Spec

**Date:** 2026-05-04
**Status:** Draft → ready for review
**Reference:** CLAUDE.md §5 Tier-2 (dogfood-gated) + Tier-4 (latent debt)

---

## 1. Goal

Ship 3 pragmatic Tier-2/4 items as one focused PR with self-contained commits.

**Karpathy "Think Before Coding" verification (run before drafting):** Of the 6 items in the original trio menu, 4 were already shipped (Phase 15.A `oc resume`, `oc session resume`, Webhook adapter, Slack/Matrix/Email adapters). CLAUDE.md was outdated. Real remaining items:

| # | Item | Tier | Estimate |
|---|---|---|---|
| 1 | profile.yaml flock concurrency fix | Tier-4 | ~1h |
| 2 | OpenAI-compat endpoint (`POST /v1/chat/completions`) | 12c.1 last | ~half day |
| 3 | MCP reconnect + health-loop | 12m partial | ~half day |

---

## 2. Detailed designs

### 2.1 profile.yaml flock concurrency fix

**Problem:** 5 callers (`cli_profile.py`, `cli_bindings.py`, `setup_wizard.py`, `cli_plugin.py`, `profiles.py`) read `profile.yaml`, mutate, and write back. They use atomic write (`tmp + os.replace`) but no advisory lock. Two concurrent `oc plugin enable X` and `oc plugin enable Y` from sibling shells can race: both read the same baseline, both append their entry, last write wins → one entry lost.

**Approach:** Reuse the existing flock pattern from `opencomputer/cron/scheduler.py`'s `_acquire_tick_lock()`. Add a small helper `opencomputer/profiles_lock.py` exposing a context manager:

```python
from contextlib import contextmanager

@contextmanager
def profile_yaml_lock(profile_dir: Path):
    """Exclusive flock around profile.yaml read-modify-write.
    
    Blocks (LOCK_EX, no LOCK_NB) — concurrent writers serialize cleanly.
    Lock file is .profile.lock in the profile directory.
    """
    lock_path = profile_dir / ".profile.lock"
    fd = open(lock_path, "w")
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)  # blocking: serialize, don't fail
        yield
    finally:
        if fcntl is not None:
            try: fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception: pass
        fd.close()
```

Wrap each of the 5 writer callsites:

```python
with profile_yaml_lock(profile_dir):
    cfg = read_profile_yaml(...)
    cfg.plugins.enabled.append(plugin_id)
    write_profile_yaml_atomic(cfg, ...)
```

**Why blocking (no LOCK_NB)?** A plugin-enable that fails-fast on concurrent writes is hostile UX. Blocking serializes; conflicts disappear. Lock contention is rare (human-typed CLI calls) so blocking cost is negligible.

**Why a separate file (.profile.lock)?** Avoid locking profile.yaml itself — the file gets atomic-replaced (tmp + os.replace), which would invalidate any flock on the original inode.

**Files:**
- Create: `opencomputer/profiles_lock.py` — the context manager
- Modify: `cli_profile.py`, `cli_bindings.py`, `setup_wizard.py`, `cli_plugin.py`, `profiles.py` — wrap each profile.yaml write site
- Test: `tests/test_profile_yaml_flock.py`

### 2.2 OpenAI-compat endpoint

**Problem:** `extensions/api-server/adapter.py` exposes `POST /v1/chat` with a custom request/response shape. Tools like Cursor, aider, LibreChat speak OpenAI's `POST /v1/chat/completions` shape (`{"model": ..., "messages": [...], "stream": bool}` → `{"choices": [{"message": ..., "finish_reason": ...}], "usage": {...}}`). Without OpenAI-compat, OC can't be plugged in as a drop-in backend.

**Approach:** Add a parallel route in the api-server adapter:

```python
app.router.add_post("/v1/chat/completions", self._handle_openai_chat_completions)
```

The handler:
1. Parse OpenAI request body: `{model, messages, stream, max_tokens, temperature, ...}`.
2. Convert OpenAI message format → OC's `Message` shape (role + content).
3. Call `agent_loop.run_conversation(...)` with the messages.
4. If `stream: true`: return `text/event-stream` with `data: {"id": ..., "choices": [{"delta": {"content": "..."}}], ...}\n\n` per token chunk.
5. If `stream: false`: return single JSON response in OpenAI shape with `id`, `object: "chat.completion"`, `created`, `model`, `choices: [{index, message, finish_reason}]`, `usage: {prompt_tokens, completion_tokens, total_tokens}`.

**Why extend api-server, not new adapter?** Same daemon, same port, same auth — just two routes side-by-side. Mirrors how OpenAI's own server hosts both `/v1/completions` (legacy) and `/v1/chat/completions`.

**Auth model unchanged:** existing `API_SERVER_TOKEN` env var is checked on both routes.

**Files:**
- Modify: `extensions/api-server/adapter.py` — add `/v1/chat/completions` route + handler
- Create: `extensions/api-server/openai_format.py` — OpenAI ↔ OC message-format converters (small, focused)
- Test: `tests/test_api_server_openai_compat.py`

### 2.3 MCP reconnect + health-loop

**Problem:** `opencomputer/mcp/client.py` has `last_connect_error` field but no automatic reconnect on disconnect. When an MCP server (e.g., a long-running stdio process) dies mid-session, all subsequent tool calls fail until the user restarts OC. No periodic health probe either.

**Approach:** Two small additions to `MCPManager`:
1. **Periodic health-ping:** Every 30s (configurable via `mcp.health_interval_seconds`), iterate enabled MCP servers and call their `list_tools()` as a cheap probe. On failure → mark `unhealthy`.
2. **Auto-reconnect:** If a tool dispatch hits a transport error AND the server is marked `unhealthy`, attempt one reconnect before failing the call. Cap: 3 reconnect attempts per minute per server (back off 2s/4s/8s).

**Out of scope (defer):**
- Remote MCP catalog fetch (e.g., from `modelcontextprotocol/servers` GitHub) — large surface, separate PR
- WebSocket-based MCP transports — only stdio + SSE today; new transports are their own thing

**Files:**
- Modify: `opencomputer/mcp/client.py` — add `last_health_check_at`, `unhealthy` state, `attempt_reconnect()` method
- Modify: `opencomputer/mcp/manager.py` (or wherever MCPManager lives) — add health-loop background task
- Modify: `opencomputer/agent/config.py` — add `MCPConfig.health_interval_seconds: float = 30.0`
- Test: `tests/test_mcp_health_reconnect.py`

---

## 3. Architecture diagram

```
┌─────────────────────────────────────────────┐
│ profile.yaml read-modify-write              │
│   with profile_yaml_lock(profile_dir):      │  ← NEW context manager
│     cfg = read_profile_yaml()               │     blocks until lock available
│     cfg.plugins.enabled.append(...)         │
│     write_profile_yaml_atomic(cfg)          │
└─────────────────────────────────────────────┘

──────────────────────────────────────────────

┌─────────────────────────────────────────────┐
│ External tool (Cursor/aider/LibreChat)      │
│        ↓                                    │
│  POST /v1/chat/completions                  │  ← NEW route on api-server
│  {model, messages, stream}                  │
│        ↓                                    │
│  openai_format.openai_to_oc_messages(req)   │
│        ↓                                    │
│  agent_loop.run_conversation(messages)      │
│        ↓                                    │
│  openai_format.oc_to_openai_response(resp)  │
│        ↓                                    │
│  text/event-stream  OR  application/json    │
└─────────────────────────────────────────────┘

──────────────────────────────────────────────

┌──────────────────────────────────────────────┐
│ MCPManager (background task every 30s)       │
│   for srv in enabled:                        │
│     try: srv.list_tools()                    │
│     except: srv.unhealthy = True             │
│                                              │
│ On tool dispatch error + unhealthy:          │
│   srv.attempt_reconnect()  (≤3/min, 2/4/8s)  │
│   retry tool dispatch once                   │
└──────────────────────────────────────────────┘
```

---

## 4. Testing strategy

| Item | Test approach |
|------|---------------|
| profile.yaml flock | Spawn 2 threads, each acquires lock, writes a unique key. Assert both keys land in final yaml (no lost-update). Test lock release on exception. Test no-fcntl fallback (Windows path). |
| OpenAI-compat route | Mock agent_loop.run_conversation. POST OpenAI-shaped request → assert OpenAI-shaped response (id starts with `chatcmpl-`, choices[0].message.role = "assistant", usage fields present). Streaming test: parse SSE, assert each line is valid JSON delta. |
| MCP health/reconnect | Mock MCP client with controllable `list_tools` failure. Trigger health probe → assert unhealthy state. Trigger tool call when unhealthy → assert reconnect attempted. Assert backoff (third attempt within 1 min refuses with rate-limit). |

---

## 5. Self-audit (executed before showing this design)

### What might be wrong with this scope?

- **Risk: profile_yaml_lock blocking forever if a writer crashes mid-lock.** Counter: file locks are released by OS on process death. Stale lock files are cosmetic.
- **Risk: OpenAI-compat streaming is non-trivial — OC's stream events may not chunk cleanly to OpenAI's delta format.** Counter: OC providers already emit `text_delta` events; map 1:1 to OpenAI's `{"delta": {"content": "..."}}`. Tool-call deltas are OpenAI's `{"delta": {"tool_calls": [...]}}` — implement only text deltas in v1, defer tool-call streaming.
- **Risk: MCP health-ping every 30s adds load.** Counter: `list_tools` is cheap (single round-trip). 30s is configurable. Default off if no MCP servers configured.
- **Risk: Three independent items might be too much for one PR.** Counter: each is ≤200 LOC, no cross-deps. Reviewer chunks per-item.

### What edge cases might bite?

1. **profile_yaml_lock on Windows:** `fcntl` is None on Windows. The existing pattern in cron/scheduler.py falls back to `msvcrt.locking`. Mirror that.
2. **OpenAI-compat without `model` field:** OpenAI requires `model`; map missing `model` to OC's configured default with a 200-OK + warning header.
3. **MCP reconnect storm:** 3 servers all failing → 3 reconnect attempts. The 3-per-minute cap is per-server, so this is bounded. But log volume: rate-limit warning logs per server.
4. **OpenAI streaming aborted mid-response:** client closes connection — OC's run_conversation doesn't know. Detect via `aiohttp` `StreamResponse.closed` attribute and cancel the underlying loop task.
5. **profile.yaml writers from setup_wizard race with `oc plugin enable` started later:** wizard probably initializes the yaml. Lock semantics handle this — wizard holds the lock for its duration; plugin-enable waits.

### Was anything missed from the menu?

- Phase 12d.3-6 (memory-vector/memory-wiki/local-providers/media-tools): bigger scope, deferred.
- Phase 14.F (per-profile credentials): meaningful work, deferred to separate PR.
- Phase 12e (coding-harness dedup audit): exploratory; needs its own scoping pass.

### Defensible? Yes.

3 commits, 3 self-contained changes, all addressing real gaps Karpathy-verified against current source. Total ≤500 LOC. Estimated 1.5 days.
