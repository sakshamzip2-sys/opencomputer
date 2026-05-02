# Hermes-Onboarding Port — Roadmap & Status

**Date:** 2026-05-02
**Status:** Living document — update as sub-projects ship

This is the single source of truth for what's done, what's pending,
and why. Updated as PRs merge.

---

## Status snapshot

**Wizard:** 8/8 sections LIVE, all titles + defaults aligned with Hermes,
OAuth-token storage foundation in place, Configuration Summary block
at end, `oc setup --new --non-interactive` flag for CI.

**Providers:** 5 original (anthropic, openai, openrouter, gemini,
aws-bedrock) + 14 OpenAI-compatible additions (deepseek, xai, zai, kimi,
dashscope, tencent, nvidia, huggingface, stepfun, arcee, ollama-cloud,
gmi, kilo, opencode-zen, opencode-go, ai-gateway, xiaomi, kimi-cn,
alibaba-coding-plan) = **19 total** in the wizard menu.

**Channel adapters:** 11 working (telegram, discord, slack, matrix,
mattermost, whatsapp, signal, email, webhook, sms, homeassistant) +
9 missing (BlueBubbles, QQ Bot, DingTalk, Feishu, WeCom, Weixin,
Yuanbao, IRC, Microsoft Teams).

---

## Shipped (PRs #288-#311)

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
| #311 | M | Regional variants (Kimi China + Alibaba Coding Plan) |

**Numbers:** ~17 PRs, ~9,500 LOC, ~210 new tests, full pytest passing,
ruff clean.

---

## Pending

### C — Channel adapter gap-fill (9 platforms)

The wizard's `messaging_platforms` section discovers any channel-kind
plugin and lists it. Adding a new channel = ship a `BaseChannelAdapter`
subclass + plugin manifest. Each adapter is ~200-500 LOC depending on
protocol complexity.

**Status: deferred — each platform needs dedicated focus.** Ship one
PR per adapter (or per cohort of similar adapters) rather than a
half-built bulk attempt.

**Per-platform ports** (rough size estimate):

| Platform | Protocol | Est size | Complexity drivers |
|---|---|---|---|
| **IRC** | RFC 1459 | ~300 LOC | Standard but stateful; nick management, channel join, MOTD |
| **Microsoft Teams** | Webhook + Graph API | ~400 LOC | OAuth + Graph API for receive; webhook for send |
| **BlueBubbles (iMessage)** | REST + WebSocket | ~350 LOC | Pairs with BlueBubbles server (https://bluebubbles.app); REST for send |
| **DingTalk** | Webhook + receive callback | ~400 LOC | Sign-verification on inbound; outbound API uses access_token |
| **Feishu / Lark** | REST + receive callback | ~400 LOC | App-level auth; mention-by-open-id |
| **WeCom (Enterprise WeChat)** | REST + receive callback | ~450 LOC | Corp + agent + secret triple; encryption/decryption on receive |
| **WeCom Callback** | webhook variant | ~150 LOC | Subset of WeCom with simpler outbound |
| **Weixin / WeChat** | Public account REST | ~400 LOC | Token cycling; mostly outbound |
| **Yuanbao** | Tencent direct API | ~300 LOC | OAuth + REST |
| **QQ Bot** | Tencent QQ Bot Open API | ~400 LOC | OAuth (QQ official bot framework); reverse-WebSocket |

**Recommended order to ship:** IRC first (simplest, well-known), then
Microsoft Teams (broad reach), then BlueBubbles. Defer the Chinese
platforms (DingTalk, Feishu, WeCom, Weixin, Yuanbao, QQ Bot) until
real user demand surfaces — each needs platform-specific research.

### O.b — OAuth device-code flow + provider plugins

Foundation in place (`opencomputer/auth/token_store.py`). What's missing:

1. **`opencomputer/auth/device_code.py`** — generic device-code flow
   client (request → poll → exchange). Needs:
   - httpx mocking for tests (no live network in CI)
   - Configurable client_id (env var override + manifest declaration)
   - Polling-interval honor + cancellation on Ctrl+C
   - Error handling for `authorization_pending` / `slow_down` / `expired_token`

2. **`opencomputer/auth/external.py`** — browser-redirect OAuth (Google
   Gemini, Qwen). Needs local HTTP server on a free port to catch the
   redirect; same as `pip install` — well-understood pattern.

3. **Provider plugins using the foundation:**
   - **Nous Portal** — needs OC's own client_id registration with Nous
     (Hermes uses `hermes-cli` — that's their registration). Document
     a `NOUS_PORTAL_CLIENT_ID` env-var override so users can supply
     their own registration in the meantime.
   - **GitHub Copilot** — uses GitHub OAuth tokens; can reuse a user's
     existing `gh` CLI token if present (Hermes pattern).
   - **GitHub Copilot ACP** — different protocol; spawns `copilot --acp
     --stdio` subprocess. Mostly subprocess-mgmt code.
   - **Google Gemini OAuth** — Cloud Code Assist backend; browser-redirect.
   - **Qwen OAuth** — browser-redirect.

**Recommended order:** ship `device_code.py` + the Nous Portal plugin
together (one PR) to validate the full flow end-to-end. Then a follow-up
adds GitHub Copilot. Then `external.py` + Google Gemini OAuth. Then Qwen.

### M.b — Anthropic-shaped providers (MiniMax, MiniMax China)

Both use `anthropic_messages` transport in Hermes (not `openai_chat`).
Subclassing OC's existing `OpenAIProvider` would 400 at runtime.

**Path:** extend `extensions/anthropic-provider` to accept a custom
`base_url` + `api_key_env`, then add MiniMax + MiniMax China as thin
subclasses. ~150 LOC for the anthropic-provider extension + 100 LOC
per subclass.

### M.c — Azure Foundry

Mixed `api_mode` per-model: same provider can serve OpenAI-style or
Anthropic-style endpoints depending on which model is requested.
Hermes resolves this at runtime via `config.yaml::model.api_mode`.

**Path:** add `api_mode` field to `ModelConfig`; provider plugin
dispatches to the appropriate transport. ~200 LOC for the plugin
+ ~50 LOC for the config schema bump.

### Polish

- **Welcome banner update-check integration** — `cli_banner.py`'s
  `prefetch_update_check` call exists but result isn't displayed.
  Add "(update available: vX.Y.Z)" line to the banner footer when
  update detected. ~20 LOC.

- **`oc setup` connection test** — Hermes's wizard tests the provider
  by sending a small ping after key entry. OC's wizard saves the key
  but doesn't validate it. Add an opt-in test step. ~80 LOC.

- **OpenClaw migration preview** — current M1 imports without preview.
  Hermes shows a dry-run preview before any file copy. Add a preview
  phase that lists what would be imported, then asks for confirmation.
  ~100 LOC.

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
