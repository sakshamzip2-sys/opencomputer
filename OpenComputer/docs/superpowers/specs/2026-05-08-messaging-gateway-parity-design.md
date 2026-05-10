# Messaging Gateway Parity (Hermes) — Production-Grade Design

**Date:** 2026-05-08
**Working title:** `gateway-parity-2026-05-08`
**Driver doc:** Hermes Agent's full gateway spec (provided in user request).
**Reference upstream:**
`/Users/saksham/Vscode/claude/sources/hermes-agent/gateway/` (21,298 LOC)
`/Users/saksham/Vscode/claude/sources/hermes-agent/hermes_cli/gateway.py` (4,468 LOC)

## 1. Why this scope (production-grade, no v1 cuts)

OpenComputer already ships a substantial gateway engine: a daemon (`opencomputer/gateway/`, 5,125 LOC), 23 channel adapters, queue manager (interrupt/queue/steer/drop_old/summarize), service-install backends (systemd/launchd/schtasks), section-driven setup wizard, channel directory, bindings, voice mode, slash commands (~38 registered), and the cron scheduler.

What's missing — measured against Hermes's 21K-LOC upstream and the user-provided spec page — is **16 production-grade gaps** spanning UX, security, operational reliability, and platform-specific polish. This spec ports each at production quality, not as a minimal stub. No "v1 cut" is taken; every Hermes feature in the page has an OC equivalent in this design.

Memory rule honored (`feedback_discovery_sweep_wildcards`): work *already done* (steer mode in PR #485, voice mode, /background, /title, /usage, /insights, /reasoning, /rollback, /update, channel adapters, service install) is **not re-ported**. Each listed gap below is a confirmed absence.

Memory rule honored (`feedback_parallel_sessions_dont_remove` + `feedback_worktrees_for_parallel_sessions`): execution will use a fresh git worktree off `origin/main`. Two files (`gateway/outgoing_drainer.py`, `gateway/outgoing_queue.py`) carry parallel-session in-flight changes — this design routes around them.

## 2. Goals

1. **Single command verb for the gateway.** All operations under `oc gateway *` (run / setup / install / uninstall / start / stop / restart / status / logs / pairing / sethome).
2. **DM Pairing (production-grade).** Crypto-random code-based authorization; lockout + rate limiting + 0600 file permissions; atomic writes; deep-link URLs (Telegram, Discord) for one-click admin approval; on-demand code regeneration.
3. **Reset Policies (per-platform).** `daily | idle | both | off` modes with per-platform overrides; archives transcripts on reset.
4. **Per-platform display config.** Tier-based defaults (high/medium/low/minimal); resolution order: per-platform → global → built-in default; covers `tool_progress`, `show_reasoning`, `tool_preview_length`, `streaming`, `runtime_footer`, `background_process_notifications`, `busy_ack_enabled`, `busy_input_mode`.
5. **Runtime metadata footer.** Opt-in `display.runtime_footer.enabled` + `fields: [model, context_pct, cwd, …]`; per-platform overrides; `/footer on|off|status`.
6. **Background-process notifications knob.** `all | result | error | off`, per-platform overrideable.
7. **First-time busy-input tip.** One-shot reminder; latched in `~/.opencomputer/<profile>/onboarding.json`.
8. **Status command sophistication.** Detect process-vs-service mismatch; show manual PIDs vs service PIDs; multi-installation aware.
9. **Multi-installation service-name hashing.** `opencomputer-gateway-<hash>` for non-default `OPENCOMPUTER_HOME`; concurrent installs on one host.
10. **Restart with drain timeout.** `--drain-timeout=N`; in-flight messages complete before restart.
11. **Delivery routing parity.** `DeliveryTarget.parse("telegram:123456:thread7")` with origin/local/platform/platform:chat/platform:chat:thread formats.
12. **Cross-session mirror.** Cron-driven sends append "delivery-mirror" entries to the target session's transcript.
13. **Missing slash commands.** `/sethome`, `/voice`, `/approve`, `/deny`, `/status` (session-info form), `/footer`.
14. **Session context via `contextvars.ContextVar`.** Per-task session vars; eliminates the `os.environ` global-overwrite footgun.
15. **Interrupt semantics finalization.** SIGTERM → 1s grace → SIGKILL on background processes; tool-call cancel cascades; message-coalesce-during-busy.
16. **Allowlist env-var conventions.** All Hermes-spec env vars: `TELEGRAM_ALLOWED_USERS`, `DISCORD_ALLOWED_USERS`, `SIGNAL_ALLOWED_USERS`, `SMS_ALLOWED_USERS`, `EMAIL_ALLOWED_USERS`, `MATTERMOST_ALLOWED_USERS`, `MATRIX_ALLOWED_USERS`, `DINGTALK_ALLOWED_USERS`, `FEISHU_ALLOWED_USERS`, `WECOM_ALLOWED_USERS`, `WECOM_CALLBACK_ALLOWED_USERS`, `GATEWAY_ALLOWED_USERS` (catch-all), `GATEWAY_ALLOW_ALL_USERS` (escape hatch).

## 3. Non-goals

- Gateway protocol redesign (`gateway/protocol.py` v1+v2 stays).
- Replacing existing 23 channel adapters (port already complete).
- Re-implementing `steer` (PR #485 covers it).
- Web-UI dashboard (`opencomputer dashboard` is a separate sub-project; gateway operates without it).
- New channel platforms.

## 4. Architecture

```
            user types `oc gateway <subcommand>`
                       │
                       ▼
            opencomputer/cli_gateway.py  (NEW Typer group, 12 subcommands)
                       │
       ┌───────────────┼───────────────────────────────────────┐
       │               │                                       │
       ▼               ▼                                       ▼
  run/foreground   setup/install/start/stop/                pairing/sethome
  (existing body)  restart/status/logs                      subgroups
                   (delegates to service.factory +
                    cli_setup.wizard + status helpers)
                       │
                       ▼  (when run)
                  Gateway.serve_forever()
                       │
        ┌──────────────┴───────────────────────────────┐
        │                                              │
        ▼                                              ▼
  adapter.handle_event                          ResetPolicyChecker.tick()
        │                                       (cron-driven idle/daily reset)
        ▼
  AllowlistGate.check(platform, user_id) ─── env-var + file-overlay + DM-pairing
        │                                       (mints code on miss)
        ▼
  Dispatch.handle_message(MessageEvent)
        │
        ▼
  ResetPolicyChecker.should_reset(platform, chat_id, last_seen)  (NEW)
        │
        ▼
  SessionMap[platform, chat_id] → session_id  (existing)
        │
        ▼
  QueueManager.dispatch  (existing — interrupt/queue/steer/drop_old/summarize)
        │
        ▼
  AgentLoop.run_conversation(...)  →  reply_text + footer + bg-notify
        │
        ▼
  display_config.resolve(platform, key) → renders per-platform display polish
        │
        ▼
  adapter.send(text)  +  mirror_to_session  (NEW for cron-cross-session)
```

The Typer group is the entry point. All other components are additive — existing dispatcher, agent loop, queue manager, channel adapters keep their current public signatures.

## 5. Component designs (16)

### 5.1 `opencomputer/cli_gateway.py` — Typer group (NEW)

```python
gateway_app = typer.Typer(
    name="gateway",
    help="Run, configure, and manage the messaging gateway daemon.",
    invoke_without_command=True,
)

@gateway_app.callback()
def _default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        _run_foreground()  # back-compat: bare `oc gateway` runs

@gateway_app.command("run", help="Run gateway in foreground.")
def run(): _run_foreground()

@gateway_app.command("setup", help="Interactive wizard for messaging platforms.")
def setup(): _run_messaging_only_wizard()

@gateway_app.command("install", help="Install gateway as a user/system service.")
def install(system: bool = False, profile: str = "default"): ...

@gateway_app.command("uninstall", help="Remove the gateway service.")
def uninstall(system: bool = False, profile: str = "default"): ...

@gateway_app.command("start") / stop / restart / status / logs / sethome
gateway_app.add_typer(pairing_app, name="pairing")
app.add_typer(gateway_app, name="gateway")
```

**Backward-compat:** the existing top-level `@app.command def gateway(install_daemon: bool, daemon_profile: str)` is replaced by the Typer group with these compat hooks:
- Bare `oc gateway` → foreground (Typer callback default).
- `oc gateway --install-daemon` flag → routed to `oc gateway install` with a deprecation warning. **Kept indefinitely** (per user requirement: "not just one release").
- `oc gateway-logs` → hidden alias to `oc gateway logs` retained.
- The systemd / launchd unit's `ExecStart=… oc gateway` continues to resolve to "run foreground" so existing service files don't need re-installing.

### 5.2 `opencomputer/channels/pairing_codes.py` — DM Pairing (NEW, full port from Hermes `gateway/pairing.py`)

```python
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 chars, no 0/O/1/I
CODE_LENGTH = 8                                  # 40 bits entropy
CODE_TTL_SECONDS = 3600
RATE_LIMIT_SECONDS = 600                         # 1 req/user/10min
LOCKOUT_SECONDS = 3600                           # platform-wide lockout
MAX_PENDING_PER_PLATFORM = 3
MAX_FAILED_ATTEMPTS = 5

class PairingCodeStore:
    """Owner-restricted file store at <profile>/pairing/.

    Files per platform:
      <profile>/pairing/{platform}-pending.json    (pending requests)
      <profile>/pairing/{platform}-approved.json   (approved users)
      <profile>/pairing/_rate_limits.json          (rate-limit + lockout state)

    Atomic writes via tmpfile + os.replace; chmod 0600 post-rename.
    threading.RLock protects all read-modify-write cycles.
    """
    def is_approved(self, platform: str, user_id: str) -> bool: ...
    def list_approved(self, platform: str | None = None) -> list[dict]: ...
    def revoke(self, platform: str, user_id: str) -> bool: ...

    def generate_code(self, platform: str, user_id: str, user_name: str = "") -> str | None:
        """Returns 8-char base32 code; None on rate-limit/lockout/cap-hit."""

    def regenerate_code(self, platform: str, user_id: str) -> str | None:
        """Force-mint a fresh code, bypassing rate limit but honoring lockout.
        Used by `oc gateway pairing regen <platform> <user_id>` for admin
        UX when a user lost their code."""

    def approve_code(self, platform: str, code: str) -> dict | None: ...
    def list_pending(self, platform: str | None = None) -> list[dict]: ...
    def clear_pending(self, platform: str | None = None) -> int: ...

    def deep_link(self, platform: str, code: str) -> str | None:
        """Return a one-click approval deep-link URL.

        Telegram: https://t.me/<bot>?start=approve_<code>
        Discord:  https://discord.com/channels/@me/<bot>?cmd=approve&code=<code>
        Other platforms: returns None.
        """
```

**Adapter integration:** the dispatcher checks `AllowlistGate` before session lookup. The gate composes (a) env-var allowlist (`TELEGRAM_ALLOWED_USERS=...`), (b) file-overlay allowlist (`<profile>/allowlist.json`), (c) approved-pairing-store. Either source allowing means allowed.

On miss, the gate calls `PairingCodeStore.generate_code(...)`. On success, the dispatcher uses the adapter's existing `send()` to reply with:

> Pairing code: `XKGH5N7P` (expires in 60 minutes)
> Ask the OpenComputer admin to run:
> `oc gateway pairing approve <platform> XKGH5N7P`
> Or click: <https://t.me/MyBot?start=approve_XKGH5N7P>

Rate-limit hits and lockouts produce no reply (silent drop) — admin sees the storm via `oc gateway pairing list --all`.

**CLI:**
```
oc gateway pairing list [--all]                 # pending + approved (table)
oc gateway pairing approve <platform> <code>    # approve
oc gateway pairing revoke <platform> <user_id>  # de-approve
oc gateway pairing regen <platform> <user_id>   # force-mint a fresh code
oc gateway pairing clear-pending [<platform>]   # admin reset of pending queue
oc gateway pairing approve-deeplink <url>       # paste a deep-link URL, parse + approve
```

**Cron sweep:** registered as a 60-second tick in the existing cron scheduler. Calls `_cleanup_expired()` for every platform.

### 5.3 `opencomputer/gateway/reset_policy.py` — session reset (NEW)

```python
@dataclass(frozen=True, slots=True)
class ResetPolicy:
    mode: Literal["off", "daily", "idle", "both"] = "both"
    daily_at_hour: int = 4
    idle_minutes: int = 1440

@dataclass(frozen=True, slots=True)
class ResetPolicyConfig:
    default: ResetPolicy = field(default_factory=ResetPolicy)
    by_platform: dict[str, ResetPolicy] = field(default_factory=dict)

class ResetPolicyChecker:
    def __init__(self, cfg: ResetPolicyConfig, *, now_fn: Callable[[], float] = time.time): ...
    def should_reset(self, platform: str, chat_id: str, last_seen: float) -> tuple[bool, str]:
        """Returns (do_reset, reason). reason ∈ {"idle:<min>", "daily:<hour>", "off"}."""
    def policy_for(self, platform: str) -> ResetPolicy: ...
```

**`GatewayConfig` additions** (`opencomputer/agent/config.py`):

```python
reset_mode: str = "both"             # "off" | "daily" | "idle" | "both"
reset_daily_at_hour: int = 4
reset_idle_minutes: int = 1440
reset_by_platform: dict = field(default_factory=dict)
# YAML form:
# gateway:
#   reset_by_platform:
#     telegram: { mode: idle, idle_minutes: 240 }
#     discord:  { mode: idle, idle_minutes: 60 }
```

**Dispatcher integration** (`opencomputer/gateway/dispatch.py`):

```python
last_seen = self._chat_last_seen.get((platform, chat_id), now)
do_reset, reason = self.reset_policy.should_reset(platform, chat_id, last_seen)
if do_reset:
    self._archive_and_reset(platform, chat_id, reason)
    self._emit_event("session.reset", {"platform": platform, "chat_id": chat_id, "reason": reason})
self._chat_last_seen[(platform, chat_id)] = now
```

`_archive_and_reset`:
1. Calls `SessionDB.archive(session_id)` if available; else moves `<profile>/sessions/<session_id>.jsonl` → `<profile>/sessions/archive/<session_id>-<reason>-<ts>.jsonl`.
2. Drops the SQLite session row.
3. Drops the `(platform, chat_id) → session_id` cache entry.

**`_chat_last_seen` persistence:** memory-resident dict written to `<profile>/gateway/last_seen.json` on every Nth update + on graceful shutdown (atomic write). On boot, load + use.

### 5.4 `opencomputer/gateway/display_config.py` — per-platform display (NEW, port from Hermes)

```python
_GLOBAL_DEFAULTS: dict[str, Any] = {
    "tool_progress": "all",
    "show_reasoning": False,
    "tool_preview_length": 0,
    "streaming": None,
    "background_process_notifications": "all",
    "busy_ack_enabled": True,
    "busy_input_mode": "interrupt",
    "runtime_footer": {"enabled": False, "fields": ["model", "context_pct", "cwd"]},
}

# Tier-based platform defaults:
_TIER_HIGH    = {"tool_progress": "all", "tool_preview_length": 40, ...}  # telegram, discord
_TIER_MEDIUM  = {"tool_progress": "new", "tool_preview_length": 40, ...}  # mattermost, matrix, feishu
_TIER_LOW     = {"tool_progress": "off", "streaming": False, ...}         # signal, bluebubbles, weixin, wecom, dingtalk
_TIER_MINIMAL = {"tool_progress": "off", "tool_preview_length": 0, ...}   # email, sms, webhook, homeassistant
# Slack: medium with tool_progress: off (Bolt posts not edit-friendly)
# whatsapp: medium (Baileys bridge supports edit)

OVERRIDEABLE_KEYS = frozenset(_GLOBAL_DEFAULTS.keys())

def resolve_display_setting(user_config: dict, platform_key: str, setting: str, fallback: Any = None) -> Any:
    """Resolution order:
       1. display.platforms.<platform>.<key>   user override
       2. display.<key>                        user global
       3. _PLATFORM_DEFAULTS[platform][key]    built-in tier default
       4. _GLOBAL_DEFAULTS[key]                built-in global default
       5. fallback                              caller-supplied
    """
```

All current readers of `display.tool_progress` etc. are updated to call `resolve_display_setting(cfg, platform_key, "tool_progress")`. The CLI ambient path passes `platform_key="cli"` (or None). One-line config-migration shim moves any old `display.tool_progress_overrides` flat dict into the new nested `display.platforms.<platform>.<key>` structure.

### 5.5 Runtime footer (refactor + production-port)

`opencomputer/gateway/runtime_footer.py` (existing 101 LOC) is extended with:

```python
@dataclass(frozen=True, slots=True)
class RuntimeFooterConfig:
    enabled: bool = False
    fields: tuple[str, ...] = ("model", "context_pct", "cwd")

def resolve_footer_config(user_config: dict, platform_key: str | None) -> RuntimeFooterConfig:
    """Per-platform-aware merge: built-in default → display.runtime_footer → display.platforms.<p>.runtime_footer."""

def format_runtime_footer(*, model: str, context_tokens: int, context_length: int | None,
                          cwd: str | None = None, fields: Iterable[str]) -> str:
    """Render `model · 32% · ~/code/oc` style line. Empty if no fields have data."""

def append_or_send_trailing(reply_text: str, footer: str, streaming: bool) -> tuple[str, str | None]:
    """If streaming, return (reply_text, footer) — caller sends the footer as a separate trailing message.
    Else return (reply_text + '\\n' + footer, None)."""
```

Also adds the `/footer on|off|status` slash command (CommandDef in `cli_ui/slash.py` plus a handler in `agent/slash_commands_impl/footer_cmd.py`).

### 5.6 Background-process notifications filter (NEW)

`opencomputer/agent/bg_notify.py` (existing) gains a filter at the Notification subscriber:

```python
def _should_emit(payload: BgProcessExit, platform_key: str | None = None) -> bool:
    mode = display_config.resolve_display_setting(
        load_user_config(), platform_key, "background_process_notifications"
    )
    env_override = os.getenv("OPENCOMPUTER_BACKGROUND_NOTIFICATIONS")
    if env_override:
        mode = env_override
    if mode == "off":     return False
    if mode == "all":     return True
    if mode == "result":  return True
    if mode == "error":   return payload.exit_code != 0
    return True
```

Per-platform overrides via `display.platforms.<platform>.background_process_notifications`.

`outgoing_drainer.py` is **NOT touched** (parallel-session in-flight changes there). Filter sits at the Notification subscriber layer, upstream of the drainer.

### 5.7 First-time busy-input tip

`opencomputer/gateway/runtime_footer.py` adds:

```python
def busy_ack_text(cfg: dict, platform_key: str, *, profile_home: Path) -> str:
    base = _format_busy_ack(cfg, platform_key)  # ⚡/⏳/⏩ depending on mode
    latch = _OnboardingLatch(profile_home / "onboarding.json")
    if not latch.seen("busy_input_prompt"):
        latch.mark_seen("busy_input_prompt")  # flock + atomic write
        return base + "\n💡 First-time tip — Hermes-style steer/queue/interrupt modes available via display.busy_input_mode."
    return base

class _OnboardingLatch:
    """flock-protected atomic JSON file at <profile>/onboarding.json.

    Schema: {"seen": {"busy_input_prompt": true, "<key>": true, ...}}
    """
```

### 5.8 Status command sophistication (`opencomputer/cli_gateway_status.py` — NEW)

Port of Hermes `hermes_cli/gateway.py` status logic + `gateway/status.py`:

```python
@dataclass(frozen=True)
class GatewayRuntimeSnapshot:
    manager: str                          # "systemd-user" | "systemd-system" | "launchd" | "schtasks"
    service_installed: bool = False
    service_running: bool = False
    gateway_pids: tuple[int, ...] = ()    # manual PIDs (no service)
    service_scope: str | None = None      # "user" | "system" (Linux only)
    foreign_home_pids: tuple[ProfileGatewayProcess, ...] = ()  # PIDs from other OPENCOMPUTER_HOMEs

    @property
    def running(self) -> bool: ...
    @property
    def has_process_service_mismatch(self) -> bool:
        """Service installed AND running AND service status says NOT running."""
```

`get_gateway_runtime_snapshot()` uses:
- systemd: `systemctl --user list-units opencomputer-gateway*` + `show MainPID`.
- launchd: `launchctl list <label>`.
- schtasks: `schtasks /Query /TN <name>` (Windows).
- pgrep: `pgrep -f "opencomputer.*gateway"` for manual PIDs.

`oc gateway status` renders this snapshot in a Rich panel:

```
OpenComputer gateway
─────────────────────────────────────
Manager:        systemd-user
Service:        installed (active)
                ↳ MainPID: 12345
Manual PIDs:    none
Foreign homes:  none
─────────────────────────────────────
```

`has_process_service_mismatch` → printed as a yellow warning with a "run `oc gateway stop --all`" hint.

### 5.9 Multi-installation service-name hashing

`opencomputer/service/_naming.py` (NEW):

```python
def service_label(profile: str = "default") -> str:
    home = os.environ.get("OPENCOMPUTER_HOME") or str(Path.home() / ".opencomputer")
    canonical_home = str(Path.home() / ".opencomputer")
    if home == canonical_home and profile == "default":
        return "opencomputer-gateway"
    suffix = hashlib.sha256(f"{home}|{profile}".encode()).hexdigest()[:8]
    return f"opencomputer-gateway-{suffix}"
```

systemd/launchd/schtasks backends consume this label; existing single-install setups keep the canonical `opencomputer-gateway` label (no migration needed). Multi-install adds the hash suffix automatically.

`oc gateway status` lists all `opencomputer-gateway*` services across systemd/launchd/schtasks so a user with two installs sees both at once.

### 5.10 Restart with drain timeout

`opencomputer/cli_gateway.py:restart` accepts `--drain-timeout=N` (default 30s):

```python
@gateway_app.command("restart")
def restart(drain_timeout: int = 30, system: bool = False):
    """Stop, wait for in-flight messages to complete (≤drain_timeout), then start."""
    snapshot = get_gateway_runtime_snapshot()
    if snapshot.running:
        _signal_drain(snapshot.service_running, snapshot.gateway_pids)
        _wait_for_drain(snapshot, timeout=drain_timeout)
    _service_restart(system=system)
```

`_signal_drain` writes a `<profile>/gateway/drain.flag` file. `Gateway.serve_forever()` checks this file every poll cycle; on flag-set it stops accepting new messages, waits for `Dispatch._inflight_count == 0`, then exits cleanly.

### 5.11 Delivery routing (`opencomputer/gateway/delivery.py` — NEW, port from Hermes)

```python
@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    platform: Platform
    chat_id: str | None = None       # None means use platform home
    thread_id: str | None = None
    is_origin: bool = False
    is_explicit: bool = False

    @classmethod
    def parse(cls, target: str, origin: SessionSource | None = None) -> "DeliveryTarget":
        """Formats: 'origin' | 'local' | '<platform>' | '<platform>:<chat>' | '<platform>:<chat>:<thread>'."""

    def to_string(self) -> str: ...

class DeliveryRouter:
    """Resolves DeliveryTargets to adapter.send() calls.

    Used by:
      - cron jobs (auto-deliver outputs to a target)
      - /sethome (sets default chat for cron auto-deliver)
      - inter-session message routing
    """
    def __init__(self, gateway: Gateway, mirror: bool = True): ...
    def route(self, message_text: str, targets: list[DeliveryTarget],
              source_label: str = "manual") -> dict[str, bool]: ...
```

Truncation policy: `MAX_PLATFORM_OUTPUT = 4000` (sized to Telegram's safe limit), output > limit gets truncated to `TRUNCATED_VISIBLE = 3800` + `… (truncated, full output saved to <path>)` suffix.

### 5.12 Cross-session mirror (`opencomputer/gateway/mirror.py` — NEW, port from Hermes)

```python
def mirror_to_session(
    platform: str,
    chat_id: str,
    message_text: str,
    source_label: str = "cli",
    thread_id: str | None = None,
    user_id: str | None = None,
) -> bool:
    """Append a 'delivery-mirror' entry to the target session's transcript.

    Used when a cron job or admin command sends to a chat — the receiving
    side's agent has context about what was sent without having generated
    it. Mirror entries carry mirror=True so the agent can distinguish them
    from its own outputs.

    Best-effort: never fatal. Returns True on success, False otherwise.
    """
```

### 5.13 Missing slash commands (4 NEW)

| Command | File | Purpose |
|---|---|---|
| `/sethome` | `opencomputer/agent/slash_commands_impl/sethome_cmd.py` | Set this chat as the home channel for the current platform; persists to `<profile>/gateway/home_channels.json`; consumed by cron auto-deliver. |
| `/voice` | `extensions/voice-mode/slash_commands/voice_cmd.py` | `/voice [on\|off\|tts\|join\|leave\|status]`; controls platform voice replies + Discord voice-channel join/leave. |
| `/approve` | `extensions/coding-harness/slash_commands/approve_cmd.py` | Approve a pending dangerous command (uses existing pending-approval store). |
| `/deny` | `extensions/coding-harness/slash_commands/deny_cmd.py` | Reject a pending dangerous command. |
| `/status` | `opencomputer/agent/slash_commands_impl/status_cmd.py` | Session info: platform, chat_id, session_id, model, queue_mode, last activity. Different from `/config` (which shows config files). |
| `/footer` | `opencomputer/agent/slash_commands_impl/footer_cmd.py` | Toggle `display.runtime_footer.enabled` (already declared in 5.5). |

### 5.14 Session context via `contextvars` (`opencomputer/gateway/session_context.py` — NEW, port from Hermes)

Replaces any `os.environ`-based session-state reads inside the gateway with `contextvars.ContextVar`-backed accessors:

```python
_SESSION_PLATFORM: ContextVar[str | object] = ContextVar("OC_SESSION_PLATFORM", default=_UNSET)
_SESSION_CHAT_ID: ContextVar[str | object] = ContextVar("OC_SESSION_CHAT_ID", default=_UNSET)
# ... THREAD_ID, USER_ID, USER_NAME, KEY, CRON_AUTO_DELIVER_*

def get_session_env(name: str, default: str = "") -> str:
    """Mirror of os.getenv for backward-compatible call sites; reads contextvar first, env second."""

def set_session_vars(*, platform="", chat_id="", thread_id="", user_id="", user_name="", key=""): ...
def clear_session_vars(): ...
```

Audit & migration: grep `opencomputer/` and `extensions/` for `os.getenv("HERMES_SESSION_*")` (likely none) and any `os.environ["OPENCOMPUTER_SESSION_*"]` reads/writes; switch to the contextvar API.

### 5.15 Interrupt semantics finalization

Audit `opencomputer/gateway/dispatch.py` and `opencomputer/agent/loop.py` against Hermes spec:
- **In-flight terminal commands killed:** verify `StartProcess` cancellation path sends SIGTERM with 1-second grace before SIGKILL. (Memory says PR #485 finalized cancellation; this is a confirmation pass.)
- **Tool calls cancelled:** when the queue manager cancels a run, only the *currently executing* tool call completes; the remaining batch is dropped.
- **Multiple messages combined:** when user sends N messages while busy and the busy_input_mode is "interrupt", they are joined into a single prompt before re-dispatch.

Where any of these don't already match, add tests + fix. No new architecture — only correctness.

### 5.16 Allowlist env-var conventions

`opencomputer/channels/allowlist.py` (NEW):

```python
_PLATFORM_ENV_VARS = {
    "telegram":        "TELEGRAM_ALLOWED_USERS",
    "discord":         "DISCORD_ALLOWED_USERS",
    "slack":           "SLACK_ALLOWED_USERS",
    "signal":          "SIGNAL_ALLOWED_USERS",
    "sms":             "SMS_ALLOWED_USERS",
    "email":           "EMAIL_ALLOWED_USERS",
    "mattermost":      "MATTERMOST_ALLOWED_USERS",
    "matrix":          "MATRIX_ALLOWED_USERS",
    "dingtalk":        "DINGTALK_ALLOWED_USERS",
    "feishu":          "FEISHU_ALLOWED_USERS",
    "wecom":           "WECOM_ALLOWED_USERS",
    "wecom_callback":  "WECOM_CALLBACK_ALLOWED_USERS",
    "whatsapp":        "WHATSAPP_ALLOWED_USERS",
    "weixin":          "WEIXIN_ALLOWED_USERS",
    "yuanbao":         "YUANBAO_ALLOWED_USERS",
    "qq":              "QQ_ALLOWED_USERS",
    "bluebubbles":     "BLUEBUBBLES_ALLOWED_USERS",
    "homeassistant":   "HOMEASSISTANT_ALLOWED_USERS",
    "irc":             "IRC_ALLOWED_USERS",
    "teams":           "TEAMS_ALLOWED_USERS",
}

class AllowlistGate:
    def check(self, platform: str, user_id: str) -> AllowlistDecision:
        """Compose:
          1. GATEWAY_ALLOW_ALL_USERS=true → allow always (escape hatch).
          2. <PLATFORM>_ALLOWED_USERS env (CSV) → allow if member.
          3. GATEWAY_ALLOWED_USERS env (CSV) → catch-all.
          4. <profile>/allowlist.json → file-based overlay.
          5. PairingCodeStore.is_approved(...) → DM-pairing approvals.
        Default: deny.
        """

@dataclass(frozen=True, slots=True)
class AllowlistDecision:
    allowed: bool
    source: str   # "env-platform" | "env-global" | "file" | "pairing-approved" | "allow-all"
    pairing_code: str | None = None  # populated when allowed=False and a code was just minted
```

Dispatcher uses `decision.allowed`. On miss, the dispatcher uses `decision.pairing_code` (already minted by the gate) to format the reply.

## 6. Configuration summary

```yaml
# ~/.opencomputer/<profile>/config.yaml

gateway:
  reset_mode: both                  # off | daily | idle | both
  reset_daily_at_hour: 4
  reset_idle_minutes: 1440
  reset_by_platform:
    telegram: { mode: idle, idle_minutes: 240 }
    discord:  { mode: idle, idle_minutes: 60 }

display:
  busy_ack_enabled: true                       # global default
  busy_input_mode: interrupt                   # interrupt | queue | steer
  background_process_notifications: all        # all | result | error | off
  tool_progress: all                           # all | new | off | verbose
  show_reasoning: false
  tool_preview_length: 0
  streaming: null                              # null = follow top-level streaming
  runtime_footer:
    enabled: false
    fields: [model, context_pct, cwd]

  platforms:
    telegram:
      tool_progress: all
      tool_preview_length: 60
    slack:
      tool_progress: off
      runtime_footer:
        enabled: true
        fields: [model, context_pct]
    email:
      tool_progress: off
      streaming: false
```

Environment variables (all optional):
```
GATEWAY_ALLOWED_USERS=...                       # catch-all
GATEWAY_ALLOW_ALL_USERS=false                   # escape hatch (DANGEROUS)
TELEGRAM_ALLOWED_USERS=...                      # per-platform
DISCORD_ALLOWED_USERS=...
... (all 19 platforms in §5.16)
OPENCOMPUTER_BACKGROUND_NOTIFICATIONS=result    # overrides config
OPENCOMPUTER_HOME=/path/to/alt/home             # multi-install
```

## 7. CLI surface (final)

```
oc gateway                                  # foreground (back-compat)
oc gateway run                              # explicit
oc gateway setup                            # wizard scoped to messaging-platforms section
oc gateway install [--system] [--profile=N]
oc gateway uninstall [--system] [--profile=N]
oc gateway start [--system]
oc gateway stop  [--system]
oc gateway restart [--drain-timeout=30] [--system]
oc gateway status                           # rich panel + foreign-home detection + mismatch warning
oc gateway logs [--system] [--follow]       # alias of oc gateway-logs

oc gateway sethome <platform> <chat_id> [--thread <thread_id>]
oc gateway sethome --list                   # show home channels
oc gateway sethome --clear <platform>

oc gateway pairing list [--all]
oc gateway pairing approve <platform> <code>
oc gateway pairing approve-deeplink <url>
oc gateway pairing revoke <platform> <user_id>
oc gateway pairing regen <platform> <user_id>
oc gateway pairing clear-pending [<platform>]
```

Backward-compat: `oc gateway --install-daemon` (deprecation warning, kept indefinitely); `oc gateway-logs` (hidden alias).

## 8. Failure modes

| Path | Failure | Behavior |
|---|---|---|
| Pairing-store JSON corrupt | Decode error | Back up to `*.corrupt.<ts>`, start fresh, log + admin alert. |
| Pairing rate-limit hit | Bot silently ignores | Log warning. Admin sees via `oc gateway pairing list --all`. |
| Pairing lockout | Platform locked for 1h | All new pairing attempts return None; CLI shows lockout countdown. |
| Reset policy fires mid-burst | First-message reset; in-flight burst-merge unaffected | Documented; tested. |
| `display.background_process_notifications=off` mid-build | Subscriber drops Notification; `bg_notify` store still tracks | Acceptable. |
| `_chat_last_seen` file corruption | JSON-decode error | Reset to empty; first message after restart gets a "fresh" reset. Documented. |
| Service-name hash collision | Two `OPENCOMPUTER_HOME` paths produce identical sha256 prefix | sha256[:8] = 32 bits = 1 collision per ~65K installs. Acceptable. Detect on `install`: refuse + suggest `--service-suffix`. |
| Drain timeout exceeded | `_wait_for_drain` returns False after N seconds | Force-stop; warn user + log inflight count. |
| Multi-install foreign-home detection | Two daemons race for the same `(platform, chat_id)` | Foreign-home PID listing flagged in `status`; user resolves manually. |
| `OPENCOMPUTER_HOME` flip mid-session | systemd unit env still points to old path | Documented: re-install service after `OPENCOMPUTER_HOME` change. |
| Onboarding latch race (two-process boot) | flock serializes; loser sees latch + skips | One-shot tip preserved. |
| Pairing deep-link expired code | `approve` returns None | `oc gateway pairing approve-deeplink` reports "code expired/invalid"; user runs `regen`. |
| Allowlist env-var contains non-numeric | Parse error | Log warning, treat list as empty; do NOT crash daemon. |

## 9. Testing strategy

| Surface | Tests (count) |
|---|---|
| Typer group structure + back-compat | 12 |
| `cli_gateway.py` per-subcommand smoke | 11 |
| Pairing store: mint/approve/revoke/regen/list/sweep/lockout/rate-limit/corruption-recovery | 18 |
| Allowlist gate composition (env + file + pairing) | 14 |
| Reset policy: idle/daily/both/off + per-platform overrides + dispatcher integration + archive | 14 |
| Display-config resolution order + tier defaults + platform overrides + migration | 16 |
| Runtime footer: per-platform resolve + format + streaming-trailing | 9 |
| Background-process notifications filter (modes × platforms) | 8 |
| First-time tip: latch + flock + once-only | 5 |
| Status: process-service mismatch + manual-PID detection + foreign-home + multi-installation | 12 |
| Service-name hashing: canonical preserved + multi-install hashing + collision detection | 6 |
| Restart with drain: signal + wait + timeout-fallback | 6 |
| Delivery routing: parse all formats + truncation + cron auto-deliver | 11 |
| Cross-session mirror: session-find + JSONL append + SQLite append + best-effort | 7 |
| Slash commands: /sethome, /voice, /approve, /deny, /status, /footer | 12 |
| Session context (contextvars) — concurrency safety vs old env approach | 7 |
| Interrupt semantics: SIGTERM→SIGKILL, tool-cancel cascade, message coalesce | 8 |
| Allowlist env-vars: all 19 platforms parse correctly | 19 |
| **Total** | **205 new tests** |

Existing 5800+ tests must remain green. No public-API breakage.

## 10. Roll-out plan

Two PRs (split for review tractability; each independently shippable):

**PR-1 — Gateway UX consolidation + DM Pairing + Reset Policy + Allowlist + Status + Multi-install hashing.**
Components 5.1, 5.2, 5.3, 5.8, 5.9, 5.16. ~1800 LOC + 75 tests.

**PR-2 — Per-platform display + Runtime footer + bg-notify filter + first-time tip + delivery + mirror + slash commands + contextvars + interrupt-finalization.**
Components 5.4, 5.5, 5.6, 5.7, 5.10, 5.11, 5.12, 5.13, 5.14, 5.15. ~2200 LOC + 130 tests.

Each PR has a single `feat(gateway): …` commit on its dedicated worktree branch. Both worktrees branch off `origin/main` to honor `feedback_worktrees_for_parallel_sessions`.

Default config values match the Hermes spec; safe-by-default (`GATEWAY_ALLOW_ALL_USERS=false`, `reset_mode=both`, `idle_minutes=1440`, `daily_at_hour=4`, `background_process_notifications=all`).

Migration: a release-note line — `oc gateway --install-daemon` flag is deprecated (still works); `display.tool_progress_overrides` flat dict auto-migrates to `display.platforms.<platform>.<key>`. No automatic migration needed.

## 11. Open questions resolved

- **Q:** Bare `oc gateway` becomes a Typer group — does Typer's `invoke_without_command=True` callback handle it cleanly? **A:** Yes; verified pattern in `cli.py:2952` (config_app, profile_app etc. use the same flag).
- **Q:** Does `SessionDB.archive` exist? **A:** Verify at execution; if missing, fallback is a transcript-file move + DB row drop (no functional gap).
- **Q:** Does `outgoing_drainer.py` carry parallel-session changes? **A:** Yes (in-flight backoff work); spec deliberately routes bg-notify filter through `bg_notify.py` to avoid touching it.
- **Q:** Do `os.environ`-based session vars exist in OC today? **A:** Verify with grep `OPENCOMPUTER_SESSION_*` at execution; if present, replace via a single PR commit. If absent, the `session_context.py` module is foundational for future cron-driven cross-session use.
- **Q:** Will the existing 23 channel adapters require source-edits to consume per-platform display config? **A:** No — `resolve_display_setting()` is consumed where the config is *read* (dispatcher, drainer, agent loop). Adapter source files are not touched.

## 12. Spec self-review

- Placeholder scan: no TBD/TODO sections.
- Internal consistency: §4 architecture, §5 component designs, §7 CLI surface match.
- Scope check: Two PRs, each ≤2200 LOC + ≤130 tests; deliverable in one focused execution session.
- Ambiguity check: bg-notify "result" mode means "emit on completion regardless of exit code"; "error" means "only on non-zero exit." Reset policy's "in-flight reset" path is explicit. Pairing rate-limit silently drops (no reply) vs. lockout (CLI countdown) is explicit.
- YAGNI re-check: every feature traces to a Hermes-spec line OR a memory-rule footgun. No speculative additions.
