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
