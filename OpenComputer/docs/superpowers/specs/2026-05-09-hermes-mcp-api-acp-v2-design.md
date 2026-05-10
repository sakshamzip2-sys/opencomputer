# Hermes MCP / API Server / ACP — v2 gap-closure (Design)

**Date:** 2026-05-09
**Source spec:** `/Users/saksham/Downloads/files (1)/hermes-mcp-api-acp-v2.md`
**Status:** auto-approved (auto mode), self-audited via 9-lens framework

---

## 1. Goal

Close the remaining honest gaps between the Hermes MCP / API Server / ACP
reference spec and the current OpenComputer implementation. OC has shipped
substantial parity already (PRs #420 / #431 / #437 / #485 / #494 / #504 / #515);
this picks up 15 specific gaps spread across CORS, Idempotency, /v1/responses
ergonomics, MCP naming + filter shape, and ACP discoverability.

## 2. Scope (15 items, 5 tiers)

| # | Tier | Gap | Outcome |
|---|---|---|---|
| G1 | 1 | API server has no CORS | Add `aiohttp_cors` middleware honoring `API_SERVER_CORS_ORIGINS` env, preflight Max-Age 600, allow Authorization+Idempotency-Key+Content-Type |
| G2 | 1 | No `Idempotency-Key` dedup | 5-min LRU cache keyed by `(token-hash, key, body-sha256)` returning the cached response |
| G3 | 2 | `/v1/responses` opt-in 404 | Default-ON; remove API_SERVER_API_TYPE gate (keep env as deprecated alias) |
| G4 | 2 | No GET/DELETE `/v1/responses/{id}` | Wire `_handle_response_get` + `_handle_response_delete` to existing `_responses_storage` LRU |
| G5 | 2 | No `/v1/health` alias | Mirror `/health` handler at `/v1/health` |
| G6 | 2 | `API_SERVER_TOKEN` only | Accept `API_SERVER_KEY` as alias |
| G7 | 2 | No `API_SERVER_ENABLED` env | Plugin reads env; if `true` and not enabled in profile, auto-enable |
| G8 | 3 | MCP tool naming `<server>__<tool>` | Register additional `mcp_<server>_<tool>` alias (FastMCP-spec form) |
| G9 | 3 | Per-server `prompts: false` / `resources: false` | Add `prompts_enabled` / `resources_enabled` fields on `MCPServerConfig`; suppress utility tools when False |
| G10 | 3 | Per-server `timeout` / `connect_timeout` | Add fields on `MCPServerConfig`; thread to `ClientSession.call_tool` + `connect()` |
| G11 | 3 | MCP sampling caps absent | Add `MCPSamplingCaps` dataclass (max_tokens_cap / max_rpm / max_tool_rounds / allowed_models); enforce in `make_sampling_callback` |
| G12 | 4 | Hermes-AS-MCP missing `conversations_*` aliases | Add `conversations_list` / `conversation_get` as second `@server.tool()` decorators sharing the body |
| G13 | 4 | No `permissions_list_open` | Query `ConsentStore` for OPEN/unanswered requests; return list of pending capability requests |
| G14 | 4 | `events_poll(after_cursor=…)` arg name + missing approval events | Accept `after_cursor` alias for `since_message_id`; surface `approval_requested` / `approval_resolved` event types from F1 audit_log |
| G15 | 5 | ACP `agent.json` not auto-bundled | On first `oc acp serve`, ensure `~/.opencomputer/<profile>/acp_registry/agent.json` exists |

## 3. Non-Goals (explicit YAGNI)

- Full OpenAI Files API (`file_id`) — already deliberate per existing 400-on-`file_id` policy.
- Renaming OC tool names to spec names (only adding aliases).
- Per-MCP-server `log_level` config (no caller, dropped from sampling caps).
- Web-UI for editing CORS origins — env-var driven only.

## 4. Architecture

**No new modules.** Surgical edits across:

- `extensions/api-server/adapter.py` — CORS middleware, Idempotency middleware, ungate /v1/responses, GET/DELETE responses routes, /v1/health alias, env-var alias resolution.
- `extensions/api-server/plugin.py` — `API_SERVER_KEY` + `API_SERVER_ENABLED` env handling.
- `opencomputer/mcp/client.py` — register `mcp_<server>_<tool>` alias tool; honor per-server timeouts; honor `prompts_enabled` / `resources_enabled`; enforce sampling caps.
- `opencomputer/mcp/sampling.py` — accept caps, enforce per-call.
- `opencomputer/agent/config.py` — extend `MCPServerConfig` (4 fields) + `MCPConfig` (1 caps dataclass); back-compat parsing for nested `tools.include/exclude` shape.
- `opencomputer/mcp/server.py` — add `conversations_list`/`conversation_get`/`permissions_list_open` tools; widen `events_poll` signature; emit approval events.
- `opencomputer/cli.py` — `acp serve` ensures `agent.json` at canonical path.

## 5. Key data shape changes

```python
# opencomputer/agent/config.py — additions to MCPServerConfig
@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    # ... existing fields ...
    prompts_enabled: bool = True              # G9
    resources_enabled: bool = True            # G9
    timeout: float = 30.0                     # G10 — per-tool-call cap
    connect_timeout: float = 30.0             # G10 — initial connect cap

@dataclass(frozen=True, slots=True)
class MCPSamplingCaps:                        # G11
    max_tokens_cap: int = 4096
    max_rpm: int = 60
    max_tool_rounds: int = 5
    allowed_models: tuple[str, ...] = ()      # () = no restriction
```

## 6. CORS middleware behavior (G1)

```python
# extensions/api-server/adapter.py
def _build_app() -> web.Application:
    app = web.Application(middlewares=[_idempotency_mw, _cors_mw])
    # ... routes ...
    origins = [o.strip() for o in os.environ.get("API_SERVER_CORS_ORIGINS", "").split(",") if o.strip()]
    if origins:
        cors = aiohttp_cors.setup(app, defaults={
            o: aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers=("Authorization", "Content-Type", "Idempotency-Key"),
                max_age=600,
            )
            for o in origins
        })
        for route in list(app.router.routes()):
            cors.add(route)
    return app
```

## 7. Idempotency-Key middleware (G2)

```python
@web.middleware
async def _idempotency_mw(request, handler):
    key = request.headers.get("Idempotency-Key")
    if not key or request.method != "POST":
        return await handler(request)
    body = await request.read()
    cache_key = (
        hashlib.sha256(request.headers.get("Authorization", "").encode()).hexdigest()[:16],
        key,
        hashlib.sha256(body).hexdigest()[:16],
    )
    cached = _IDEMPOTENCY_CACHE.get(cache_key)
    if cached and cached.expires > time.monotonic():
        return web.Response(
            body=cached.body, status=cached.status,
            headers={**cached.headers, "X-Idempotent-Replay": "1"},
        )
    request = request.clone(rel_url=request.rel_url)  # re-readable body
    response = await handler(request)
    _IDEMPOTENCY_CACHE[cache_key] = _CachedResponse(
        body=response.body, status=response.status, headers=dict(response.headers),
        expires=time.monotonic() + 300.0,
    )
    return response
```

## 8. MCP tool-name alias (G8)

```python
# opencomputer/mcp/client.py — when registering MCP tool, also register alias
canonical_name = f"{server_name}__{tool_name}"          # legacy OC name
hermes_alias = f"mcp_{server_name}_{tool_name}"         # Hermes spec name
# Both are MCPTool instances pointing at same dispatch path; no schema collision
# because each is a unique schema_name in the registry.
```

## 9. permissions_list_open (G13)

OPEN approval request = a ConsentRequest emitted by F1 that has no matching ConsentGrant or revocation. Query path:

```python
# opencomputer/mcp/server.py
@server.tool()
def permissions_list_open(limit: int = 50) -> list[dict[str, Any]]:
    """List capabilities currently awaiting user decision (Hermes parity)."""
    db = sqlite3.connect(str(_home() / "sessions.db"))
    rows = db.execute(
        "SELECT capability_id, scope, requested_at, requested_by "
        "FROM consent_requests WHERE state = 'pending' "
        "ORDER BY requested_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [{"capability_id": r[0], "scope": r[1], "requested_at": r[2], "requested_by": r[3]} for r in rows]
```

If `consent_requests` table doesn't exist, return empty list (back-compat).

## 10. Failure modes (audit map)

- CORS misconfigured → preflight 403 (default origins=[] keeps off)
- Idempotency wrong dedup → mitigated by full body-hash in cache key
- Alias collision → log + skip (defensive)
- permissions_list_open missing table → return []
- agent.json wrong path → `oc acp manifest --print` shows where
- Sampling caps exceed → return MCP error code -32603 with "rpm exceeded"

## 11. Tests (new — minimum 15)

| File | Covers |
|---|---|
| tests/test_api_server_cors.py | G1 — CORS preflight, allowed headers, max-age |
| tests/test_api_server_idempotency.py | G2 — replay, body-hash collision, TTL |
| tests/test_api_server_responses_default_on.py | G3 — /v1/responses works without env |
| tests/test_api_server_responses_get_delete.py | G4 — GET/DELETE response by id |
| tests/test_api_server_health_v1.py | G5 — /v1/health 200 |
| tests/test_api_server_env_aliases.py | G6 + G7 — KEY + ENABLED |
| tests/test_mcp_tool_naming_alias.py | G8 — both names register, same dispatch |
| tests/test_mcp_per_server_filters_v2.py | G9 — prompts/resources off |
| tests/test_mcp_per_server_timeout.py | G10 — timeout / connect_timeout applied |
| tests/test_mcp_sampling_caps.py | G11 — token cap, rpm, tool rounds, allowed models |
| tests/test_mcp_server_conversation_aliases.py | G12 — aliases callable |
| tests/test_mcp_server_permissions_list_open.py | G13 — pending-only query |
| tests/test_mcp_server_events_poll_aliases.py | G14 — after_cursor + approval events |
| tests/test_acp_agent_json_autobundle.py | G15 — agent.json appears at canonical path |

## 12. Self-audit verdict (9-lens, applied pre-plan)

| Lens | Finding |
|---|---|
| Assumption-check | grep-verified absence of every gap |
| Architecture stress | ToolRegistry uniqueness — alias = wrapper-tool, not duplicate |
| Alternative dismissal | Considered rename — rejected (shipped tests + docs) |
| Requirement gap | agent.json path = `~/.opencomputer/<profile>/acp_registry/`; CORS preflight needs OPTIONS verb |
| Composability | All 4 areas independent; CORS+Idempotency middlewares apply to all routes |
| Scope honesty | Each gap < 100 LOC, 15 gaps total ~10-12 hr |
| API surface drift | Aliases forward-compat, nested filter parses to flat back-compat |
| Failure mode map | All 6 failure modes have mitigations |
| YAGNI sweep | Dropped `log_level` per-server (no caller); kept caps with cost+security value |

**Risks accepted:** none unmitigated.
