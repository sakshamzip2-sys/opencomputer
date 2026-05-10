# `opencomputer/gateway/` — channel-agnostic dispatch + wire protocol

The gateway translates between channel adapters (Telegram / Discord /
Slack / etc.) and the agent loop. Every channel adapter implements one
contract — `BaseChannelAdapter` — and the gateway treats them uniformly.

## Layout

```
opencomputer/gateway/
├── server.py        # Gateway daemon — registers adapters, serves forever
├── dispatch.py      # MessageEvent → AgentLoop routing + typing heartbeat
├── protocol.py      # WireRequest/Response/Event v1 (JSON over WebSocket)
├── protocol_v2.py   # Per-method/event typed schemas extending v1 (Phase 12g)
├── wire_server.py   # WebSocket server that speaks the protocol
└── builtin_hooks/   # (none yet)
```

## The dispatcher's contract

`Dispatch.handle_message(MessageEvent)` is the only entry point channel
adapters need. It:

1. Maps `(platform, chat_id) → session_id` deterministically (sha256 of
   the pair, first 32 hex chars). Same chat → same session, forever.
2. Acquires a per-chat `asyncio.Lock` so two messages from the same chat
   never interleave through the agent loop.
3. Starts a typing-indicator heartbeat (every 4s; Telegram's typing
   state expires at ~5s).
4. Calls `loop.run_conversation(...)` with the user's text + session id.
5. Returns the assistant's final text for the adapter to send back.

If a new adapter's platform has special semantics (Slack threads,
Discord guild scoping), encode those in the `MessageEvent.metadata`
dict — never branch in the dispatcher. The dispatcher must stay
platform-agnostic.

## What channel adapters MAY do

- Implement `BaseChannelAdapter` (`connect/disconnect/send/send_typing`)
- Override `send_image`, `send_notification` if the platform supports
  richer payloads
- Translate inbound platform events into `MessageEvent` and call
  `self.handle_message(event)` (which routes through the gateway-set
  message handler)

## What channel adapters MAY NOT do

- Import from `opencomputer.gateway.dispatch` or `.server` directly.
  The gateway hands you a message handler via `set_message_handler`.
  Use it.
- Import from `opencomputer.agent.*`. Channel adapters never see the
  agent loop — that's the gateway's job.
- Mutate `Gateway.adapters` or any other internal state.

## Protocol versioning

`protocol.py` is v1 — generic `params: dict[str, Any]` request shape.
That works but loses every type guarantee at the wire. Wire clients
(TUI, web dashboard, IDE bridges) silently desync when a new field
lands.

`protocol_v2.py` adds per-method and per-event typed schemas WITHOUT
breaking v1 callers (it imports + re-exports the v1 base types).
New code should reach for `protocol_v2.<method>RequestParams` etc.;
existing wire callers still work via the generic v1 types.

## Boundary

- Gateway MAY import from `opencomputer.agent.loop` (the loop is its
  consumer), `opencomputer.tools.registry` (for capability listing),
  and `plugin_sdk.*` (for channel adapter types).
- **Channel-adapter plugins** MUST NOT import from `opencomputer.gateway.*`
  except `BaseChannelAdapter` from `plugin_sdk.channel_contract`. Use
  the message handler the gateway sets on you, not the gateway directly.

## Bus → Wire bridge (Tier-C of 2026-05-10 memory-observability design)

`WireServer` bridges select in-process bus events to all connected WS
clients. The first instance is `MemoryWriteEvent → EVENT_MEMORY_WRITE`.

**Adding a new bridged event** (template — duplicate this once a second
event needs broadcasting):

1. Add a `EVENT_FOO = "foo.bar"` constant to `protocol.py` and the
   matching `FooPayload(_StrictModel)` to `protocol_v2.py`. Register in
   `EVENT_SCHEMAS` dict.
2. In `WireServer.__init__`, add `self._foo_subscription: Any | None = None`.
3. In `WireServer.start()` after the `websockets.serve` line, subscribe:
   `self._foo_subscription = default_bus.subscribe("foo_event_type", self._on_foo_bus_event)`.
4. In `WireServer.stop()` BEFORE the server.close line: unsubscribe + nil
   the handle.
5. Implement `_on_foo_bus_event(event)` as a SYNC handler (the bus
   publish path is sync-only — async handlers are silently SKIPPED).
   Use `asyncio.run_coroutine_threadsafe(self._broadcast_global(...),
   self._loop_ref)` to hop into the wire server's loop. Wrap in
   try/except — observability never breaks a publisher.
6. `_broadcast_global` already handles fan-out to `_session_clients_all`
   with per-client error isolation.

**When to broadcast vs. session-key**: if the event has a meaningful
`session_id`, prefer per-session keying (mirror
`broadcast_permission_request` at line 552). Use `_broadcast_global` only
for per-process events that affect every client (memory caps, scheduler
ticks, future global telemetry).

**Replay**: `_session_rings` is per-session. Global broadcasts are NOT
buffered, so a client that reconnects after a global event misses it.
Accept this gap; clients pull current state via REST/RPC on reconnect.

## Initial-state RPC pattern (`memory.status`)

Companion to a global-broadcast event — closes the "fresh-connect
blindness" gap that broadcasts can't fix on their own. Pattern:

1. Add `METHOD_FOO_STATUS = "foo.status"` to `protocol.py` plus typed
   `FooStatusParams` (usually empty) and `FooStatusResult` in
   `protocol_v2.py`. Register in `METHOD_SCHEMAS` + `__all__`.
2. Add a dispatch case in `WireServer._dispatch` that resolves the
   active loop, calls a `_collect_foo_status` static helper, returns
   the typed payload via `_send_response`.
3. The collector helper does all the I/O and degrades gracefully —
   missing dependency → empty result, per-item failure → omit that item +
   WARN log. Never raise out of the helper.
4. Update the hello-handshake's `methods` list so capability-detecting
   clients see the new RPC at handshake time.
5. (Optional) Mirror the same response shape as a REST endpoint under
   `opencomputer/dashboard/routes/foo.py` so the dashboard SPA can fetch
   it without speaking WS — use the same typed schema for cross-surface
   consistency. See `opencomputer/dashboard/routes/memory.py` and the
   `MemoryStatusResult` schema for the canonical example.

**When to add a status RPC**: any time you've added a global-broadcast
event whose state is non-trivially derivable from history alone — i.e.
the event is a delta and the client needs the snapshot to render
correctly. If the event IS the full snapshot (e.g. a config-changed
event that always carries the full new config), no status RPC needed.
