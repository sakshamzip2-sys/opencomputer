# Open Computer — Privacy, Data Locality, & Observability Audit

**Date:** 2026-04-28.
**Mode:** Read-only.
**Codebase:** `/Users/saksham/Vscode/claude/OpenComputer/`.
**Scope:** Every external network call, local-only guarantees, credential handling, log content + redaction + telemetry, user-facing inspection affordances, reset/deletion completeness, self-modification feedback loops, and a non-optional risk register. 

> **Headline:** Open Computer is privacy-by-default. The only unconditional egress is to the user-chosen LLM provider; all memory, search, channel, voice, and extension features are opt-in. No embedded telemetry (Sentry/PostHog/Mixpanel/Segment/Amplitude/Datadog all confirmed absent). Logs use HMAC-chained tamper-evident audit chains and seven secret-redaction regexes applied at format time. **The risk register at the end is non-optional and has 17 entries** — none are "the affect plan is broken," but several are real privacy or operational hazards independent of any forward planning.

---

## Data egress

Every external network call falls into one of seven categories. Each is enumerated with file:line, what is sent, and whether user content rides on the request.

### 1. LLM providers — *unconditional*, every turn

The only unconditional egress in the codebase. If you have no LLM provider configured, OpenComputer cannot run a turn.

| Provider | Path | What gets sent | Auth | Notes |
|----------|------|----------------|------|-------|
| **Anthropic** | `/extensions/anthropic-provider/provider.py:432-437` (complete), `:478-530` (stream), via `AsyncAnthropic` SDK | `{"model", "max_tokens", "messages": [...with tool_use blocks...], "system": <full assembled prompt>, "tools": [...]}`. **System prompt embeds**: MEMORY.md (4 KB), USER.md (2 KB), top-20 user_facts from F4 graph, persona overlay, SOUL.md, workspace context (100 KB/file), tool schemas. **Messages embed**: full conversation history. **Attachments**: base64-encoded images. | `ANTHROPIC_API_KEY` env or proxy via `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_MODE=bearer`. Comma-separated values trigger credential-pool rotation. | x-api-key strip hook on Bearer-proxy mode (lines 160-168, 242-249) |
| **OpenAI** | `/extensions/openai-provider/provider.py` | Same shape; OpenAI tool-calls JSON format | `OPENAI_API_KEY` + optional `OPENAI_BASE_URL` for compatible endpoints (Ollama, OpenRouter, etc.) | Pool-mode supported |
| **AWS Bedrock** | `/extensions/aws-bedrock-provider/provider.py:44, 69-90` | Converse API shape; same content | boto3 default credential chain (env, ~/.aws/credentials, IAM role) | Lazy boto3 import |

This is the unavoidable trust boundary. Everything in MEMORY.md, USER.md, SOUL.md, user_facts, persona overlay, workspace context, and the conversation transcript reaches the LLM provider.

### 2. Honcho memory provider — *opt-in*, default-on for the bundled provider, every Nth turn

Honcho is the **default `MemoryProvider`** (`config.py:136`). The provider's HTTP client speaks to a self-hosted Honcho instance.

| Path | What gets sent |
|------|----------------|
| `/extensions/memory-honcho/provider.py:216-225` | `POST {HONCHO_BASE_URL}/v1/messages` with `{"workspace": str, "host_key": str, "user": <user message>, "assistant": <assistant message>, "turn_index": int}` |
| `/extensions/memory-honcho/provider.py:210` | `POST {HONCHO_BASE_URL}/v1/context` for `prefetch()` |

- Default endpoint: `http://localhost:8000` (the docker-compose stack the user runs locally). `HONCHO_BASE_URL` overrides; if pointed at a remote Honcho, the same payloads go off-device.
- Cadence: `HONCHO_CONTEXT_CADENCE` (default 1, every turn) and `HONCHO_DIALECTIC_CADENCE` (default 3).
- Per-profile isolation: `HONCHO_HOST_KEY = "opencomputer.<profile_name>"` — each profile lives in a separate Honcho peer (`plugin.py:89-102`).
- AGPL-3.0 upstream image; user runs the stack themselves.

### 3. WebFetch tool — *opt-in*, on user/agent invocation

`/opencomputer/tools/web_fetch.py:85-150`. Fetches arbitrary URLs the agent decides to read.

- SSRF guard at `/opencomputer/security/url_safety.py` blocks RFC1918 (10/8, 172.16/12, 192.168/16), loopback (127/8), link-local (169.254/16, including the AWS IMDS at 169.254.169.254), and DNS-resolution failures. Each redirect Location is re-checked.
- User-Agent: `"OpenComputer/0.1 (+https://github.com/sakshamzip2-sys/opencomputer)"`.
- Body sent: nothing — just the URL request. **No user content** unless the URL itself contains it.

### 4. WebSearch tool — *opt-in*, on user/agent invocation

`/opencomputer/tools/web_search.py:108-150`. Routes through pluggable backends:

| Backend | Auth | Path |
|---------|------|------|
| DDG (default) | none | `search_backends/ddg.py` |
| Brave | `BRAVE_SEARCH_API_KEY` | `search_backends/brave.py` |
| Tavily | `TAVILY_API_KEY` | `search_backends/tavily.py` |
| Exa | `EXA_API_KEY` | `search_backends/exa.py` |
| Firecrawl | `FIRECRAWL_API_KEY` | `search_backends/firecrawl.py` |

Each request body contains the **search query text** — that's user content (the agent often searches with the user's framing). The query goes to whichever backend is selected.

### 5. OSV malware check — *on-demand*, on MCP launch

`/opencomputer/security/osv_check.py:130-154`. `POST https://api.osv.dev/v1/query` with `{"package": {"name", "ecosystem"}, "version"}`.

- Endpoint configurable via `OSV_ENDPOINT`.
- **Fails open on network errors** (see Risk Register §RR-1).
- Fires when `oc mcp install` or `oc plugin load` triggers an MCP launch.
- **No user content** — package metadata only.

### 6. PyPI version check — *automatic, opt-out*

`/opencomputer/cli_update_check.py:109-137`. Background daemon thread on CLI startup; `GET https://pypi.org/pypi/opencomputer/json`.

- 24-hour cache at `~/.opencomputer/.update_check.json`.
- Result shown at chat exit, not startup.
- 5-second timeout; fails open silently.
- **Opt-out** via `OPENCOMPUTER_NO_UPDATE_CHECK=1`.
- Body sent: nothing — pure GET. User-Agent: `"opencomputer/{version} (+update-check)"`.

This is the closest thing to a "phone home." It is read-only against a public registry; PyPI logs the request, but no user payload is transmitted. Still, it is *a network call the user did not initiate*. Documenting this in the README would help.

### 7. Channel adapters — *opt-in*, on plugin enable

Each enabled channel plugin (telegram, discord, slack, matrix, signal, whatsapp, imessage, email, mattermost, homeassistant, webhook) has its own outbound endpoint:

| Channel | Auth | Sends |
|---------|------|-------|
| Telegram | `TELEGRAM_BOT_TOKEN` | Message text + media via Bot API (`api.telegram.org`) |
| Discord | `DISCORD_BOT_TOKEN` | Messages via gateway WS + REST |
| Slack | `SLACK_BOT_TOKEN` | Messages via Slack Web API |
| Matrix | matrix-python-sdk credentials | Room events via homeserver |
| Signal | signal-cli REST endpoint | Local signal-cli; outbound via Signal protocol |
| WhatsApp | Cloud API token | Messages via Graph API |
| iMessage | BlueBubbles bridge | Local bridge → Apple iCloud |
| Email | IMAP/SMTP creds | Email content via SMTP server |
| Mattermost | server token | Posts via Mattermost API |
| HomeAssistant | `HOMEASSISTANT_TOKEN` | Commands via HA REST |
| Webhook | none (token in URL) | POST body = message text |

All payloads include user message content (it's the message). All require explicit enable + token configuration.

### 8. Optional extension egress

| Plugin | Endpoint | Trigger |
|--------|----------|---------|
| `extensions/dev-tools/fal_tool.py:128` | fal.ai function-calling endpoint | Agent calls `Fal` tool |
| `extensions/voice-mode/` | OpenAI Whisper (if cloud STT chosen); else local mlx-whisper / pywhispercpp models | Voice mode active |
| Browser-control | Arbitrary URLs at user direction; cookies isolated per session | Agent calls `Browser*` tools |

### Aggregate

| Egress | Trigger | User content sent? | Opt-in? | Recurring? |
|--------|---------|---------------------|---------|------------|
| LLM provider | Every turn | **YES** (full prompt) | Required for operation | Yes |
| Honcho | Every Nth turn (configurable) | **YES** (turn pair) | Default-on, can disable | Yes |
| WebFetch | Tool call | URL only | Yes | No |
| WebSearch | Tool call | Query text | Yes | No |
| OSV | MCP install/load | Package metadata | Yes (only when installing MCPs) | No |
| PyPI update | CLI startup | Nothing | Default-on, opt-out | Daily |
| Channel adapters | Per send | **YES** | Yes (per-plugin) | Yes |

**Confirmed absent:** Sentry, PostHog, Mixpanel, Segment, Amplitude, Datadog (`grep -rn -i "sentry\|posthog\|mixpanel\|segment\|amplitude\|datadog"` returns 1 hit — `/opencomputer/mcp/presets.py:362-379` defines a `"sentry"` MCP **preset** the user can choose to install, not bundled telemetry).

---

## Local-only claims vs. reality

### What the codebase claims is local-only

- README/CLAUDE.md framing: "personal AI agent framework" with Hostinger/local VPS deployment.
- `/CLAUDE.md` describes Honcho as self-hosted (postgres + redis + honcho-api on `127.0.0.1:8000`).
- Ambient sensors README claims "no network egress" + AST no-egress contract test (`extensions/ambient-sensors/`).
- Skill-evolution README: "no raw transcript on disk, no network."
- Browser-control README: isolated session per call; no auto-login.

### What is actually local-only (verified)

| Layer | Verified local? |
|-------|-----------------|
| All SQLite (sessions.db, graph.sqlite, motifs.sqlite, drift_reports.sqlite, rate.db, audit_log) | Yes — pure SQLite, file path under `~/.opencomputer/<profile>/` |
| Chroma vector store (`<profile>/profile_bootstrap/vector/`) | Yes — `PersistentClient` is SQLite-backed; no HTTP |
| BGE embedding model (33 MB) | First-run download from HuggingFace via sentence-transformers; cached locally; subsequent use is offline |
| Logs (`~/.opencomputer/logs/`) | Yes — `RotatingFileHandler` to disk |
| Audit log HMAC chain | Yes — local SQLite + keyring-stored HMAC key |
| Tool usage telemetry (`tool_usage` table) | Yes — local SQLite |
| Profile bootstrap raw_store | Yes — JSON files on disk |
| Skill evolution staging | Yes — markdown files on disk |
| Cost-guard ledger (`cost_guard.json`) | Yes — local JSON |

### Gaps between claim and reality

1. **Honcho is "self-hosted" but the docker stack ships AGPL upstream.** The provider's HTTP client respects `HONCHO_BASE_URL`. If a user points `HONCHO_BASE_URL` at a remote URL (e.g., a hosted Honcho instance), turn pairs leave the device. The default is `http://localhost:8000` and the `oc memory setup` flow runs the docker stack locally — but no code prevents the user (or a sloppy config copy) from setting a remote URL.

2. **The PyPI update check is on by default** with no obvious indicator. It's read-only and harmless but it *is* a daily outbound call. Some users prefer fully air-gapped environments.

3. **First-run BGE model download is implicit.** The `embedding.py:39-46` singleton pulls the model from HuggingFace on first use of layer-3 deepening. This is documented in the `[deepening]` extra's prerequisites but not in the runtime behaviour. If a user tries to run with no internet, the deepening pass fails to load the model.

4. **Workspace context is included in the LLM prompt.** This is *correctly designed* — workspace files are explicitly meant to be context — but there's no warning that **putting secrets in CLAUDE.md / AGENTS.md / OPENCOMPUTER.md will exfiltrate them via the LLM prompt** on every turn. See Risk Register §RR-3.

---

## Credentials

### Storage locations

| Location | What lives there |
|----------|-------------------|
| Environment variables | `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_MODE`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `HONCHO_BASE_URL`, `HONCHO_API_KEY`, `HONCHO_WORKSPACE`, `HONCHO_HOST_KEY`, `TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `SLACK_BOT_TOKEN`, `BRAVE_SEARCH_API_KEY`, `TAVILY_API_KEY`, `EXA_API_KEY`, `FIRECRAWL_API_KEY`, `FAL_API_KEY`, `HOMEASSISTANT_TOKEN`, `OSV_ENDPOINT`, `OPENCOMPUTER_NO_UPDATE_CHECK`, `OPENCOMPUTER_CONSENT_BYPASS` |
| Per-profile config | `~/.opencomputer/<profile>/config.yaml` — supports tool/provider settings (no secrets stored here by design) |
| Honcho plugin config | `~/.opencomputer/honcho/.env` (loaded by `_config_from_env()` in `extensions/memory-honcho/plugin.py:22-29`) |
| Audit-log HMAC key | System keyring (macOS Keychain / Linux Secret Service / Windows DPAPI), per `/opencomputer/agent/consent/audit.py` |
| Profile-local `.env` | Phase 14.F (planned, not yet implemented) |

### Credential pool

`/opencomputer/agent/credential_pool.py` accepts comma-separated env values for least-used distribution. Anthropic provider invokes it at `/extensions/anthropic-provider/provider.py:196-205`:

```python
api_key_raw = api_key or os.environ.get(self._api_key_env, "")
if "," in api_key_raw:
    keys = [k.strip() for k in api_key_raw.split(",") if k.strip()]
    self._credential_pool: CredentialPool | None = CredentialPool(keys=keys) if len(keys) > 1 else None
    self._api_key = keys[0] if keys else api_key_raw
```

Rotation: 401 quarantines a key for `ROTATE_COOLDOWN_SECONDS` (default 60 s). Up to 3 rotation attempts per request.

### Redaction patterns (verbatim from `/opencomputer/observability/logging_config.py:75-85`)

```python
_REDACT_PATTERNS = [
    (re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+"), "Bearer ***"),
    (re.compile(r"xox[bp]-[A-Za-z0-9-]{20,}"), "xox?-***"),
    (re.compile(r"\b\d+:[A-Za-z0-9_-]{20,}\b"), "***:telegram"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "sk-ant-***"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "sk-***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"(/Users/[^/]+/\.opencomputer/secrets/[^\s\"']+)"), "<secret-path>"),
]
```

Coverage:
- Bearer tokens (any auth scheme).
- Slack workspace + bot tokens (`xox[bp]-...`).
- Telegram bot tokens (`<digits>:<base64>`).
- Anthropic-issued keys (`sk-ant-...`).
- Generic OpenAI-style keys (`sk-...`).
- AWS access key IDs (`AKIA...`).
- Filesystem paths under `~/.opencomputer/secrets/`.

The `RedactingFormatter` (line 89-102) applies these patterns to the formatted message text on every log record. Order matters — more specific patterns precede generic `sk-*` to avoid double-replace.

**Coverage gaps** (worth noting, not necessarily exploitable):
- AWS secret keys (the 40-char half) are not regex-matched — only the 20-char access key ID is.
- GCP service-account JSON blobs (`{"type": "service_account", ...}`) are not redacted.
- Generic JWT (`eyJ...`) is not redacted.
- Stripe live keys (`sk_live_...`) are not specifically matched (the generic `sk-` regex would catch them but only with a 20+ char tail).

### Credential leakage hazards (audit grep results)

`grep -n "key\|token" /opencomputer/agent/credential_pool.py extensions/anthropic-provider/provider.py extensions/openai-provider/provider.py | grep -E "log|warning|info" | head -40`:

- `credential_pool.py:96-101` logs `key[:8]` (first 8 characters) at `WARNING` level when a key is quarantined. **See Risk Register §RR-4.**
- `credential_pool.py:103-104` logs `key[:8]` again at WARNING for "report_auth_failure for unknown key".
- `credential_pool.py:132` includes `key[:8] + "..."` in the snapshot dict returned by `pool_status()`.
- Anthropic provider logs image attachment failures by path + exception (no key content).
- OpenAI provider similar.

**Bearer-proxy x-api-key stripping** is correctly implemented at `/extensions/anthropic-provider/provider.py:160-169` — when `auth_mode == "bearer"`, an httpx event hook strips `x-api-key` before the request leaves the client, preventing Anthropic from rejecting the proxy's bearer token via the wrong auth header.

---

## Logs & telemetry

### Logger config

`/opencomputer/observability/logging_config.py`:

| Logger name | File | Default level | Rotation |
|-------------|------|----------------|----------|
| `opencomputer` | `~/.opencomputer/logs/agent.log` | `WARNING` (Python default; overridable) | 10 MB × 5 backups |
| `opencomputer.gateway` | `~/.opencomputer/logs/gateway.log` | inherited | 10 MB × 5 backups |
| `opencomputer.errors` | `~/.opencomputer/logs/errors.log` | `ERROR` | 10 MB × 5 backups |

Total ceiling: ~150 MB on disk. Permissions inherited from `~/.opencomputer/` (user mode 700 on macOS by default).

### Session-context filter

`SessionContextFilter` (lines 41-67) stamps every record with the current session_id from a `ContextVar`. ContextVar copy-on-task-spawn keeps concurrent sessions properly isolated within a single asyncio loop.

### Logged content audit

| Source | What's logged | Sensitive? |
|--------|---------------|------------|
| `/extensions/anthropic-provider/provider.py:99-115` | Image attachment failure: file path + exception | Path-only |
| `/opencomputer/agent/credential_pool.py:96-104, 132` | Partial key (first 8 chars) | **Yes — see RR-4** |
| `/opencomputer/tools/bash_safety.py` | Pattern_id (e.g., `rm_rf_root`) — **not** the command | Safe |
| `tool_usage` table | session_id, tool, outcome, duration_ms, model, ts — **no arguments, no output** | Safe |
| Hook fire sites | Event name + session id; no message content | Safe |
| Tool result spillover | The spillover *path* (e.g., `<profile>/tool_result_storage/{tool_use_id}.txt`) is referenced in messages but the file content is not duplicated to logs | Safe |

`grep -rn "logger.*content\|logger.*message\|log.*user_message" opencomputer/ extensions/ | head -40` returns no clear matches where user message content is interpolated into log records. The codebase is disciplined about not logging conversation content — likely because it would also blow log size limits.

### Audit log (the HMAC chain)

`/opencomputer/agent/consent/audit.py` — append-only SQLite table at `sessions.db.audit_log`:

- Per row: `session_id`, `actor`, `action`, `capability_id`, `tier`, `scope`, `decision`, `reason`, `prev_hmac`, `row_hmac`, `timestamp`.
- HMAC-SHA256 chained: row N's HMAC depends on row N-1.
- SQLite triggers (`state.py:192-200`) prevent UPDATE/DELETE — writes are append-only at the engine level.
- Genesis HMAC: `"0" * 64`.
- HMAC key in system keyring; not on disk.
- Verification: `verify_chain()` recomputes all HMACs; any tampering breaks the chain.
- Backup/recovery: `export_chain_head()` produces a JSON checkpoint the user can store off-system.

**See Risk Register §RR-5** for the start-up verification gap.

### Telemetry — confirmed absence

```bash
$ grep -rn -i "sentry\|posthog\|mixpanel\|segment\|amplitude\|datadog" opencomputer/ extensions/ pyproject.toml
```

Returns one hit (`/opencomputer/mcp/presets.py:362-379` defining a Sentry MCP **preset** — user-installable MCP, not bundled telemetry). Zero embedded analytics.

```bash
$ grep -rn "version.*check\|update_check\|check_for_update\|GET.*github.com\|GET.*pypi" opencomputer/
```

One hit: `/opencomputer/cli_update_check.py:109-137` — the documented PyPI version check, opt-out via `OPENCOMPUTER_NO_UPDATE_CHECK=1`.

### Encryption at rest

None for logs. Plaintext on disk. Redaction is the primary defence. Audit log HMAC chain is not encryption — it provides integrity (tamper evidence) but not confidentiality.

---

## User inspection affordances

The codebase exposes inspection through ~12 CLI sub-apps. Cross-referenced from `/opencomputer/cli.py`'s sub-app registrations.

### Memory & declarative knowledge

| Command | File:line | Output |
|---------|-----------|--------|
| `oc memory show` | `/opencomputer/cli_memory.py:62` | Dump MEMORY.md verbatim |
| `oc memory show --user` | `/opencomputer/cli_memory.py:62` | Dump USER.md verbatim |
| `oc memory search <query>` | `/opencomputer/cli_memory.py:95` | FTS5 over all session messages |
| `oc memory stats` | `/opencomputer/cli_memory.py:117` | Byte counts vs caps + `.bak` freshness |
| `oc memory doctor` | `/opencomputer/cli_memory.py:447-521` | Cross-layer health (baseline, episodic SessionDB, Docker, Honcho, provider, dreaming) |

### User-model graph (F4)

| Command | File:line | Output |
|---------|-----------|--------|
| `oc user-model nodes list [--kind X] [--limit 20]` | `/opencomputer/cli_user_model.py:125-159` | F4 nodes with confidence, last_seen, value |
| `oc user-model edges list [--kind X]` | `/opencomputer/cli_user_model.py:190-230` | F4 edges with salience, confidence, recency_weight, source_reliability |
| `oc user-model search <query>` | `/opencomputer/cli_user_model.py:236-257` | FTS5 over `nodes.value` |
| `oc user-model import-motifs` | `/opencomputer/cli_user_model.py:263-285` | Materialise motifs as nodes + edges |
| `oc user-model context` | `/opencomputer/cli_user_model.py:291-339` | Ranked context snapshot with optional text/kind filters |
| `oc user-model decay run [--apply]` | `/opencomputer/cli_user_model.py:345-395` | Preview or apply temporal decay |
| `oc user-model drift list` | `/opencomputer/cli_user_model.py:455-489` | Drift reports (KL divergence + significance) |
| `oc user-model drift show <id>` | `/opencomputer/cli_user_model.py:492-517` | Full drift report JSON |

### Cost & consent

| Command | File:line | Output |
|---------|-----------|--------|
| `oc cost show [--provider]` | `/opencomputer/cli_cost.py:31-68` | Usage + limits per provider, ops-today |
| `oc consent list` | `/opencomputer/cli_consent.py:109-126` | Active grants (capability_id, scope, tier, expiry) |
| `oc consent history [<cap>]` | `/opencomputer/cli_consent.py:181-206` | Audit log entries per capability |
| `oc audit show [--tool / --since / --decision / --session / --limit / --json]` | `/opencomputer/cli_audit.py:71-153` | Full audit_log rows |

### Skill catalogue & evolution

| Command | File:line | Output |
|---------|-----------|--------|
| `oc skill scan <path>` | `/opencomputer/cli_skills.py:64-120` | Skills Guard scan of a candidate SKILL.md |
| `oc evolution skills list` | `/opencomputer/evolution/cli.py:50-81` | Quarantined auto-proposals |
| `oc evolution skills review <slug>` | `/opencomputer/evolution/cli.py` | Inspect quarantined skill |
| `oc evolution skills promote <slug>` | `/opencomputer/evolution/cli.py:84-100` | Move quarantine → active skills |

### What is NOT inspectable from the CLI

1. **F4 graph bulk-export.** `oc user-model nodes/edges list` paginates with `--limit`; no single-command full dump. (Mitigation: `sqlite3 graph.sqlite '.dump'` works directly.)
2. **Motif store** (`<profile>/inference/motifs.sqlite`) — no dedicated inspect CLI; only via `oc user-model import-motifs` which converts to F4 nodes.
3. **Drift-reports SQLite** raw rows — only via the curated drift commands.
4. **Chroma vector contents** — no CLI surface (Chroma's own client is the only path).
5. **HMAC audit chain internals** (key material, raw HMACs) — `oc consent verify-chain` / `oc audit verify` check integrity but do not expose internals.
6. **Tool result spillover files** — accumulated in `<profile>/tool_result_storage/` but no CLI lists them or cleans them. See RR-11.
7. **Profile bootstrap raw_store JSON envelopes** — accumulated in `<profile>/profile_bootstrap/raw_store/`, no CLI inspect command.

### Developer / debugging affordances

- `oc doctor` — top-level health check (memory + episodic + Docker + Honcho + provider + dreaming + cost + voice + ambient + browser).
- Verbose logging — `LOG_LEVEL=DEBUG` env var.
- `oc session resume` — referenced in CLAUDE.md §5 ("Tier 2 — Checkpoint table shipped; CLI surface pending"); status of full wire-up is **partial** per project memory.
- No OpenTelemetry / structured tracing emitter found in the tree.
- No replay or dry-run mode for sessions.

---

## Reset & deletion

### "Delete everything" path

**There is no single command that wipes all user data.** A full wipe requires:

1. `oc memory prune` — clears MEMORY.md (with `.bak` backup).
2. `oc memory prune --user` — clears USER.md (with `.bak` backup).
3. `oc memory reset` — Honcho-specific: tears down docker containers + wipes postgres + redis volumes.
4. `oc profile rm <name>` — deletes `<profile>/` directory entirely.

After these four steps:
- ✅ MEMORY.md, USER.md, F4 graph, motifs, drift reports, sessions.db, audit log, episodic events, vector store, raw_store, tool_result_storage, evolution staging — all gone (deleted with the profile directory).
- ❌ `~/.opencomputer/logs/*.log` — global log dir, **not** profile-local; survives `oc profile rm`.
- ❌ `~/.opencomputer/.update_check.json` — survives.
- ❌ Audit log HMAC key in system keyring — survives.
- ❌ Honcho's docker volumes — survive *unless* `oc memory reset` was run first (which it wouldn't have been if the profile was already deleted).
- ❌ Voice / Whisper model cache (~33 MB BGE + larger Whisper) — survives.

### Per-component deletion table

| Command | What it removes | Hard or soft |
|---------|-----------------|--------------|
| `oc memory prune [--user]` | MEMORY.md or USER.md (creates `.bak` first) | Soft — `.bak` backup preserves one undo |
| `oc memory restore [--user]` | Restores from `.bak` | Restorative |
| `oc memory reset --yes` | Honcho docker stack + volumes | Hard (destructive) |
| `oc cost reset [--provider X]` | Recorded usage in `cost_guard.json` (keeps limits) | Hard |
| `oc consent revoke <cap>` | One grant in `consent_grants` (logs audit event) | Logical delete |
| `oc user-model decay run --apply` | Bumps `recency_weight` toward zero (does not delete edges) | Soft (no row delete) |
| `oc evolution skills <slug>` deletion | Manual fs delete of quarantine/<slug>/ — no dedicated reject command | Hard |
| `oc profile rm <name>` | Entire `<profile>/` directory | Hard |

### Hard vs. soft characteristics

| Path | Atomicity | Recoverability |
|------|-----------|-----------------|
| MEMORY.md / USER.md prune | Atomic write via `_write_atomic()` (`/opencomputer/agent/memory.py` invariant block) | `.bak` recoverable for one step; afterwards filesystem-level recoverability only until reuse |
| sessions.db row delete | **No CLI to delete rows** — see RR-2 | N/A |
| F4 nodes/edges delete | **No CLI to delete rows** | N/A |
| Motifs delete | **No CLI** | N/A |
| Tool-result spillover files | **No CLI cleans these up periodically** — see RR-11 | Survive across sessions |
| SQLite VACUUM | Not run after deletes | Disk pages remain readable until reused |

### Survive-a-reset list

What lives outside the profile dir or beyond `oc memory reset`:

- `~/.opencomputer/logs/*.log` — global, not profile-scoped.
- `~/.opencomputer/.update_check.json` — global cache.
- `~/.opencomputer/.locks/*.lock` — plugin single-instance locks.
- `~/.opencomputer/keyring` (or system keyring) — audit-chain HMAC key.
- HuggingFace BGE model cache — typically `~/.cache/huggingface/`.
- Voice / Whisper model cache — typically `~/.cache/whisper/` or platform-specific.
- Honcho docker volumes (if `oc memory reset` was not run before profile delete).
- Browser-control Playwright browser caches (typically `~/.cache/ms-playwright/`).

---

## Self-modification & feedback loops

### Training-on-own-output paths

1. **PostResponseReviewer** — `/opencomputer/agent/reviewer.py:35-72, 99`. Rule-based regex (no LLM). Fires after every assistant turn (fire-and-forget). Phrases like "i'll remember", "noted", "got it you prefer", "thanks for telling me", "good to know" trigger an `append_declarative()` call into MEMORY.md. The note then becomes part of the next turn's frozen system prompt → influences the model's next output → could re-trigger another note. Recursion guard: `is_reviewer=True` flag prevents the reviewer from spawning a reviewer of its own observation.

2. **Episodic dreaming** — `/opencomputer/agent/dreaming.py`. Consolidates undreamed `episodic_events` rows into summary rows; uses cheap_model or main model. Clustering is heuristic (date bucket + topic keywords). Writes consolidations as new rows with `source = "dreaming"`. These then participate in FTS5 cross-session search → may surface in next-turn context → next turn may add more event rows → next dreaming pass may consolidate them again. Bounded by the `dreamed_into` pointer that marks events already consumed.

3. **Skill evolution** — `/extensions/skill-evolution/subscriber.py`. Listens to `SessionEndEvent`. Two-stage filter: heuristic `pattern_detector.is_candidate_session()` then `judge_candidate_async()` (the LLM judge — same model as the agent). Quarantined drafts land in `<profile>/evolution/quarantine/`. **The promotion gate is explicit: agent cannot self-promote.** Only `oc evolution skills promote <slug>` (a CLI, not a tool) moves quarantine → active skills.

### Closed-loop hazards & guardrails

| Hazard | Guardrail | Cite |
|--------|-----------|------|
| Honcho synthesis edges feeding back into the next dialectic call | Confidence cap ≤ 0.5; `source = "honcho_synthesis"` provenance; importer skips edges whose source `startswith("honcho_")` | `/opencomputer/user_model/honcho_bridge.py:25-26, 171` |
| Persona auto-classifier tunnelling to a single persona | Heuristic rules with priority order (state-query → trading → relaxed → coding → learning → companion fallback); no LLM call so no drift | `/opencomputer/awareness/personas/classifier.py:65, 73-102` |
| Vibe classifier influencing future vibes | Persisted to `sessions.db.vibe`, used cross-session as context only (`PREVIOUS-SESSION VIBE` anchor); does not write F4 | `loop.py:1218-1271`, `state.py:54-56, 552, 565` |
| Auto-skill-evo self-promotion | Two-stage filter + user-gated CLI promote (agent has no tool wrapper for promote) | `/opencomputer/evolution/cli.py:84-100` |
| Agent editing SOUL.md / config.yaml | No automatic writer; agent has only Edit tool, gated by F1 consent + PreToolUse hooks | `/opencomputer/agent/memory.py` (no `write_soul()` method); `loop.py:1609-1689` consent gate |
| Agent disabling plugins / hooks | No tool surface for `disable_plugin` — those are CLI-only commands | (verified by absence of tool registration for disable) |
| Agent granting itself consent | F1 gate checks at tool-call time; agent has no tool wrapper for `oc consent grant` | `loop.py:1609-1689` |
| User-explicit nodes (confidence 1.0) being overwritten by lower-confidence inferred edges | F4 upsert uses confidence; lower-confidence inserts do not overwrite higher-confidence values | `/opencomputer/user_model/store.py:306-318` (upsert behaviour) |

### Net assessment

The codebase has **deliberate guardrails** at every closed-loop boundary:
- Skills user-gated.
- Honcho synthesis confidence-capped + provenance-tagged + cycle-prevented.
- Agent cannot edit SOUL.md, config.yaml, or grant itself consent through any tool.
- Reviewer is rule-based, not LLM-driven.
- Persona classifier is heuristic.

The remaining drift surface is the LLM-judge in skill evolution (it shares the agent's model). But because promotion requires user action, even an LLM-judge that approves everything cannot affect runtime behaviour without user consent.

---

## File references

### Data egress

- `/extensions/anthropic-provider/provider.py:78-133, 160-168, 196-205, 242-249, 264-371, 432-437, 478-530`.
- `/extensions/openai-provider/provider.py:78-130, 149-158, 165, 189-200`.
- `/extensions/aws-bedrock-provider/provider.py:30, 40, 44, 69-90`.
- `/extensions/memory-honcho/provider.py:1-12, 106, 185, 210, 232, 251, 286, 320-326, 341-404`; `plugin.py:22-29, 89-102, 105-149`.
- `/opencomputer/tools/web_fetch.py:85-150`.
- `/opencomputer/tools/web_search.py:108-150`; backends in `/opencomputer/tools/search_backends/`.
- `/opencomputer/security/url_safety.py`; `osv_check.py:130-154`.
- `/opencomputer/cli_update_check.py:109-137`.
- `/extensions/{telegram,discord,slack,…}/plugin.py` token reads.
- `/extensions/dev-tools/fal_tool.py:128`.

### Local-only

- `<profile>/sessions.db`, `<profile>/user_model/graph.sqlite`, `<profile>/inference/motifs.sqlite`, `<profile>/profile_bootstrap/{vector,raw_store}/`, `<profile>/evolution/{quarantine,approved,archive}/`, `<profile>/tool_result_storage/`, `~/.opencomputer/logs/`, `~/.opencomputer/<profile>/cost_guard.json`.
- `/opencomputer/agent/consent/audit.py` HMAC chain.
- `/opencomputer/profile_bootstrap/embedding.py:39-46` BGE singleton.

### Credentials

- `/opencomputer/agent/credential_pool.py:78, 96-104, 132`.
- `/extensions/anthropic-provider/provider.py:160-169, 196-205, 242-249`.
- `/opencomputer/observability/logging_config.py:75-85, 89-102`.

### Logs & telemetry

- `/opencomputer/observability/logging_config.py` (full file).
- `/opencomputer/agent/state.py:1022-1060` `tool_usage`.
- `/opencomputer/agent/consent/audit.py` HMAC chain.
- `/opencomputer/cli_update_check.py:109-137`.
- `/opencomputer/mcp/presets.py:362-379` (Sentry MCP preset, opt-in only).

### Inspection affordances

- `/opencomputer/cli_memory.py:62, 95, 117, 137, 168, 238, 276, 298, 447-521, 543, 595` — 11+ subcommands.
- `/opencomputer/cli_user_model.py:125-159, 190-230, 236-257, 263-285, 291-339, 345-395, 455-489, 492-517` — F4 inspect surface.
- `/opencomputer/cli_consent.py:109-126, 163-178, 181-206`.
- `/opencomputer/cli_audit.py:71-153`.
- `/opencomputer/cli_cost.py:31-68`.
- `/opencomputer/cli_skills.py:64-120`; `/opencomputer/evolution/cli.py:50-81, 84-100`.

### Reset & deletion

- `/opencomputer/cli_memory.py:137-165, 168-180, 297-324`.
- `/opencomputer/agent/memory.py` `_write_atomic()` invariant.
- `/opencomputer/profiles.py` (cited; profile rm path).

### Self-modification & feedback

- `/opencomputer/agent/reviewer.py:35-72, 99`.
- `/opencomputer/agent/dreaming.py`.
- `/extensions/skill-evolution/subscriber.py`; `/opencomputer/evolution/{pattern_detector,synthesize,cli}.py`.
- `/opencomputer/user_model/honcho_bridge.py:25-26, 58-70, 171`.
- `/opencomputer/awareness/personas/classifier.py:65, 73-102`.

---

## Risk register (non-optional)

17 items. Sorted by severity. Citations are file:line. None of these are "the affect plan is broken" — they are independent privacy / operational hazards worth tracking.

### RR-1 — OSV malware check fails open (Medium)

**File:line:** `/opencomputer/security/osv_check.py:130-154`.

**Description:** OSV is the pre-launch malware screen for MCP packages. On any network error (DNS failure, OSV outage, timeout, 5xx response), the check returns "no advisory" → MCP launches unscanned. A network partition silently bypasses the malware gate.

**Mitigation:** Add a `osv_check_fail_closed=True` config switch (off by default for backwards compat). Log a WARN-level signal whenever a fail-open occurs so operators can alert on it.

### RR-2 — `sessions.db` rows accumulate without prune affordance (Medium)

**File:line:** `/opencomputer/agent/state.py` (no prune surface), `/opencomputer/cli_memory.py` (no `oc session prune`).

**Description:** Every conversation turn writes to `messages` (and indirectly to `messages_fts`); every consent decision writes to `audit_log`; every tool call writes to `tool_usage`. There is no CLI to delete old rows. Disk usage grows unbounded over time. Long-running profiles will eventually see multi-GB sessions.db files.

**Mitigation:** Add `oc session prune --older-than <days>` (default 90) that deletes from `messages`, `episodic_events`, `tool_usage` for sessions older than the threshold and runs `VACUUM` afterwards. Audit log should remain append-only by design.

### RR-3 — Workspace context is sent to the LLM on every turn (High)

**File:line:** `/opencomputer/agent/prompt_builder.py:31, 38-46, 88-89`.

**Description:** `load_workspace_context()` walks cwd + ancestors looking for `CLAUDE.md`, `OPENCOMPUTER.md`, `AGENTS.md`. Each found file (capped at 100 KB) is concatenated into the frozen system prompt. **If a user has API keys, secrets, or sensitive notes in any of these files anywhere up the directory tree, they are sent to the LLM provider on every turn.** The redaction layer applies only to logs, not to outbound LLM payloads.

**Mitigation:** Add a startup-time scan that checks workspace files for the same secret-pattern regexes used in logging redaction; warn loudly if a match is found. Optionally provide a `--skip-workspace-context` CLI flag. Document the hazard in the README and the `opencomputer-skill-authoring` skill.

### RR-4 — Credential pool logs first 8 chars of API keys at WARNING (Low → potentially Medium)

**File:line:** `/opencomputer/agent/credential_pool.py:96-104, 132`.

**Description:** The `quarantined key %s... for %ds` and similar log messages emit `key[:8]` to the WARNING channel. For Anthropic keys (format `sk-ant-XXX...`), the first 8 chars are `sk-ant-X` — leaks one character of secret entropy plus the format prefix. For OpenAI-style `sk-XXX...`, it's `sk-XXXXXX` — leaks 6 characters of the secret. Concrete impact depends on key entropy, but the pattern violates "log no secrets" hygiene.

**Note:** The redaction formatter at `/opencomputer/observability/logging_config.py:75-85` MAY redact this — the `\bsk-[A-Za-z0-9]{20,}\b` pattern requires 20+ chars, but `key[:8]` is only 8. So the formatter's regex does NOT match these short prefixes. **The leak survives redaction.**

**Mitigation:** Replace `key[:8]` with a stable hash (`hashlib.sha256(key.encode()).hexdigest()[:8]`) so logs can correlate failures across rotations without leaking key prefix bytes. Or use just an opaque pool index (`key_0`, `key_1`).

### RR-5 — Audit log HMAC chain not verified at startup (Informational)

**File:line:** `/opencomputer/agent/consent/audit.py`.

**Description:** Tamper detection requires running `oc consent verify-chain` or `oc audit verify` explicitly. A user who modifies `sessions.db.audit_log` rows offline (the SQLite triggers prevent UPDATE/DELETE only when accessed via the python connection — direct sqlite3 file edits or another process bypass them) will not be caught until the next manual verify. Audit logs are most valuable when they're checked, not just maintained.

**Mitigation:** Add an opt-in `audit.verify_on_start = true` config that runs `verify_chain()` lazily on the first audit-related call. Acceptable startup cost trade-off (chain length × HMAC cost ≈ 100 µs/row; negligible for thousands of rows).

### RR-6 — `_RECENT_NOTES` deduplication cache is module-level, not per-profile (Low)

**File:line:** `/opencomputer/agent/reviewer.py:44-45, 95-96, 104-106`:

```python
_RECENT_NOTES: list[str] = []
_RECENT_NOTES_MAX = 16
```

**Description:** The 16-note dedup buffer is module-global. If two profiles run in the same Python process (e.g., a gateway daemon serving multiple chat IDs that map to different profiles), notes from profile A can suppress notes for profile B. In practice this is unlikely (each profile usually has its own process), but it's a latent footgun.

**Mitigation:** Move `_RECENT_NOTES` into the `PostResponseReviewer` instance, or key it by profile/session.

### RR-7 — Cron-fired turns can call any tool with no consent prompt (High)

**File:line:** `/opencomputer/agent/loop.py:697, 975` (the `runtime.agent_context` short-circuit), `/opencomputer/cron/scheduler.py`.

**Description:** Cron-fired turns set `runtime.agent_context = "cron"` (or similar) and short-circuit user-prompting paths — most importantly, `MemoryBridge.sync_turn()` skips on `_BATCH_CONTEXTS`. **The F1 consent gate at `loop.py:1609-1674` does not appear to special-case cron context.** A misconfigured cron job with a prompt like "send a slack message about today's news" will run with whatever consent grants exist on the active profile. If `channel.send.slack` is granted with tier 4 (implicit), the cron job will spam Slack unattended — and the user won't see an interactive prompt because the cron context cannot show one.

**Mitigation:** Cron-context turns should default to tier-2 (per-action) for any side-effect tools (network, send, write outside profile). Either reject the call or post an audit-log entry explicitly noting "cron turn invoked X under implicit consent." Surface in `oc audit show --since 1d`.

### RR-8 — Browser-control isolation guarantee not verified (Medium)

**File:line:** `/extensions/browser-control/` README claims "isolated session per call"; `/extensions/browser-control/tools.py` Playwright client init.

**Description:** Per-session Playwright browser context is the standard isolation mechanism, but I did not verify that the Playwright client uses `browser.new_context()` per call (vs. reusing one context across calls). If contexts are reused, cookies / localStorage / authenticated session state leak between calls. The user's intent in opting into browser-control is automation; cross-call leakage breaks that contract.

**Mitigation:** Read `extensions/browser-control/tools.py` carefully and confirm `new_context()` is called per tool invocation, with `context.close()` afterwards. If reuse is happening, fix it. Write a test that asserts cookies set in call 1 do not appear in call 2.

### RR-9 — Voice mode audio cache not investigated (Informational)

**File:line:** `/extensions/voice-mode/`, `/opencomputer/voice/`.

**Description:** Voice mode invokes `mlx-whisper`, `whisper-cpp`, or cloud Whisper. Local Whisper backends typically cache transcribed audio buffers in temp files. If those temp files are not cleaned up, raw audio (and the transcript) can persist on disk. I did not verify the cleanup path.

**Mitigation:** Read `extensions/voice-mode/` plugin code, confirm temp-file cleanup, document the location.

### RR-10 — Ambient sensors filter list scope is opaque (Informational)

**File:line:** `/extensions/ambient-sensors/`.

**Description:** README claims SHA-256 hashing of titles + sensitive-app filter. The exact filter list (e.g., banking apps, password managers, browser titles for sensitive URLs) is not documented in this audit. If the list misses an app the user considers sensitive, that app's title hash + frequency reaches the F4 graph + downstream prompts.

**Mitigation:** Document the filter list verbatim in the user-facing README. Provide a `~/.opencomputer/ambient_sensors_blocklist.yaml` users can extend.

### RR-11 — Tool-result spillover files accumulate forever within a profile (Medium)

**File:line:** `/opencomputer/agent/tool_result_storage.py`; `<profile>/tool_result_storage/`.

**Description:** Large tool outputs (e.g., a `Grep` over a large repo, a Bash command emitting megabytes) are written to `<profile>/tool_result_storage/{tool_use_id}.txt` to keep the in-context preview short. There is **no periodic cleanup**. Files persist until the profile is deleted. Long-running profiles accumulate gigabytes of historical tool outputs, including potentially sensitive shell command output (e.g., `cat .aws/credentials` would land in this dir).

**Mitigation:** Add `oc session prune --include-tool-results` that deletes spillover files older than N days. Or auto-clean spillover files at session end. Or cap total spillover-dir size and LRU-evict.

### RR-12 — `extensions/oi-capability/` husk could be loaded by accident (Low)

**File:line:** `/extensions/oi-capability/` — vestigial after the OI Bridge → native introspection migration (PR #179, per `01-topology.md` §Dead/vestigial).

**Description:** The directory still exists. If it contains a `plugin.json`, the plugin discovery scan will pick it up and attempt to load it. If the entry module imports the removed AGPL Open Interpreter package, the load will fail at runtime. Worse, if some half-migrated entry remains and successfully loads, behaviour is undefined.

**Mitigation:** Delete the directory entirely (one-line PR). Verify no `plugin.json` is present in the dir; if present, that confirms the husk hazard.

### RR-13 — `MemoryProvider.on_session_end()` declared but not invoked (Medium)

**File:line:** `/plugin_sdk/memory.py:103-109` (declaration); `/opencomputer/agent/memory_bridge.py:475-490` (`fire_session_end()` defined but never called from `loop.py`).

**Description:** Plugins that implement `on_session_end()` (Honcho stubs it) believe they will be notified at session end. They are not. End-of-session cleanup, finalisation, or aggregation in plugin code silently does not run. A plugin author depending on this hook will be misled.

**Mitigation:** Either (a) wire `fire_session_end()` into the loop's `finally` block (~5 lines), or (b) remove the declaration from the SDK and document that `SessionEndEvent` on the ingestion bus is the only end-of-session hook.

### RR-14 — `USER_PROMPT_SUBMIT` declared but not invoked (Low)

**File:line:** `/plugin_sdk/hooks.py:50` declares `USER_PROMPT_SUBMIT`; `grep -n "USER_PROMPT_SUBMIT" /opencomputer/agent/loop.py` returns zero fire sites.

**Description:** Same shape as RR-13. Plugin authors registering for this event won't fire. The event is the only path to *blocking* refusal of a user message; its absence means there is no clean way to block a turn at the input boundary.

**Mitigation:** Either fire the event in `loop.py` near line 684 (before user-message append), or remove the declaration.

### RR-15 — Honcho's AGPL-3.0 dependency not surfaced at `oc memory setup` (Informational)

**File:line:** `/extensions/memory-honcho/plugin.py` (provider docstring mentions AGPL); `/opencomputer/cli_memory.py:238` (`oc memory setup` Honcho bootstrap).

**Description:** Running `oc memory setup` pulls and runs Plastic Labs's Honcho image, which is AGPL-3.0. Users may be unaware of the licence implications.

**Mitigation:** Add a "this will pull AGPL-3.0 software — do you accept? [y/N]" prompt at the start of `oc memory setup`.

### RR-16 — Telegram pairing does not verify bot ownership (Medium)

**File:line:** `/opencomputer/channels/pairing.py:86-94` (Telegram `getMe` validation).

**Description:** `oc pair telegram` validates the bot token is format-correct and reachable but does not verify the user owns the bot. A user could paste another person's bot token, grant themselves `channel.send.telegram`, and send messages on that bot. The bot's actual owner has no notification.

**Mitigation:** Add a verification round-trip: instruct the user to send a one-time code to the bot from their Telegram account; only validate the pairing if the code arrives. Acceptable for v1.0 to defer; document explicitly.

### RR-17 — Workspace context loader has no symlink-escape protection documented (Low)

**File:line:** `/opencomputer/agent/prompt_builder.py:38-46` (`load_workspace_context`); compare to `/opencomputer/plugins/discovery.py` which DOES validate symlink escapes.

**Description:** `load_workspace_context()` walks cwd up to `max_depth = 5` ancestors. If any ancestor contains a symlinked CLAUDE.md / AGENTS.md / OPENCOMPUTER.md pointing to a sensitive file (e.g., `~/.aws/credentials`), the loader follows the symlink and embeds the target's content (capped at 100 KB) in the system prompt. This could exfiltrate arbitrary files matching the names anywhere reachable via symlink.

**Mitigation:** Resolve symlinks and verify the resolved path stays within an allowed root (e.g., the cwd subtree only); refuse to read files outside that root. Match the symlink-escape protection already present in plugin discovery.

### Severity rollup

| Severity | Count | Items |
|----------|-------|-------|
| Critical | 0 | — |
| High | 2 | RR-3 (workspace-context secrets), RR-7 (cron + implicit consent) |
| Medium | 7 | RR-1, RR-2, RR-8, RR-11, RR-13, RR-16, plus an arguable case for RR-4 |
| Low | 5 | RR-4 (or Medium), RR-6, RR-12, RR-14, RR-17 |
| Informational | 4 | RR-5, RR-9, RR-10, RR-15 |

The highest priorities are RR-3 (workspace-context secrets exfiltrate via every LLM call) and RR-7 (cron jobs bypass interactive consent). Both warrant a ship-blocking PR before v1.0 stable. RR-4 is borderline — leaking 6-8 characters of `sk-...` keys in WARNING logs is a hygiene violation even if direct exploitation is hard.

