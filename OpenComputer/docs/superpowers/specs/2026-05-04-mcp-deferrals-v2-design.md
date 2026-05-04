# MCP Deferrals v2 — Design Spec

**Date:** 2026-05-04
**Status:** Draft → ready for review
**Source:** Two real Tier-2 gaps after Karpathy verification

---

## 1. Goal

Close 2 verified MCP-area Tier-2 gaps in one focused PR.

**Karpathy "Think Before Coding" verification:** Of 4 candidate items, 2 were false-pending after grepping the actual source:
- ~~flock follow-ups (cli_profile/profiles/setup_wizard/cli_bindings)~~ — none of those write profile.yaml RMW; cli_bindings already has `_mutate` with file lock
- ~~AgentCache wiring~~ — `AgentRouter._loops: dict[str, AgentLoop]` already caches per-profile; LRU eviction is overkill for the small bounded set

**Real remaining items:**

| # | Item | Why |
|---|---|---|
| 1 | MCP background health-loop invoker | `MCPManager.health_check_all()` exists (PR #431) but no periodic invoker. Server failures stay undetected until next tool call. |
| 2 | Phase 12m remote MCP catalog | Bundled `PRESETS` dict is hardcoded; users can't install community servers without editing OC source. |

---

## 2. Detailed designs

### 2.1 MCP background health-loop invoker

**Problem:** `MCPManager.health_check_all()` exists but is never called. Need a periodic background task firing every 30s (configurable) that probes every connected server and marks unhealthy ones — so when a tool dispatch hits `state="error"`, we already know.

**Approach:**
- Add `MCPManager.start_health_loop(interval_seconds: float = 30.0)` that creates an `asyncio.Task` running an infinite loop: `await asyncio.sleep(interval); await self.health_check_all()`. Task reference stored on the manager.
- Add `MCPManager.stop_health_loop()` that cancels the task.
- Wire into the gateway daemon lifecycle: start when MCPManager is constructed (or after `connect_all`), stop on shutdown.
- Config knob: `MCPConfig.health_interval_seconds: float = 30.0` (already added in PR #431; consume it now).

**Why a task on the manager, not a global asyncio job?** Multiple MCPManager instances (gateway + CLI in same process) would each get their own loop, scoped to their own connections.

**Edge cases handled:**
- Idempotent start: calling `start_health_loop()` twice is a no-op (returns existing task).
- Cancel-clean shutdown: cancellation propagates through `asyncio.sleep`; the loop exits cleanly.
- Exception in `health_check_all`: caught + logged; loop continues (don't crash on one bad server).

**Files:**
- Modify: `opencomputer/mcp/client.py` — add `start_health_loop` + `stop_health_loop` to `MCPManager`
- Modify: gateway startup/shutdown wherever `MCPManager` is instantiated (search for `MCPManager(`)
- Test: `tests/test_mcp_health_loop.py`

### 2.2 Phase 12m remote MCP catalog

**Problem:** OC bundles a hardcoded `PRESETS: dict[str, Preset]` dict in `opencomputer/mcp/presets.py`. Adding a new community server today requires editing OC source. The user-facing `oc mcp install <slug>` only knows about bundled presets.

**Approach:**
- Add `opencomputer/mcp/remote_catalog.py` — fetches the index of community servers from a known URL (`https://raw.githubusercontent.com/modelcontextprotocol/servers/main/README.md` parses the structured list, OR a JSON catalog if one exists).
- Cache the fetched catalog locally at `~/.opencomputer/mcp_catalog_cache.json` with a 24h TTL. Stale cache = re-fetch + replace; fetch failure with cache present = use cache + warn.
- Add `oc mcp catalog --remote` to fetch+display, `oc mcp catalog --refresh` to bypass cache.
- Add `oc mcp install <slug>` extension: if slug not in bundled `PRESETS`, fall back to remote catalog. Use the remote entry's documented `command/args/env` shape.

**Tradeoff considered:** Fetching from `modelcontextprotocol/servers` README requires markdown parsing (fragile). A JSON manifest at a stable URL would be cleaner. **First cut: ship a curated `oc-mcp-catalog.json` at a known URL** (could be a GitHub repo we maintain) that lists the same servers but in machine-parseable form. Bundled `PRESETS` becomes the offline fallback when the remote fetch fails.

**For v1, ship the FETCH + CACHE + DISPLAY but NOT the install fallback.** Install-from-remote needs more validation (checksums, version pinning, etc) — punt to v2.

**Files:**
- Create: `opencomputer/mcp/remote_catalog.py` — fetch + cache + parse
- Modify: `opencomputer/cli_mcp.py` — add `--remote` and `--refresh` flags to `mcp catalog` command
- Test: `tests/test_mcp_remote_catalog.py`

---

## 3. Architecture diagram

```
┌──────────────────────────────────────────────┐
│ MCPManager startup                           │
│   await connect_all(servers, ...)            │
│   self.start_health_loop(interval=30.0)      │  ← NEW
│       ↓ asyncio.create_task                  │
│   ┌──────────────────────────────────┐       │
│   │ while True:                       │       │
│   │   await asyncio.sleep(30)         │       │
│   │   try:                            │       │
│   │     await self.health_check_all() │       │
│   │   except Exception: log + continue│       │
│   └──────────────────────────────────┘       │
│                                               │
│ MCPManager shutdown                           │
│   self.stop_health_loop()                     │  ← NEW
│   await self.shutdown()                       │
└──────────────────────────────────────────────┘

────────────────────────────────────────────────

┌──────────────────────────────────────────────────┐
│ oc mcp catalog --remote                          │
│        ↓                                         │
│ remote_catalog.fetch_catalog(refresh=False)      │  ← NEW
│   if cache fresh (< 24h): return cache           │
│   else:                                          │
│     try: data = httpx.get(CATALOG_URL).json()    │
│            write to ~/.opencomputer/             │
│              mcp_catalog_cache.json              │
│            return data                           │
│     except: if cache exists: warn + return cache │
│             else: raise                          │
└──────────────────────────────────────────────────┘
```

---

## 4. Testing strategy

| Item | Test approach |
|------|---------------|
| Health-loop start/stop | Mock `health_check_all` to count invocations. Mock `asyncio.sleep` to advance fast. Start loop → wait 3 ticks → assert ≥3 calls. Stop → assert no further calls. Idempotent start → only one task. Exception in probe → loop continues, next tick still fires. |
| Remote catalog fetch | Mock httpx response with sample catalog JSON. Verify cache file written with TTL. Second fetch within 24h hits cache. Fetch failure with stale cache → returns cache + warn. Fetch failure no cache → raises. |

---

## 5. Out of scope (deferred)

- **Install-from-remote** — needs version pinning + checksum validation + env-var prompting; v2.
- **Catalog source URL configurability** — for v1 hardcode the URL; future config knob.
- **Catalog signing/verification** — per OC's security posture (OSV check is fail-open warn for now), defer.

---

## 6. Self-audit (executed before showing this design)

### What might be wrong with this scope?

- **Risk: Background task spawned in `__init__` is a constructor side-effect.** Counter: explicit `start_health_loop()` method call by the gateway daemon, not implicit in `__init__`. CLI mode (one-shot) skips it.
- **Risk: Test of health-loop using mocked sleep needs careful asyncio handling.** Counter: standard pattern — `monkeypatch.setattr("opencomputer.mcp.client.asyncio.sleep", _no_sleep)` then await one or two iterations via `asyncio.create_task` + `asyncio.sleep(0)` to yield.
- **Risk: Remote catalog URL hardcoded.** Counter: documented as v1 simplification; config knob is a 3-line follow-up.
- **Risk: Network call in tests.** Counter: all httpx calls mocked; no network IO in CI.
- **Risk: cache file location.** Counter: `~/.opencomputer/mcp_catalog_cache.json` follows OC's existing per-home convention.

### What edge cases might bite?

1. **Health-loop firing during shutdown** — `health_check_all` may try to probe a connection mid-disconnect. The probe will raise; caught by the exception handler in the loop. State flip to "error" right before "disconnected" is harmless.
2. **Remote fetch returns invalid JSON** — wrap in try/except json.JSONDecodeError → fall back to cache or raise.
3. **Cache file is partial/corrupted** — same: try/except + delete + re-fetch.
4. **24h TTL granularity** — using `os.path.getmtime` + `time.time()` comparison; clock skew between cache write and read is ≤ seconds. Acceptable.

### Was anything missed?

Re-checked Tier-2/4 list:
- Phase 14.F per-profile credentials: bigger scope (1-2 days), separate PR
- Phase 12e coding-harness dedup: exploratory; needs scoping pass
- Phase 12d.3-6 plugin ports: each ~1 day, separate PRs
- E7 keyword-match demand detection: 1-2 days, separate PR

### Defensible? Yes.

2 commits, ≤300 LOC total, ~4-5h estimate. Each item closes a verified gap.
