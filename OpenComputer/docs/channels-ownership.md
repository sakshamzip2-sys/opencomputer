# Channel Ownership Model

## TL;DR

OpenComputer is the **sole channel handler** for all messaging platforms (Telegram, Discord, Slack, Matrix, Email, WhatsApp, Webhook, Signal, SMS, IRC, iMessage, HomeAssistant, DingTalk, Feishu).

When the gateway daemon starts, it runs a **preflight check** that scans the process table for known competitors (Claude Code's `--channels` bridges, Hermes daemons, rival `oc gateway` instances). If any are found:

- **Default**: refuse to start with a clear error naming the offender PID and cmdline.
- **With `gateway.takeover_on_start: true` in config.yaml** (or `--force-takeover` flag): SIGTERM the competitors, escalate to SIGKILL after a 5-second grace, append an audit log entry, then proceed.

This was the architectural fix for the **2026-05-08 silent-reply incident** where Claude Code's `claude --channels plugin:telegram` bun bridge had been silently competing with OC's adapter for the same Telegram polling slot for hours, causing a 33-hour SQLite error loop and zero replies delivered.

## Why polling exclusivity is fundamental, not just a Telegram quirk

Most chat platforms enforce *one consumer per credential* at the API layer:

| Platform | Single-consumer enforcement |
|---|---|
| Telegram | `getUpdates` long-poll: 409 Conflict if two clients poll same bot token |
| Discord | Gateway WebSocket: only one connection per bot at a time; second disconnects the first |
| Slack | Socket Mode: app-level connection enforced one-at-a-time |
| Matrix | Per-device tokens; sync token races are silently inconsistent if shared |
| WhatsApp | One session per account on the underlying Baileys connection |

**Webhook mode doesn't fix this** for most platforms — `setWebhook` is "last writer wins", and if two services share a token they can clobber each other's URLs back and forth indefinitely.

The architecturally correct answer is **enforce a single owner**, not "find a transport that hides the conflict."

## How preflight works

```
┌──────────────────────────────────────────────────────────────────┐
│                    Gateway.start() flow                          │
└──────────────────────────────────────────────────────────────────┘

  ┌──────────────────────┐
  │ _run_channel_        │
  │ ownership_preflight()│
  └──────────┬───────────┘
             │
             ▼
  ┌──────────────────────┐    ┌─────────────────────────┐
  │ detect_competitors() │───▶│ ps -eo pid,args          │
  └──────────┬───────────┘    │ + regex match            │
             │                └─────────────────────────┘
             │
             ▼
       ┌──────────┐
       │ Found?   │
       └────┬─────┘
            │
   ┌────────┴────────┐
   │ no              │ yes
   ▼                 ▼
┌──────────┐   ┌────────────────────────┐
│ Continue │   │ takeover_on_start=true?│
│ adapters │   └─────────┬──────────────┘
└──────────┘             │
                ┌────────┴────────┐
                │ false           │ true
                ▼                 ▼
       ┌────────────────┐  ┌──────────────────────┐
       │ raise          │  │ takeover():          │
       │ ChannelOwner-  │  │  SIGTERM + 5s grace  │
       │ shipConflict   │  │  → SIGKILL if alive  │
       │ — gateway      │  │  → write audit log   │
       │ stays stopped  │  │ then continue        │
       └────────────────┘  └──────────────────────┘
```

## Configuration

### Default (refuse-and-fail-loud)

The gateway will REFUSE to start if any competitor is detected. The operator must intervene by:

1. Running `oc service preflight --force-takeover` once to clear the field
2. Or stopping the competitor manually (`kill -TERM <pid>`)
3. Or setting takeover automatic via config (below)

### Auto-takeover (recommended for users with one OC instance)

```yaml
# ~/.opencomputer/<profile>/config.yaml
gateway:
  takeover_on_start: true
  takeover_grace_seconds: 5.0  # default
```

With this, the gateway terminates competitors on every start. Useful when:

- You're the only OC instance on this machine
- Other tools (Claude Code's `--channels`, Hermes, ...) shouldn't be handling channels at all

### Disabling preflight entirely

You can't. Preflight is mandatory because silent failures here cause hours of debugging. If you genuinely have multiple legitimate consumers (e.g., a webhook-mode OC + a poll-mode OC for redundancy), use **different bot tokens** — one per consumer.

## Audit log

Every takeover writes a JSONL record to:

```
<profile_home>/audit/competitor-takeover.jsonl
```

Schema:

```json
{
  "ts": "2026-05-08T03:42:11.123456+00:00",
  "pid": 7983,
  "kind": "claude_code_telegram_bridge",
  "cmdline_preview": "/Users/saksham/.bun/bin/bun server.ts",
  "signal": "SIGTERM",
  "exit_code": "clean_sigterm"
}
```

`signal` is one of: `already_dead`, `SIGTERM`, `SIGKILL`. `exit_code` is one of: `already_dead`, `clean_sigterm`, `clean_sigkill`, `still_alive`, `signal_refused`.

Append-only; never rotated by OC. Operator handles retention.

## Known competitor patterns

The detector matches these regexes (case-insensitive) against `ps -eo args`:

| Kind | Pattern |
|---|---|
| `claude_code_telegram_bridge` | `claude-plugins-official/telegram` OR `claude.*channels.*plugin:?telegram` |
| `hermes_gateway` | `hermes[_-]?cli(\.main)?\s+gateway` OR `hermes[_-]?agent.*gateway` |
| `rival_oc_gateway` | `opencomputer\b.*\bgateway` OR `/oc\b.*\bgateway` OR `\boc\s+...gateway\b` |

To add a new competitor pattern (e.g., a future Discord bridge from another tool), edit `_COMPETITOR_PATTERNS` in `opencomputer/gateway/preflight.py`.

## CLI

```bash
# Read-only check — lists competitors, exits 1 if any
oc service preflight

# Terminate competitors with audit log
oc service preflight --force-takeover

# Doctor row — same data, surfaced alongside other health checks
oc doctor | grep "telegram polling slot"
```

## Recommended operational hygiene

1. **Disable Claude Code's `telegram@claude-plugins-official`** in `~/.claude/settings.json`. The bun bridge is harmless when nothing else handles the bot, but it's the most common preflight competitor for this user group.
2. **Don't run Hermes if you've migrated to OC.** Same bot, two daemons → one always loses.
3. **Set `takeover_on_start: true`** if you accept "OC always wins" as your invariant.
4. **Audit `<profile_home>/audit/competitor-takeover.jsonl`** periodically to spot accidental respawns (e.g., a launchd plist you forgot to disable).

## Future work (not in this PR)

- **Webhook-mode setup helper** (`oc channels webhook setup`) — automated tunnel detection (cloudflared/ngrok), `setWebhook` registration, cross-restart persistence. Webhook mode + ownership enforcement is the gold standard for production deployment.
- **Cross-machine channel ownership** — current preflight is local-process-only. Two OC instances on different hosts could still collide. Solution: a shared lock service (Redis/etcd) — out of scope for laptop deployments.
- **Channel-agnostic generalization** — Discord, Slack, Matrix, etc. each have their own competitor patterns; current detector covers Telegram + Hermes + OC. Add patterns as new bridges become common.
