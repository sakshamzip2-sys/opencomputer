# Platform Reach — Hermes → OpenComputer Adapter Port Guide

> Written 2026-04-27 to make Track B mechanical. Each adapter port should
> be ~1-2h of focused work using this guide as the recipe. Ships from the
> master plan at `docs/superpowers/plans/2026-04-27-companion-and-platform-master.md`.

## The pattern (OC's existing channel adapter contract)

Every channel adapter ships as an extension under `extensions/<platform>/`:

```
extensions/<platform>/
├── plugin.py        ← ~20 lines; reads env vars, instantiates adapter, calls api.register_channel
├── adapter.py       ← extends BaseChannelAdapter, implements platform methods
├── plugin.json      ← optional metadata; not required for register-time discovery
└── README.md        ← env vars + setup notes
```

Reference implementation: **`extensions/discord/`** (272 LOC adapter, 21 LOC plugin.py — clean and small enough to read in 5 minutes).

The contract lives in `plugin_sdk/channel_contract.py:BaseChannelAdapter`. Required methods:

| Method | Purpose |
|---|---|
| `__init__(config)` | Take config dict; store creds; build client (don't connect yet) |
| `start(dispatch)` | Connect to platform; on inbound, call `dispatch(MessageEvent)` |
| `stop()` | Clean shutdown |
| `send(chat_id, body, attachments=...)` | Outbound delivery; return `SendResult` |

Optional capabilities (declare via `capabilities = ChannelCapabilities.X | Y`): typing indicator, reactions, edit-message, delete-message, threads, attachments.

`MessageEvent` shape (from `plugin_sdk.core`):

```python
@dataclass(frozen=True, slots=True)
class MessageEvent:
    platform: Platform
    chat_id: str
    user_id: str
    text: str
    timestamp: float
    attachments: list[str] = []   # OC convention: "<platform>:<file_id>"
    metadata: dict[str, Any] = {}
```

## Hermes → OC mapping cheatsheet

When porting any hermes `gateway/platforms/<X>.py`:

| Hermes thing | OC equivalent |
|---|---|
| `from hermes_constants import is_wsl` | inline `platform.uname().release.lower().startswith("microsoft")` |
| `from hermes_state import ...` | `from opencomputer.agent.state import SessionDB` |
| `class XPlatform(BasePlatform)` | `class XAdapter(BaseChannelAdapter)` |
| `BasePlatform.start(self, on_message)` | `start(self, dispatch)` |
| `OutboundMessage` type | `SendResult` |
| Hermes' inline message-event dataclass | `plugin_sdk.core.MessageEvent` |
| `hermes_logging.get_logger(__name__)` | `logging.getLogger("opencomputer.ext.<platform>")` |
| Hermes config (`config.get_str(...)`) | `os.environ.get("XYZ_TOKEN", "")` in plugin.py + dict to adapter |
| `Platform.X` (hermes enum) | `Platform.X` (OC enum at `plugin_sdk.core.Platform`) — extend if missing |

## Per-adapter recipes

### B.1 — Matrix (`hermes/gateway/platforms/matrix.py`, 2,216 LOC)

**Most complex of the queue.** matrix-nio integration with E2E encryption support. Recipe:

1. Add to `Platform` enum: `MATRIX = "matrix"`
2. Dependency: `matrix-nio[e2e]>=0.24` (E2E is optional but expected)
3. Env vars: `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, optional `MATRIX_DEVICE_ID`
4. Port skeleton from hermes — strip its config-system reads, replace with config dict
5. Trim: hermes ships device verification UX, room-membership management, large-attachment chunking. **Skip for v1**; adapter just needs send/receive text messages. File those features as `B.1.1` if needed.
6. Test: mock `nio.AsyncClient` interactions; verify `dispatch(MessageEvent)` fires on `on_room_message`.
7. README: how to get `MATRIX_ACCESS_TOKEN` via element.io login.

Minimum viable port: ~600 LOC adapter + 200 LOC tests. Full feature parity with hermes: ~1,500 LOC.

### B.1 — Mattermost (`hermes/gateway/platforms/mattermost.py`, 739 LOC)

Tractable. WebSocket + REST hybrid via mattermostdriver.

1. `Platform.MATTERMOST = "mattermost"` (extend enum)
2. Dependency: `mattermostdriver>=7.3`
3. Env vars: `MATTERMOST_URL`, `MATTERMOST_TOKEN`, optional `MATTERMOST_TEAM`
4. Port pattern is mostly lift-and-shift; hermes uses driver.login + driver.init_websocket + handler.
5. Skip: hermes' server-side hooks integration. Just inbound + outbound for v1.
6. Estimated: ~400 LOC adapter + 150 LOC tests. ~3h.

### B.2 — Signal (`hermes/gateway/platforms/signal.py`, 993 LOC)

**Externally complex** — requires `signal-cli` daemon running locally. Hermes spawns/manages it.

1. `Platform.SIGNAL = "signal"`
2. External dep: `signal-cli` (Java app) on PATH; env var `SIGNAL_CLI_PATH` for override
3. Env vars: `SIGNAL_PHONE_NUMBER` (registered)
4. Hermes uses `signal-cli` JSON-RPC mode over a Unix socket. Subprocess management is half the file.
5. Skip: hermes' device-link QR flow. v1 assumes `signal-cli` is already registered.
6. Estimated: ~500 LOC + 200 LOC tests. ~5h. Highest user value of the SMS/messaging cluster.

### B.2 — WhatsApp (`hermes/gateway/platforms/whatsapp.py`, 1,074 LOC)

Cloud API (Meta WhatsApp Business API).

1. `Platform.WHATSAPP = "whatsapp"`
2. Env vars: `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_VERIFY_TOKEN`
3. Hermes uses webhook mode (Meta sends inbound) + REST send. OC's existing `webhook_helper.py` (in `extensions/telegram/`) is the pattern — port the helper concept.
4. Skip: hermes' template-message workflow. v1 ships text messages only.
5. Estimated: ~500 LOC + 200 LOC tests. ~4h.

### B.2 — SMS (`hermes/gateway/platforms/sms.py`, 373 LOC) — START HERE

**Smallest of the queue. Best worked example.** Twilio inbound webhook + outbound REST.

1. `Platform.SMS = "sms"`
2. Dependency: `twilio>=8.0`
3. Env vars: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`
4. Port is mostly direct — Twilio's SDK is small.
5. Estimated: ~250 LOC + 100 LOC tests. **~2h. Recommended first port.**

### B.4 — Email (`hermes/gateway/platforms/email.py`, 626 LOC)

IMAP polling for inbound + SMTP for outbound.

1. `Platform.EMAIL = "email"`
2. Stdlib only (`imaplib`, `smtplib`, `email`).
3. Env vars: `EMAIL_IMAP_HOST`, `EMAIL_IMAP_USER`, `EMAIL_IMAP_PASS`, `EMAIL_SMTP_HOST`, `EMAIL_FROM`, optional `EMAIL_POLL_SECONDS=30`.
4. Hermes uses an IDLE-based long-poll. v1 can use simple polling loop (every 30s) — IDLE is an optimization.
5. Skip: hermes' OAuth2 flow. v1 supports app-password auth only.
6. Estimated: ~350 LOC + 150 LOC tests. ~4h.

### B.5 — Webhook (`hermes/gateway/platforms/webhook.py`, 775 LOC) — GENERIC

The most general adapter. Inbound: HTTP server listening for POST. Outbound: HTTP POST to user-configured URL.

1. `Platform.WEBHOOK = "webhook"`
2. Stdlib + existing `aiohttp` dep.
3. Env vars: `WEBHOOK_BIND_HOST=0.0.0.0`, `WEBHOOK_BIND_PORT=18790`, `WEBHOOK_OUTBOUND_URL`, `WEBHOOK_AUTH_TOKEN`.
4. Hermes ships HMAC signature verification for inbound — port this.
5. Estimated: ~400 LOC + 200 LOC tests. ~3h.

### B.5 — Home Assistant (`hermes/gateway/platforms/homeassistant.py`, 449 LOC)

WebSocket connection to HA core; subscribe to `conversation` events; respond via HA service call.

1. `Platform.HOMEASSISTANT = "homeassistant"`
2. Dependency: `aiohttp` (already in OC).
3. Env vars: `HA_URL`, `HA_TOKEN` (Long-Lived Access Token).
4. Port is small; main work is the WebSocket protocol handler.
5. Estimated: ~300 LOC + 150 LOC tests. ~2.5h.

## Suggested execution order (re-ranked by tractability)

1. **B.2 SMS** (~2h) — smallest, demonstrates the Twilio webhook pattern
2. **B.5 HomeAssistant** (~2.5h) — small, shows WebSocket pattern
3. **B.1 Mattermost** (~3h) — driver-based pattern
4. **B.5 Webhook** (~3h) — generic; reusable webhook helper
5. **B.4 Email** (~4h) — stdlib-heavy
6. **B.2 WhatsApp** (~4h) — webhook-mode cloud API
7. **B.2 Signal** (~5h) — subprocess management
8. **B.1 Matrix** (~6h+) — most complex; do last

Total realistic effort: **~28h across 8 adapters**, 1 PR each.

## Per-PR template

Every adapter PR should include:

1. New `extensions/<platform>/` directory with `adapter.py`, `plugin.py`, `README.md`
2. New `Platform.X = "x"` enum entry in `plugin_sdk/core.py`
3. New unit-test file `tests/test_<platform>_adapter.py` with mocked client interactions
4. CHANGELOG entry under `## [Unreleased]` → `### Added (Track B — <Platform> channel adapter)`
5. README documents env vars + setup steps
6. NO new top-level dependency unless it's the platform's required SDK (no incidentals)

## License attribution

Hermes is MIT. Each ported adapter MUST include a header comment:

```python
"""
<Platform>Adapter — <Platform> channel adapter.

Ported from hermes-agent (MIT) gateway/platforms/<platform>.py
2026-04-27. Mapping in docs/superpowers/specs/
2026-04-27-platform-reach-port-guide.md.
"""
```

That preserves attribution without requiring a separate LICENSE-NOTICE file.

## What this guide replaces

Without this guide, a fresh Claude session opening "port hermes' webhook adapter" would spend the first 30-45 minutes:

- Reading hermes' channel base class to understand its conventions
- Mapping hermes config-system reads to OC env vars
- Discovering the OC channel SDK contract by grepping
- Working out what to skip (hermes ships features OC doesn't need)
- Building scaffold from scratch

With this guide, those 30-45 minutes drop to 5: the recipe above is the playbook.
