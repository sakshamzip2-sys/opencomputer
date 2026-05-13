# Consuming the `profile.swap` event

Last updated: 2026-05-13. Companion to `docs/superpowers/specs/2026-05-13-profile-handoff-design.md`.

When OC silently swaps the active profile (auto-trigger or `/handoff`), it publishes a `ProfileSwapEvent` to the in-process bus. **Three transport surfaces relay the event to clients** — pick whichever fits your runtime.

## Surface 1: WebSocket (typed) — `oc wire` clients (TUI / IDE / programmatic)

WireServer broadcasts a typed `profile.swap` event globally to every connected WS client.

**Event:** `profile.swap`
**Payload schema:** `ProfileSwapPayload` (`opencomputer/gateway/protocol_v2.py`)

```typescript
type ProfileSwapPayload = {
  from_profile: string;
  to_profile: string;
  trigger: 'auto' | 'manual' | 'cli';
  classifier_confidence: number;   // 0.0 if non-auto
  classifier_reason: string;        // up to 200 chars
  has_handoff: boolean;             // true if handoff doc was written
};
```

**Client integration (TypeScript, raw WS):**

```typescript
const ws = new WebSocket('ws://127.0.0.1:18789');
ws.addEventListener('message', (raw) => {
  const msg = JSON.parse(raw.data);
  if (msg.type === 'event' && msg.event === 'profile.swap') {
    const payload = msg.payload as ProfileSwapPayload;
    showToast(`↪ @${payload.from_profile} → @${payload.to_profile}`);
  }
});
```

No subscription RPC needed — global broadcast reaches every connected client. Replay is NOT buffered (a client that reconnects after the event missed it; fetch active profile via existing RPCs to re-sync).

## Surface 2: Server-Sent Events (projected) — OC webui / hermes-workspace / any HTTP client

The dashboard's `/api/v1/events` SSE endpoint subscribes to the bus with a wildcard pattern. Every `SignalEvent` projects to JSON automatically. **No new route is needed — the event flows through.**

**Endpoint:** `GET /api/v1/events?topics=profile_swap`

**Projection shape** (output of `dataclasses.asdict(event)`):

```json
{
  "event_type": "profile_swap",
  "event_id": "01HX...",
  "timestamp": 1715617921.234,
  "session_id": null,
  "source": "",
  "metadata": {},
  "from_profile": "default",
  "to_profile": "stocks",
  "trigger": "auto",
  "classifier_confidence": 0.87,
  "classifier_reason": "state-query detected",
  "has_handoff": true
}
```

**Client integration (vanilla JS + EventSource — already shipped in `_dashboard.js`):**

```javascript
window.OCDash.subscribeStream(
  '/api/v1/events?topics=profile_swap',
  (data) => renderToast(data),
);
```

The bundled OC webui (`oc webui`) auto-installs a toast renderer via `installProfileSwapToast()` in `_dashboard.js`. Opt out by setting `window.__OC_DISABLE_PROFILE_SWAP_TOAST = true` before `_dashboard.js` loads.

**Client integration (React + native EventSource):**

```typescript
useEffect(() => {
  const es = new EventSource('/api/v1/events?topics=profile_swap');
  es.addEventListener('event', (e: MessageEvent) => {
    const data = JSON.parse(e.data);
    if (data.event_type === 'profile_swap') {
      toast.info(
        `Switched: @${data.from_profile} → @${data.to_profile}` +
        (data.has_handoff ? ' (handoff)' : ''),
      );
    }
  });
  return () => es.close();
}, []);
```

This is the recommended path for `hermes-workspace` since the SPA's existing API client already supports SSE.

## Surface 3: In-runtime — same process as the agent loop

For code running in the same Python process (channel adapter callbacks, plugins, slash commands), read `runtime.custom["profile_swap_notification"]` directly. The dict is set by the orchestrator on every successful auto-swap and persists through `_apply_pending_profile_swap`:

```python
note = runtime.custom.get("profile_swap_notification")
if note:
    cli.print(note["message"])  # "↪ @stocks (handoff written)"
```

This is the CLI status-bar path and is read by any slash command or plugin that runs after the swap.

## Surface 4: Audit query — historical swaps

For "what swaps happened in this session", query the existing `audit_log` table:

```sql
SELECT timestamp, actor, capability_id, decision, reason
FROM audit_log
WHERE action = 'profile_swap'
ORDER BY id DESC
LIMIT 20;
```

DB location: `<profile-home>/consent/audit.db`. The HMAC chain is shared with consent grants — `oc memory doctor` validates the whole chain.

## Failure modes

| Failure | What happens |
|---|---|
| Bus publish fails | Logged at DEBUG; swap proceeds (event delivery is best-effort) |
| WS subscriber raises in handler | Caught + logged WARN; other subscribers unaffected |
| SSE queue full (slow client) | Event dropped for that client; bus + other clients unaffected |
| WireServer not running (e.g. `oc chat` direct mode) | Bus event publishes but nothing forwards to WS; SSE still works if dashboard is up |

## Adding a consumer

Two contracts to honor:

1. **Event-type stability:** the string `"profile_swap"` is load-bearing across SSE wildcard projection, WS broadcast routing, and bus pattern matching. Pinned by `tests/test_handoff_cross_surface.py::test_event_type_is_stable_wire_contract`.
2. **Schema additive-only:** adding a field to `ProfileSwapEvent` / `ProfileSwapPayload` with a default is fine; renaming or removing fields is a breaking change for every consumer.
