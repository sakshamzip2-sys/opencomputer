# BRIEF — `server/` + `server_context/` (Waves W1c + W2b)

> The HTTP control surface (~46 routes), auth + CSRF + SSRF perimeter, lifecycle ordering, and the orchestrator state holder.
> Deep dive: [05-server-and-auth.md](../refs/openclaw/browser/05-server-and-auth.md) (971 lines — read end-to-end).
> **This is the security perimeter. Don't take shortcuts.**

## What to build

### `extensions/browser-control/server_context/` (W1c — independent, can start alongside session/)

| File | Public API |
|---|---|
| `state.py` | `@dataclass class BrowserServerState(profiles: dict[str, ProfileRuntimeState], port: int, ...)` · `@dataclass class ProfileRuntimeState(profile, browser, context, last_target_id: str \| None, role_refs_by_target: dict, chrome_proc, ...)` |
| `lifecycle.py` | `async def ensure_profile_running(state, profile_name) -> ProfileRuntimeState` · `async def teardown_profile(state, profile_name) -> None` |
| `selection.py` | `def select_target_id(profile_state, requested: str \| None) -> str` — sticky `last_target_id` fallback chain (requested → last → first available) |
| `tab_ops.py` | `async def open_tab(profile_state, url) -> TabInfo` · `async def focus_tab(profile_state, target_id) -> None` · `async def close_tab(profile_state, target_id) -> None` |

### `extensions/browser-control/server/` (W2b — depends on session, snapshot, server_context)

| File | Public API |
|---|---|
| `app.py` | `def create_app(state: BrowserServerState, *, auth: BrowserAuth) -> FastAPI` |
| `auth.py` | `class BrowserAuth(token: str \| None, password: str \| None)` · `async def ensure_browser_control_auth(config) -> BrowserAuth` (auto-generates token via `secrets.token_hex(24)` if not configured) · auth dependency for FastAPI routes |
| `csrf.py` | FastAPI middleware: validates `Sec-Fetch-Site` → `Origin` → `Referer` chain on mutating verbs; loopback-only |
| `middleware.py` | abort-signal propagation, JSON body parser, rate-limit message construction |
| `lifecycle.py` | `async def start_browser_control_server(config) -> BrowserServerState` (12 steps; see deep dive) · `async def stop_browser_control_server(state) -> None` (6 steps) |
| `dispatcher.py` | `async def dispatch_browser_control_request(method, path, *, body, profile, auth) -> Response` — **the in-process other half** of the dual transport. Same handler called by both HTTP routes and direct in-process callers. |
| `policy.py` | `def is_persistent_browser_profile_mutation(path: str) -> bool` · `def normalize_browser_request_path(path: str) -> str` — gates which profile can hit which route |
| `routes/basic.py` | `GET /` (status) · `POST /start` · `POST /stop` · `GET /profiles` · `POST /profiles/create` · `POST /reset-profile` · `DELETE /profiles/{name}` |
| `routes/tabs.py` | `GET /tabs` · `POST /tabs/open` · `POST /tabs/focus` · `DELETE /tabs/{target_id}` |
| `routes/agent.py` | `POST /navigate` · `POST /screenshot` · `POST /pdf` · `POST /snapshot` · `POST /act` · `POST /hooks/dialog` · `POST /hooks/file-chooser` |
| `routes/storage.py` | `GET/POST /storage/cookies` · `GET /storage/local` · `GET /storage/session` · etc. |
| `routes/observe.py` | `GET /console` · `GET /errors` · `GET /requests` · `POST /trace/start` · `POST /trace/stop` · `GET /debug` |

## What to read first

1. The full route table in the deep dive (~46 endpoints, grouped by router). **First-pass undercounted as ~32 — go by the deep pass count.**
2. The auth lifecycle: 4 mode carve-outs (test / password / SecretRef / trusted-proxy), bootstrap order, failure modes.
3. The CSRF chain: `Sec-Fetch-Site` → `Origin` → `Referer`, with `OPTIONS` bypass and `Origin: null` handling.
4. The SSRF nav-guard: 6-step pre-nav, 3-step post-nav, redirect-chain hostname pinning.
5. Lifecycle: 12 startup steps, 6 shutdown steps, with per-step failure modes.
6. The dispatcher walkthrough — proves both transports converge on the same handler.

## Acceptance

- [ ] All ~46 endpoints exist and respond
- [ ] Auth: token mode works (Bearer header); password mode works (X-OpenComputer-Password header); both use `hmac.compare_digest`
- [ ] Token auto-generates via `secrets.token_hex(24)` if not configured (and `OPENCOMPUTER_ENV != "test"`)
- [ ] CSRF blocks requests with `Sec-Fetch-Site: cross-site` on mutating verbs; allows on `same-origin`
- [ ] CSRF allows `OPTIONS` preflight regardless
- [ ] SSRF guard blocks `file://`, `chrome://`, private IPs (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16), and configurable host blocklist
- [ ] Hostname pinning catches a redirect that lands on an attacker-controlled host
- [ ] Dispatcher: a `POST /act` over HTTP and the equivalent in-process call return identical results (test both paths against the same fixture)
- [ ] Lifecycle: server binds to `127.0.0.1` ONLY (test: TCP connect from non-loopback IP fails). Auth registry per-port works.
- [ ] Profile-mutation gating: `existing-session` profile can't hit `/profiles/create` or `/reset-profile`
- [ ] Tests in `tests/test_server_*.py`. Cover: every route at minimum once; auth happy + sad paths; CSRF acceptance + rejection; SSRF block list; dispatcher dual-path equivalence.
- [ ] No imports from `opencomputer/*`

## Do NOT reproduce

| OpenClaw bug | Don't do |
|---|---|
| `?` in URL pattern matcher claimed but unimplemented | Drop from docstring or implement properly. Don't claim what's not there. |
| Pulling the gateway's CSRF model wholesale | OpenClaw's `gateway.auth.token` carve-outs include legacy/SecretRef paths that don't apply to OpenComputer. Default to a clean: token OR password, no other modes. |

## Implementation gotchas

- **FastAPI middleware ordering** is REVERSE of `add_middleware()` calls — easy to flip. Test the order explicitly.
- **`secrets.token_hex(24)` produces 48 hex chars = 24 bytes.** Matches OpenClaw's `crypto.randomBytes(24).toString("hex")`.
- **`hmac.compare_digest`** is the timing-safe compare. Don't `==` on tokens.
- **`ipaddress.ip_address(host).is_private`** handles IPv4 + IPv6 + link-local. Use `is_loopback` separately for `127.0.0.0/8`.
- **httpx redirect-chain inspection**: use `event_hooks={"response": [check_redirect]}` to validate intermediate responses for the post-nav re-validation.
- **CSRF on loopback only** — there's no off-the-shelf FastAPI plugin for this exact model. Hand-roll it (~30 LOC).

## Open questions

- Should bridge/control split exist for v0.1, or just the control server (no sandbox initially)? Recommend **just control** — sandbox is post-v0.2.
- Trace endpoints (`POST /trace/start`, `/trace/stop`) on day one or defer? Recommend **defer** — gate behind a config flag.
- Per-port auth registry (`bridge_auth_registry`) — only relevant if we ship the bridge server. If not, drop the registry.

## Where to ask

PR description with `**Question:**` line. **Especially** for anything in the security perimeter — flag uncertainty, don't guess.
