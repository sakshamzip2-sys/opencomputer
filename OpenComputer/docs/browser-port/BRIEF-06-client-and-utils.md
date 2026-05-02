# BRIEF — `client/` + `tools.py` + `plugin.py` + e2e (Wave W3)

> Client transport, session tab registry, the single `Browser` tool, deprecation shims, plugin entry, end-to-end integration tests. **The wiring wave.**
> Deep dive: [06-client-and-utils.md](../refs/openclaw/browser/06-client-and-utils.md) (703 lines — read end-to-end).

## What to build

### `extensions/browser-control/client/`

| File | Public API |
|---|---|
| `fetch.py` | `async def fetch_browser_json(method, path_or_url, *, body, headers, timeout, auth) -> dict` — **dual transport**: if `path_or_url` is absolute HTTP, use httpx; if path-only, route through `server.dispatcher.dispatch_browser_control_request` (in-process) |
| `auth.py` | `def is_loopback_host(url: str) -> bool` · `def inject_auth_headers(headers: dict, *, auth: BrowserAuth, url: str) -> dict` — only injects on loopback |
| `actions.py` | `class BrowserActions`: ~38 method wrappers around the HTTP routes (browser_status / browser_start / browser_stop / browser_navigate / browser_act / browser_snapshot / browser_screenshot / etc.) |
| `form_fields.py` | `def normalize_form_field(field: dict) -> dict` — type coercion, validation. **Does NOT do array coercion** (deep pass corrected first-pass error). |
| `proxy_files.py` | `async def persist_proxy_files(files: list[dict]) -> dict[str, str]` (decode + write) · `def apply_proxy_paths(result: Any, mapping: dict[str, str]) -> None` — **recursive walker** (fix OpenClaw's shallow-walk bug) |
| `tab_registry.py` | `def track_session_browser_tab(*, session_key, target_id, base_url, profile) -> str` · `def untrack_session_browser_tab(...)` · `async def close_tracked_browser_tabs_for_sessions(session_keys: list[str]) -> int` — **delete-from-map-first, then close** ordering |

### `extensions/browser-control/` top level

| File | Public API |
|---|---|
| `tools.py` | `class Browser(BaseTool)` (the one discriminator tool) + 11 deprecation shims (`BrowserNavigate`, `BrowserClick`, `BrowserFill`, `BrowserSnapshot`, `BrowserScrape`, plus the 6 Hermes-parity names from existing `tools.py`) — each shim emits a one-time-per-process warning + dispatches to `Browser` |
| `schema.py` | pydantic models for `BrowserParams` + `ActRequest` (flat, OpenAI-compatible — see BLUEPRINT §5) |
| `plugin.py` | `def register(api: PluginApi) -> None` — registers `Browser` + the deprecation shims, registers the control service, registers a doctor row |

### `extensions/browser-control/tests/test_e2e_*.py`

End-to-end integration: launch managed Chrome via `chrome/`, attach via `session/`, navigate to a fixture HTML, snapshot, click a button, fill a form, screenshot, close. Roundtrips both transport paths.

## What to read first

1. The deep dive's **3 top-level corrections to the skeleton** — dual transport, no typed error hierarchy on client, no `Retry-After` in rate-limit. Internalize these *before* writing.
2. The complete client API surface table (~38 endpoints).
3. The session tab registry algorithms — composite NUL-separated keys, delete-from-map-first ordering.
4. The utility modules in depth (paths.ts strict policy, no fsync, buggy trash, shallow-walk).

## Acceptance

- [ ] `fetch_browser_json` correctly routes absolute URLs to httpx and path-only URLs to the dispatcher (test both paths)
- [ ] Auth headers attached ONLY on loopback URLs (test: non-loopback URL gets no auth header even if auth is configured)
- [ ] No retry on 4xx/5xx (test: 401 doesn't retry, 500 doesn't retry); only retry on connection-refused
- [ ] Rate-limit (429) gets the friendly hint message; `Retry-After` is **NOT** consulted (matches OpenClaw)
- [ ] Tab registry: `close_tracked_browser_tabs_for_sessions` removes from map atomically before issuing closes; "tab already closed" is swallowed
- [ ] `apply_proxy_paths` recurses through deeply-nested result trees (test with 4-level nesting)
- [ ] `Browser` tool (the discriminator one) accepts all 16 actions and dispatches correctly
- [ ] Each deprecation shim emits a `DeprecationWarning` ONCE per process, then calls through to `Browser`
- [ ] `plugin.py:register(api)` registers exactly: 1 Browser tool + N shim tools + 1 doctor row
- [ ] Doctor row checks: playwright installed, chromium downloaded, control port reachable
- [ ] e2e test: full flow against local fixture HTML works end to end (`pytest tests/test_e2e_*.py`)
- [ ] No imports from `opencomputer/*` in `client/`, `tools.py`, `schema.py`. `plugin.py` may import from `opencomputer/tools/registry` for `BaseTool` (TBD — verify which package owns BaseTool).
- [ ] `ruff check` clean

## Do NOT reproduce

| OpenClaw bug | Don't do |
|---|---|
| `proxy-files.ts` shallow-walks the result tree | Recurse properly. Test with deeply-nested structure. |
| `output-atomic.ts` skips fsync | Use `_utils/atomic_write.py` (fsync built in) |
| `trash.ts` Linux fallback is buggy | Use `send2trash` library |
| Typed client error hierarchy (`AuthError`, `PolicyError`, `ConflictError`, …) | These don't exist on the client side in OpenClaw; first-pass got it wrong. Use `BrowserServiceError` + plain `Error`. Only 429 is special. |
| `Retry-After` parsing for rate-limit messages | Doesn't happen in OpenClaw. Use the response body's hint message. |

## Implementation gotchas

- **`isinstance(x, BaseTool)` SDK boundary check** — `tools.py` MUST import `BaseTool` from `plugin_sdk.tool_contract`, NOT from any `opencomputer/*` module. The boundary test will catch it but better not to wave at the wall.
- **Deprecation shim warning suppression**: use `warnings.warn(..., DeprecationWarning)` plus a module-level `_emitted: set[str]` to dedupe per process.
- **`session_key`** for the tab registry — does OpenComputer's existing session DB ([opencomputer/agent/state.py](../../opencomputer/agent/state.py)) already provide one? Check before inventing a new id.
- **Doctor row format** — match what other plugins do; check [extensions/anthropic-provider/](../../extensions/anthropic-provider/) for the pattern.
- **httpx event hooks**: use `event_hooks={"request": [auth_inject], "response": [error_translate]}` for clean separation.

## Open questions

- Where does `BaseTool` live in OpenComputer's current SDK — `plugin_sdk.tool_contract`? Verify before importing.
- Existing session id source — does `agent/state.py` expose a session_key, or do we generate one fresh? Recommend: reuse if it exists; else compose from `(profile, session_uuid)`.
- e2e test fixture: do we ship a tiny static HTML file in `tests/fixtures/`, or use a public URL? Recommend **local fixture** — deterministic, offline-friendly, no network flakes.

## Where to ask

PR description with `**Question:**` line. This wave touches the most existing OpenComputer surface (plugin registration, BaseTool, session id) — flag any uncertainty about how to integrate cleanly.
