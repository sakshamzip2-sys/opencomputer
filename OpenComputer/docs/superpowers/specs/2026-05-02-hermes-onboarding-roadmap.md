# Hermes-Onboarding Port — Roadmap & Status

**Date:** 2026-05-02
**Status:** Living document — update as sub-projects ship

This is the single source of truth for what's done, what's pending,
and why. Updated as PRs merge.

---

## Status snapshot (updated 2026-05-02 — final round of session)

**Wizard:** 8/8 sections LIVE, all titles + defaults aligned with Hermes,
OAuth-token storage foundation in place, Configuration Summary block
at end, `oc setup --new --non-interactive` flag for CI. Polish items
shipped: banner update-check display, provider construction test
after key save, OpenClaw migration preview phase.

**Providers in wizard menu (22 total):**
- 5 original (anthropic, openai, openrouter, gemini, aws-bedrock)
- 17 OpenAI-compatible (deepseek, xai, zai, kimi, dashscope,
  tencent, nvidia, huggingface, stepfun, arcee, ollama-cloud, gmi,
  kilo, opencode-zen, opencode-go, ai-gateway, xiaomi, kimi-cn,
  alibaba-coding-plan, azure-foundry)
- 2 Anthropic-shaped (minimax, minimax-cn) — added in M.b

**Channel adapters in extensions/:** **18 working** as of this update:

Original 12: telegram, discord, slack, matrix, mattermost, whatsapp,
signal, email, webhook, sms, homeassistant, **imessage** (BlueBubbles).

Shipped this session (6):
- **irc** (RFC 1459, full asyncio client; ~250 LOC)
- **teams** (Microsoft Teams via Incoming Webhook; outbound)
- **dingtalk** (Custom Robot webhook + HMAC-SHA256 signing; outbound)
- **feishu** (Lark Custom Robot webhook + signing; outbound)
- **wecom-callback** (Group Chat Bot webhook; outbound)
- **yuanbao** (Tencent Yuanbao webhook + Bearer auth; outbound)

**Still pending (2 channels):**
- **Weixin (公众号 / WeChat)** — Customer Service Message API needs
  access_token rotation (every 2 hours) + AppID/AppSecret pair. ~250 LOC
  with token caching; deferred to a focused PR.
- **QQ Bot** — Tencent QQ Bot Open API uses OAuth + reverse-WebSocket.
  Different protocol surface than the other Chinese platforms.
  ~400 LOC; deferred to a focused PR.

---

## Shipped (PRs #288-#326)

| PR | Sub-project | Summary |
|---|---|---|
| #288 | env-loader fix | Restores global ~/.opencomputer/.env when profile-leaf is sticky |
| #290 | F0+F1+F2 | Foundation — menu primitives, section orchestrator, welcome banner |
| #292-298 | S1+S5+M1+S4+S2+S3 + CLI wire | All 8 wizard sections LIVE + `oc setup --new` CLI flag |
| #299-302 | P1.a-P1.d | First 16 OpenAI-compatible providers |
| #303 | P1.fixes | Hermes-source alignment for 11 misaligned providers + remove broken MiniMax |
| #304 | section-audit + P1.e | Section title alignment + Vercel + Xiaomi |
| #305 | Q3 | Configuration Summary block at end of wizard |
| #307 | P | API key entry flow in inference_provider |
| #308 | T | Per-platform credential entry flow in messaging_platforms |
| #309 | O | OAuth token store foundation (deferred device-code flow) |
| #310 | Q1+Q2 | Reconfigure detection + --non-interactive flag |
| #311 | M.a | Regional variants (Kimi China + Alibaba Coding Plan) |
| #312 | docs | Initial roadmap doc |
| **#313** | **M.b** | **MiniMax + MiniMax China (Anthropic-shaped subclass)** |
| **#314** | **M.c** | **Azure AI Foundry (OpenAI-style)** |
| **#315** | **polish** | **Banner update-check + provider test + OpenClaw preview** |
| #316 | docs | Roadmap update + BlueBubbles correction |
| **#319** | **O.b** | **Device-code OAuth flow (RFC 8628) + Nous Portal provider** |
| **#321** | **C.irc** | **IRC channel adapter (full RFC 1459 asyncio client)** |
| **#322** | **C.teams** | **Microsoft Teams adapter (Incoming Webhook outbound)** |
| **#324** | **C.dingtalk** | **DingTalk adapter (Custom Robot webhook + HMAC)** |
| **#325** | **C.feishu+wecom** | **Feishu + WeCom-Callback adapters (webhook outbound)** |
| **#326** | **C.yuanbao** | **Tencent Yuanbao adapter (webhook + Bearer auth)** |

**Numbers (final):** ~28 PRs, ~13,000 LOC, ~300 new tests, full pytest
passing, ruff clean throughout.

---

## Pending

### ~~C~~ — Channel adapter gap-fill — Shipped

| Platform | Protocol | Status | PR |
|---|---|---|---|
| ~~IRC~~ | RFC 1459 | ✓ Shipped | #321 |
| ~~Microsoft Teams~~ | Incoming Webhook (outbound) | ✓ Shipped | #322 |
| ~~BlueBubbles (iMessage)~~ | already shipped | ✓ See `extensions/imessage/` | — |
| ~~DingTalk~~ | Custom Robot webhook + HMAC | ✓ Shipped | #324 |
| ~~Feishu / Lark~~ | Custom Robot webhook + signing | ✓ Shipped | #325 |
| ~~WeCom Callback~~ | Group Chat Bot webhook | ✓ Shipped | #325 |
| ~~Yuanbao~~ | webhook + Bearer auth | ✓ Shipped | #326 |
| ~~Weixin / WeChat~~ | Public account REST + access_token rotation | ✓ Shipped | #344 |
| ~~WeCom (full)~~ | corp+agent+secret + access_token rotation | ✓ Shipped (outbound) | #346 |
| ~~QQ Bot~~ | bots.qq.com bot token + REST | ✓ Shipped (outbound) | #347 |
| ~~Webhook Inbound (Teams/DingTalk/Feishu)~~ | aiohttp listener + per-platform HMAC | ✓ Shipped | #342 |

**Outbound-only paths still:**
- WeCom-Callback (Group Chat Bot) — platform has no inbound model
- Yuanbao — platform has no inbound model
- WeCom (full) — encrypted callback (WXBizMsgCrypt AES-256-CBC) is a
  focused follow-up; outbound shipped
- QQ Bot — WebSocket gateway (Identify/Heartbeat/Resume state machine)
  is a focused follow-up; outbound shipped
- Weixin — XML callback signature + dispatch is a focused follow-up;
  outbound + plain-text inbound primitives shipped

### O.b — OAuth device-code flow + provider plugins

**Shipped foundation (PR #319):**
- ✓ `opencomputer/auth/token_store.py` — JSON-backed token persistence
- ✓ `opencomputer/auth/device_code.py` — RFC 8628 generic flow client
  (`request_device_code` + `poll_for_token` + `to_oauth_token`).
  Honors authorization_pending, slow_down (+5s interval), expired_token,
  access_denied, request timeout.
- ✓ `extensions/nous-portal-provider/` — first OAuth-backed provider
  plugin. Subclasses OpenAIProvider; token resolution order is
  `NOUS_PORTAL_API_KEY` env → auth token store. Includes
  `run_device_code_login` driving the full flow.

**Shipped follow-ups (this session, PRs #331-#334):**
- ✓ **GitHub Copilot** (PR #331) — reuses the user's `gh` CLI token via
  `~/.config/gh/hosts.yml` parser, env-var fallbacks
  (`COPILOT_GITHUB_TOKEN`/`GH_TOKEN`/`GITHUB_TOKEN`). 13 tests.
- ✓ **Qwen OAuth** (PR #332) — reads/refreshes credentials at
  `~/.qwen/oauth_creds.json`; refresh-on-expiry via
  `chat.qwen.ai/api/v1/oauth2/token`; falls back to `QWEN_API_KEY`
  env var. Persists rotated tokens back to disk. 15 tests.
- ✓ **`opencomputer/auth/external.py`** (PR #333) — generic
  browser-redirect helpers: `PKCEPair`/`generate_pkce_pair()`
  (RFC 7636 S256), `validate_redirect_uri()` (loopback only),
  `wait_for_redirect_callback()` (one-shot HTTPServer on a daemon
  thread), `open_url()`. Modeled on Hermes's `_spotify_wait_for_callback`
  but extracted as provider-agnostic primitives. 12 tests.
- ✓ **Google OAuth flow + Gemini OAuth provider** (PR #334) —
  `opencomputer/auth/google_oauth.py` ships the full PKCE login,
  refresh, and credential persistence (`~/.opencomputer/auth/google_oauth.json`,
  chmod 0600). The `gemini-oauth` provider plugin reads/refreshes
  via the auth module. **Honest deferral:** the Cloud Code Assist
  transport adapter (`cloudcode-pa.googleapis.com/v1internal:*`) is a
  pending follow-up — `complete()` raises `NotImplementedError` with
  guidance until the adapter ships. 25 tests (18 google_oauth + 7
  gemini-oauth).

**Shipped follow-ups (latest round):**
- ✓ **Cloud Code Assist transport adapter** (PR #340) — Gemini OAuth
  now actually performs inference. Ports Hermes's
  `gemini_cloudcode_adapter` + `google_code_assist`: project resolution
  via env / loadCodeAssist / onboardUser, generateContent +
  streamGenerateContent SSE, message translation (functionCall/Response
  + thoughtSignature sentinel), JSON-Schema sanitization. 37 tests.
- ✓ **GitHub Copilot ACP** (PR #348) — auth/discovery scaffold ships;
  validates `copilot` CLI is installed, env-overrides for command path,
  wizard discovery. JSON-RPC-over-stdio transport stub raises
  `NotImplementedError` pointing at the REST `copilot` provider as
  today's workaround. 10 tests.

**Still pending:**
- **GitHub Copilot ACP transport** (~400 LOC) — JSON-RPC framing over
  stdio, subprocess supervisor, stream-event mapping.
- **Nous Portal real client_id** — currently defaults to `opencomputer-cli`
  (placeholder). Parked until users complain. Reopen by registering OC
  with Nous Portal OR documenting `NOUS_PORTAL_CLIENT_ID` for users
  bringing their own.

### ~~M.b~~ — Shipped in PR #313

Anthropic-shaped MiniMax + MiniMax China subclass the bundled
`anthropic-provider`. AnthropicProvider already supported configurable
base_url + _api_key_env, so subclasses just override 3 class attrs and
pre-validate env-var-not-set in __init__ for proper error messages.

### ~~M.c~~ — Shipped (full — both OpenAI-style and Anthropic-style)

OpenAI-style Azure Foundry deployments shipped in PR #314 (subclass
of OpenAIProvider with required AZURE_FOUNDRY_BASE_URL).

Anthropic-style support shipped in PR #336:
- ✓ `ModelConfig.api_mode` field (default `"auto"`, accepts
  `"openai"`/`"anthropic"`) — validated in `__post_init__`, hashable.
- ✓ `_resolve_provider(name, api_mode=...)` threads api_mode into the
  provider's `__init__` when its signature accepts the kwarg
  (inspect-based gate; backward-compatible across all 23 providers).
- ✓ `AzureFoundryProvider` rewritten as a `__new__` dispatcher:
  `api_mode="anthropic"` returns a wrapper around `AnthropicProvider`
  configured with `auth_mode="bearer"` (lazy import). The
  Anthropic SDK only loads when actually selected.
- ✓ `AZURE_FOUNDRY_API_MODE` env var honored when no kwarg passed.

13 tests (test_api_mode.py); 7069 total tests pass after the change.

### ~~Polish~~ — Shipped in PR #315

All three polish items shipped:
- ✓ Banner update-check display via `cli_update_check.get_update_hint()`
- ✓ Provider construction test after key save (best-effort `__init__()`
  call to catch wrong-key-shape / missing-dep issues; doesn't make
  network calls)
- ✓ OpenClaw migration preview phase (dry-run lists fresh files vs
  files that would land at `<name>.imported`, plus skill counts)

---

## Architecture decisions worth remembering

1. **Manifest is source of truth for env_vars + signup_url** — wizard
   reads the plugin's `setup.providers[*]` / `setup.channels[*]` to
   know what to prompt for. New plugins automatically appear in
   wizard menus by declaring these fields. No core code changes
   needed per provider.

2. **Cross-checking against Hermes source caught 11 of 16 providers
   misaligned** (PR #303). The lesson: when porting a manifest schema
   from another project, diff against the source — don't extrapolate.
   Tests passing internal consistency don't catch external-truth drift.

3. **MiniMax + Azure Foundry don't fit OpenAIProvider subclass** because
   they use Anthropic-shaped requests. Extending anthropic-provider for
   custom base_url is the path forward. Don't try to shim them through
   the OpenAI client.

4. **OAuth client_id is project-specific.** Hermes uses `hermes-cli` for
   Nous Portal — that's *their* registration. OC needs its own
   registrations or env-var overrides. This is the gating item for
   shipping working OAuth providers.

5. **GH Actions billing limits hit during this work** — the escape
   hatch is admin-merge with local pytest verification, documented in
   the commit. CI is a guard; absence of CI doesn't mean missing
   coverage if local runs are clean and reproducible.

---

## How to use this roadmap

When picking up this work in a future session:

1. Read this doc first.
2. Check `git log --oneline | head -20` to see what shipped recently.
3. Pick the next item from the appropriate section above.
4. Each item names files + estimated size + complexity drivers.
5. Update this doc as you ship: move from "Pending" to "Shipped"
   with PR link.
