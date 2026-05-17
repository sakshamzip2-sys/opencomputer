"""
Wire server — WebSocket JSON-RPC server for TUI / IDE / web clients.

Listens on 127.0.0.1:<port> by default. Each connection is independent —
clients send WireRequests, receive WireResponses (and server-pushed
WireEvents during long-running calls like chat).

Supported methods:
  hello                — handshake, returns server capabilities
  chat                 — send a user message, stream assistant response
  sessions.list        — list recent sessions
  search               — FTS5 search across session history
  skills.list          — list available skills

New methods can be added by plugins in a future phase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import deque
from typing import TYPE_CHECKING, Any

import websockets

from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.steer import default_registry as _steer_registry
from opencomputer.gateway.protocol import (
    EVENT_ASSISTANT_MESSAGE,
    EVENT_ERROR,
    EVENT_EVOLUTION_TUNING_CHANGED,
    EVENT_MEMORY_WRITE,
    EVENT_PERMISSION_REQUEST,
    EVENT_PROFILE_SWAP,
    EVENT_STREAM_RETRY,
    EVENT_TURN_BEGIN,
    EVENT_TURN_END,
    METHOD_CHAT,
    METHOD_CHECKPOINTS_DELETE,
    METHOD_CHECKPOINTS_LIST,
    METHOD_CONFIG_GET,
    METHOD_CONFIG_SET,
    METHOD_EVOLUTION_STATUS,
    METHOD_HELLO,
    METHOD_MEMORY_STATUS,
    METHOD_MODEL_OPTIONS,
    METHOD_MODEL_SET,
    METHOD_PERMISSION_RESPONSE,
    METHOD_SEARCH,
    METHOD_SESSION_DELETE,
    METHOD_SESSION_FORK,
    METHOD_SESSION_INTERRUPT,
    METHOD_SESSION_LIST,
    METHOD_SESSION_MOST_RECENT,
    METHOD_SESSION_RENAME,
    METHOD_SESSION_RESUME,
    METHOD_SESSION_USAGE,
    METHOD_SKILL_SHOW,
    METHOD_SKILLS_LIST,
    METHOD_SLASH_DISPATCH,
    METHOD_SLASH_LIST,
    METHOD_STEER_SUBMIT,
    METHOD_SUBAGENTS_LIST,
    METHOD_TOOLS_LIST,
    WireEvent,
    WireRequest,
    WireResponse,
)

#: v1.1 plan-1 M3.3 (2026-05-09) — per-session ring-buffer capacity.
#: 200 events covers a long-running turn with many tool calls without
#: bounded memory ballooning. A reconnecting client whose
#: ``last_event_seq`` is older than this buffer gets ``gap_warning=True``
#: in the HelloResult so it can decide whether to re-fetch state.
RING_BUFFER_MAX = 200

if TYPE_CHECKING:
    from opencomputer.gateway.agent_router import AgentRouter

logger = logging.getLogger("opencomputer.gateway.wire_server")


class WireServer:
    """Minimal JSON-RPC-over-WebSocket server for local clients.

    Accepts either a pre-built ``AgentLoop`` (legacy single-loop path) or
    an ``AgentRouter`` (multi-profile path). The two arguments are mutually
    exclusive — pass exactly one.

    For v1, wire clients always get the default profile (per-call profile
    binding via a ``profile_id`` field in the RPC params is deferred to
    v1.1). The ``loop=`` legacy path wraps the supplied loop into a
    one-entry router seeded as ``"default"`` so all dispatch goes through
    the same router code path regardless of caller style.
    """

    def __init__(
        self,
        loop: AgentLoop | None = None,
        *,
        router: AgentRouter | None = None,
        host: str = "127.0.0.1",
        port: int = 18789,
    ) -> None:
        if router is not None and loop is not None:
            raise ValueError("WireServer: pass either loop or router, not both")
        if router is None and loop is None:
            raise ValueError("WireServer: pass either loop or router")

        if router is None:
            # Legacy single-loop path: wrap the loop into a one-entry router
            # seeded as "default" so all dispatch uses the same router path.
            from opencomputer.agent.config import _home as _resolve_home
            from opencomputer.gateway.agent_router import AgentRouter

            _captured_loop = loop  # capture for the lambda closures
            router = AgentRouter(
                loop_factory=lambda pid, home: _captured_loop,
                profile_home_resolver=lambda pid: _resolve_home(),
            )
            router._loops["default"] = loop

        self._router: AgentRouter = router
        # Legacy attribute: preserved so existing test/caller code that reads
        # ``server.loop`` directly continues to work. For router-only
        # construction this will be None — callers must use ``_router``
        # directly (v1.1+).
        self.loop: AgentLoop | None = loop
        self.host = host
        self.port = port
        self._server: websockets.WebSocketServer | None = None
        # M3.3 — per-session ring buffer of recently-emitted events.
        # ``deque(maxlen=RING_BUFFER_MAX)`` evicts oldest on overflow;
        # ``_session_seq`` carries a monotonic counter so a reconnecting
        # client can ask for ``last_event_seq + 1 ..`` and get back any
        # events it missed. Both are keyed on session_id; absent session
        # → no buffering (anonymous wire calls don't need replay).
        self._session_rings: dict[str, deque[WireEvent]] = {}
        self._session_seq: dict[str, int] = {}
        # M3.1 — per-session set of currently-connected wire clients,
        # used by the permission-request producer to broadcast
        # ``permission.request`` events to every reachable client on
        # the session. The first client to respond wins; later
        # responders see ``resolved=False`` from
        # ``ConsentGate.resolve_pending`` (no-op on already-resolved).
        self._session_clients: dict[
            str, set[websockets.WebSocketServerProtocol]
        ] = {}
        # Tier-C of 2026-05-10 memory-observability design — every
        # currently-connected ws regardless of session binding. Used by
        # ``_broadcast_global`` for events that are per-process and don't
        # carry a ``session_id`` (e.g. ``MemoryWriteEvent``). Updated in
        # ``_handle_client`` connect/disconnect; intentionally distinct
        # from ``_session_clients`` because anonymous wire calls (no hello
        # session_id) still need to receive memory-write broadcasts.
        self._session_clients_all: set[websockets.WebSocketServerProtocol] = set()
        # Saved at ``start()`` so the sync bus handler can schedule
        # ``_broadcast_global`` coroutines onto the wire-server's loop
        # via ``asyncio.run_coroutine_threadsafe``. None until start().
        self._loop_ref: asyncio.AbstractEventLoop | None = None
        # Subscription handle so ``stop()`` can cleanly unsubscribe and
        # avoid leaking subscribers across test/server lifecycles.
        self._memory_write_subscription: Any | None = None
        # 2026-05-11 — evolution-tuning bus → wire bridge. Same pattern
        # as memory_write_subscription; unsubscribed on stop() to avoid
        # leaks across test/server lifecycles.
        self._evolution_tuning_subscription: Any | None = None
        # 2026-05-13 — profile-swap bus → wire bridge. Surfaces
        # ProfileSwapEvent globally so workspace SPA / TUI / IDE clients
        # render a swap notification without polling.
        self._profile_swap_subscription: Any | None = None

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handle_client, self.host, self.port
        )
        # Tier-C: capture the running loop ref so the sync bus handler
        # can schedule async broadcasts via run_coroutine_threadsafe.
        # get_running_loop is guaranteed to succeed here — start() is
        # always awaited.
        self._loop_ref = asyncio.get_running_loop()
        try:
            from opencomputer.ingestion.bus import default_bus

            self._memory_write_subscription = default_bus.subscribe(
                "memory_write", self._on_memory_write_bus_event
            )
        except Exception:
            # Bus unavailable in some lightweight test harnesses; the wire
            # server still works for chat/sessions/etc. without it.
            logger.exception(
                "wire: failed to subscribe to default_bus for memory.write; "
                "TUI memory panel will not receive events"
            )

        # 2026-05-11 — evolution-tuning bus → wire bridge.
        try:
            from opencomputer.ingestion.bus import default_bus as _bus_evo

            self._evolution_tuning_subscription = _bus_evo.subscribe(
                "evolution_tuning_changed",
                self._on_evolution_tuning_bus_event,
            )
        except Exception:
            logger.exception(
                "wire: failed to subscribe to default_bus for "
                "evolution.tuning_changed; dashboards won't receive tuning events"
            )

        # 2026-05-13 — profile-swap bus → wire bridge.
        try:
            from opencomputer.ingestion.bus import default_bus as _bus_ps

            self._profile_swap_subscription = _bus_ps.subscribe(
                "profile_swap",
                self._on_profile_swap_bus_event,
            )
        except Exception:
            logger.exception(
                "wire: failed to subscribe to default_bus for "
                "profile.swap; workspace/TUI won't receive swap events"
            )

        logger.info("wire: listening on ws://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        # Tier-C: unsubscribe from the bus FIRST so a late publish during
        # shutdown can't enqueue a broadcast onto a closing loop.
        if self._memory_write_subscription is not None:
            try:
                self._memory_write_subscription.unsubscribe()
            except Exception:
                logger.exception("wire: memory_write unsubscribe failed (ignored)")
            self._memory_write_subscription = None
        if self._evolution_tuning_subscription is not None:
            try:
                self._evolution_tuning_subscription.unsubscribe()
            except Exception:
                logger.exception(
                    "wire: evolution_tuning unsubscribe failed (ignored)"
                )
            self._evolution_tuning_subscription = None
        if self._profile_swap_subscription is not None:
            try:
                self._profile_swap_subscription.unsubscribe()
            except Exception:
                logger.exception(
                    "wire: profile_swap unsubscribe failed (ignored)"
                )
            self._profile_swap_subscription = None
        self._loop_ref = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(
        self, ws: websockets.WebSocketServerProtocol
    ) -> None:
        client_id = str(uuid.uuid4())[:8]
        logger.info("wire: client %s connected", client_id)
        # Tier-C: register for global broadcasts (memory.write etc.). The
        # session-keyed _session_clients registry is populated lazily in
        # _dispatch when the client provides session_id via hello/chat.
        self._session_clients_all.add(ws)
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send_response(
                        ws, "", False, error="invalid JSON"
                    )
                    continue

                if data.get("type") != "req":
                    await self._send_response(
                        ws, data.get("id", ""), False, error="expected type=req"
                    )
                    continue

                try:
                    req = WireRequest(**data)
                except Exception as e:
                    await self._send_response(
                        ws,
                        data.get("id", ""),
                        False,
                        error=f"invalid request: {e}",
                    )
                    continue

                try:
                    await self._dispatch(ws, req)
                except Exception as e:  # noqa: BLE001
                    logger.exception("wire dispatch error for method %s", req.method)
                    await self._send_response(
                        ws, req.id, False, error=f"{type(e).__name__}: {e}"
                    )
        except websockets.ConnectionClosed:
            pass
        finally:
            # Tier-C: drop from global broadcast set first so any in-flight
            # broadcast scheduled before disconnect simply skips this ws.
            self._session_clients_all.discard(ws)
            # M3.1 — drop this ws from any session it was registered on
            # so future permission.request broadcasts don't try to send
            # over a dead socket. Walk the registry copy to mutate safely.
            for sid, conns in list(self._session_clients.items()):
                conns.discard(ws)
                if not conns:
                    self._session_clients.pop(sid, None)
            logger.info("wire: client %s disconnected", client_id)

    async def _dispatch(
        self, ws: websockets.WebSocketServerProtocol, req: WireRequest
    ) -> None:
        if req.method == METHOD_HELLO:
            # M3.3 — optional ``session_id`` + ``last_event_seq`` enable
            # wire-reconnect replay. The HelloResult carries gap_warning
            # + server_last_event_seq so the client can decide whether
            # the replayed window covers everything it missed.
            session_id = req.params.get("session_id")
            last_event_seq = req.params.get("last_event_seq")
            gap_warning = False
            server_last_seq: int | None = None
            replay_events: list[WireEvent] = []
            if session_id and last_event_seq is not None:
                try:
                    last_seq_int = int(last_event_seq)
                except (TypeError, ValueError):
                    last_seq_int = -1
                server_last_seq, gap_warning, replay_events = self._replay_after_hello(
                    ws, str(session_id), last_seq_int
                )
            elif session_id:
                server_last_seq = self._session_seq.get(str(session_id), 0)
            if session_id:
                # Register this connection for permission-request broadcasts.
                self._session_clients.setdefault(
                    str(session_id), set()
                ).add(ws)
            await self._send_response(
                ws,
                req.id,
                True,
                payload={
                    "server": "opencomputer",
                    "version": "0.0.1",
                    "methods": [
                        METHOD_HELLO,
                        METHOD_CHAT,
                        METHOD_SESSION_LIST,
                        METHOD_SEARCH,
                        METHOD_SKILLS_LIST,
                        METHOD_STEER_SUBMIT,
                        METHOD_SLASH_LIST,
                        METHOD_SLASH_DISPATCH,
                        METHOD_PERMISSION_RESPONSE,
                        METHOD_MEMORY_STATUS,
                        METHOD_EVOLUTION_STATUS,
                        METHOD_SESSION_RESUME,
                        METHOD_SESSION_DELETE,
                        METHOD_MODEL_OPTIONS,
                        METHOD_CONFIG_GET,
                        METHOD_MODEL_SET,
                        METHOD_CONFIG_SET,
                        METHOD_SESSION_RENAME,
                        METHOD_SESSION_USAGE,
                        METHOD_SUBAGENTS_LIST,
                        METHOD_SESSION_MOST_RECENT,
                        METHOD_SKILL_SHOW,
                        METHOD_SESSION_FORK,
                        METHOD_SESSION_INTERRUPT,
                        METHOD_TOOLS_LIST,
                        METHOD_CHECKPOINTS_LIST,
                        METHOD_CHECKPOINTS_DELETE,
                    ],
                    "events": [
                        EVENT_TURN_BEGIN,
                        EVENT_TURN_END,
                        EVENT_ASSISTANT_MESSAGE,
                        EVENT_ERROR,
                        EVENT_PERMISSION_REQUEST,
                        EVENT_MEMORY_WRITE,
                        EVENT_EVOLUTION_TUNING_CHANGED,
                        EVENT_STREAM_RETRY,
                    ],
                    "gap_warning": gap_warning,
                    "server_last_event_seq": server_last_seq,
                },
            )
            # Replay missed events AFTER the HelloResult so the client
            # always sees the response first and can branch on
            # gap_warning before consuming events.
            for ev in replay_events:
                await ws.send(ev.model_dump_json())
        elif req.method == METHOD_CHAT:
            await self._handle_chat(ws, req)
        elif req.method == METHOD_SESSION_LIST:
            # Note: get_or_load("default") is O(1) after first load (cached
            # dict hit). Per-call wire binding (RPC carries profile_id)
            # deferred to v1.1.
            # v1: always default profile; v1.1 will accept profile_id in params.
            limit = int(req.params.get("limit", 20))
            _loop = await self._router.get_or_load("default")
            rows = _loop.db.list_sessions(limit=limit)
            await self._send_response(ws, req.id, True, payload={"sessions": rows})
        elif req.method == METHOD_SEARCH:
            # v1: always default profile; v1.1 will accept profile_id in params.
            query = str(req.params.get("query", ""))
            limit = int(req.params.get("limit", 20))
            _loop = await self._router.get_or_load("default")
            hits = _loop.db.search(query, limit=limit)
            await self._send_response(ws, req.id, True, payload={"hits": hits})
        elif req.method == METHOD_SKILLS_LIST:
            # v1: always default profile; v1.1 will accept profile_id in params.
            _loop = await self._router.get_or_load("default")
            skills = _loop.memory.list_skills()
            payload = {
                "skills": [
                    {
                        "id": s.id,
                        "name": s.name,
                        "description": s.description,
                        "version": s.version,
                    }
                    for s in skills
                ]
            }
            await self._send_response(ws, req.id, True, payload=payload)
        elif req.method == METHOD_STEER_SUBMIT:
            # P-2 round 2a: route a /steer nudge into SteerRegistry. The
            # actual injection happens between turns inside AgentLoop;
            # here we just record + ack. ``had_pending`` tells the caller
            # whether their submission overrode an earlier pending nudge
            # so the UI can surface a "previous nudge discarded" hint.
            session_id = str(req.params.get("session_id", "")).strip()
            prompt = str(req.params.get("prompt", "")).strip()
            if not session_id:
                await self._send_response(
                    ws, req.id, False, error="steer.submit: session_id is required"
                )
                return
            if not prompt:
                await self._send_response(
                    ws, req.id, False, error="steer.submit: prompt must be non-empty"
                )
                return
            had_pending = _steer_registry.has_pending(session_id)
            _steer_registry.submit(session_id, prompt)
            await self._send_response(
                ws,
                req.id,
                True,
                payload={
                    "session_id": session_id,
                    "had_pending": had_pending,
                    "queued_chars": len(prompt),
                },
            )
        elif req.method == METHOD_SLASH_LIST:
            # 2026-05-07 (PR6): enumerate registered slash commands so
            # the dashboard ChatPage and the (future) Ink TUI share a
            # single source of truth for the slash palette.
            try:
                from opencomputer.agent.slash_commands import (
                    get_registered_commands,
                )

                cmds = get_registered_commands()
                payload = {
                    "commands": [
                        {
                            "name": getattr(c, "name", str(c)),
                            "description": getattr(c, "description", ""),
                            "aliases": list(getattr(c, "aliases", [])),
                        }
                        for c in cmds
                    ]
                }
                await self._send_response(ws, req.id, True, payload=payload)
            except Exception as exc:  # noqa: BLE001
                await self._send_response(
                    ws, req.id, False, error=f"slash.list: {exc}"
                )
        elif req.method == METHOD_PERMISSION_RESPONSE:
            # M3.1 (2026-05-09) — wire client posts the user's
            # allow_once / allow_always / deny verdict in response to
            # a permission.request event. Routes into ConsentGate.
            # resolve_pending on the AgentLoop's gate. First responder
            # wins; later responders see ``resolved=False`` since the
            # gate's pending registry only holds one entry per
            # (session_id, capability_id) pair.
            session_id = str(req.params.get("session_id", "")).strip()
            capability_id = str(req.params.get("capability_id", "")).strip()
            decision = str(req.params.get("decision", "")).strip()
            if not session_id or not capability_id:
                await self._send_response(
                    ws,
                    req.id,
                    False,
                    error="permission.response: session_id + capability_id required",
                )
                return
            if decision not in ("allow_once", "allow_always", "deny"):
                await self._send_response(
                    ws,
                    req.id,
                    False,
                    error=(
                        "permission.response: decision must be one of "
                        "allow_once, allow_always, deny"
                    ),
                )
                return
            allowed = decision != "deny"
            persist = decision == "allow_always"
            # v1: always default profile (matches the rest of the wire
            # surface). v1.1 will accept profile_id in RPC params.
            try:
                _loop = await self._router.get_or_load("default")
                gate = getattr(_loop, "_consent_gate", None)
                if gate is None:
                    await self._send_response(
                        ws,
                        req.id,
                        False,
                        error="permission.response: agent loop has no consent gate",
                    )
                    return
                resolved = gate.resolve_pending(
                    session_id=session_id,
                    capability_id=capability_id,
                    allowed=allowed,
                    persist=persist,
                )
            except Exception as exc:  # noqa: BLE001
                await self._send_response(
                    ws, req.id, False, error=f"permission.response: {exc}"
                )
                return
            await self._send_response(
                ws,
                req.id,
                True,
                payload={
                    "request_id": str(req.params.get("request_id", "")),
                    "resolved": bool(resolved),
                },
            )
        elif req.method == METHOD_SLASH_DISPATCH:
            # 2026-05-07 (PR6): invoke a slash command via OC's dispatcher.
            try:
                from opencomputer.agent.slash_commands import dispatch_slash

                name = str(req.params.get("name", "")).strip()
                args = str(req.params.get("args", ""))
                if not name:
                    await self._send_response(
                        ws, req.id, False, error="slash.dispatch: name required"
                    )
                    return
                # dispatch_slash expects a full message string starting with /
                msg = "/" + name + (" " + args if args else "")
                output = dispatch_slash(msg)
                await self._send_response(
                    ws, req.id, True, payload={"output": output, "side_effects": {}}
                )
            except Exception as exc:  # noqa: BLE001
                await self._send_response(
                    ws, req.id, False, error=f"slash.dispatch: {exc}"
                )
        elif req.method == METHOD_MEMORY_STATUS:
            # Tier-C+ of 2026-05-10 memory-observability design.
            # Returns current cap status for every declarative-memory file
            # (MEMORY.md + USER.md) so a freshly-connected client can seed
            # its memory panel without waiting for a write event.
            # v1: always default profile (matches the rest of the wire surface).
            try:
                _loop = await self._router.get_or_load("default")
                entries = self._collect_memory_status(_loop)
                await self._send_response(
                    ws, req.id, True, payload={"entries": entries}
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("memory.status: failed")
                await self._send_response(
                    ws, req.id, False, error=f"memory.status: {exc}"
                )
        elif req.method == METHOD_EVOLUTION_STATUS:
            # 2026-05-11 — self-evolution status snapshot. Initial-state
            # RPC companion to EVENT_EVOLUTION_TUNING_CHANGED so a
            # freshly-connecting client can render the tuning panel
            # without waiting for the next change event.
            try:
                payload = self._collect_evolution_status()
                await self._send_response(
                    ws, req.id, True, payload=payload
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("evolution.status: failed")
                await self._send_response(
                    ws, req.id, False, error=f"evolution.status: {exc}"
                )
        elif req.method == METHOD_SESSION_RESUME:
            # 2026-05-17 TUI-parity M1 — load a stored session's transcript
            # so a resume picker can render past turns. v1: always default
            # profile (matches the rest of the wire surface).
            session_id = str(req.params.get("session_id", "")).strip()
            if not session_id:
                await self._send_response(
                    ws, req.id, False, error="session.resume: session_id is required"
                )
                return
            try:
                _loop = await self._router.get_or_load("default")
                payload = self._collect_session_resume(_loop, session_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("session.resume: failed")
                await self._send_response(
                    ws, req.id, False, error=f"session.resume: {exc}"
                )
                return
            if payload is None:
                await self._send_response(
                    ws,
                    req.id,
                    False,
                    error=f"session.resume: no session {session_id}",
                )
                return
            await self._send_response(ws, req.id, True, payload=payload)
        elif req.method == METHOD_SESSION_DELETE:
            # 2026-05-17 TUI-parity M1 — delete a stored session + its
            # cascaded rows. Idempotent: deleting an unknown id is a
            # success with found=False, not an error.
            session_id = str(req.params.get("session_id", "")).strip()
            if not session_id:
                await self._send_response(
                    ws, req.id, False, error="session.delete: session_id is required"
                )
                return
            try:
                _loop = await self._router.get_or_load("default")
                found = bool(_loop.db.delete_session(session_id))
            except Exception as exc:  # noqa: BLE001
                logger.exception("session.delete: failed")
                await self._send_response(
                    ws, req.id, False, error=f"session.delete: {exc}"
                )
                return
            await self._send_response(
                ws, req.id, True, payload={"deleted": session_id, "found": found}
            )
        elif req.method == METHOD_MODEL_OPTIONS:
            # 2026-05-17 TUI-parity M1 batch 2 — registry snapshot for a
            # model-picker overlay. No params; reads the process-global
            # model registry + the active profile's bound default.
            try:
                payload = self._collect_model_options()
            except Exception as exc:  # noqa: BLE001
                logger.exception("model.options: failed")
                await self._send_response(
                    ws, req.id, False, error=f"model.options: {exc}"
                )
                return
            await self._send_response(ws, req.id, True, payload=payload)
        elif req.method == METHOD_CONFIG_GET:
            # 2026-05-17 TUI-parity M1 batch 2 — fetch one config value by
            # dotted key for a settings panel. Unknown key → found=False
            # (a success, not an error).
            key = str(req.params.get("key", "")).strip()
            if not key:
                await self._send_response(
                    ws, req.id, False, error="config.get: key is required"
                )
                return
            try:
                payload = self._collect_config_value(key)
            except Exception as exc:  # noqa: BLE001
                logger.exception("config.get: failed")
                await self._send_response(
                    ws, req.id, False, error=f"config.get: {exc}"
                )
                return
            await self._send_response(ws, req.id, True, payload=payload)
        elif req.method == METHOD_MODEL_SET:
            # 2026-05-17 TUI-parity M1 batch 3 — persist a new default
            # provider+model. Persist-only: the running session is
            # unaffected until restart (matches the dashboard route).
            provider = str(req.params.get("provider", "")).strip()
            model = str(req.params.get("model", "")).strip()
            if not provider or not model:
                await self._send_response(
                    ws,
                    req.id,
                    False,
                    error="model.set: provider and model are both required",
                )
                return
            result = self._apply_model_set(provider, model)
            if isinstance(result, str):
                await self._send_response(ws, req.id, False, error=result)
                return
            await self._send_response(ws, req.id, True, payload=result)
        elif req.method == METHOD_CONFIG_SET:
            # 2026-05-17 TUI-parity M1 batch 3 — persist one config value
            # by dotted key. Validated by round-trip; rolled back on failure.
            key = str(req.params.get("key", "")).strip()
            if not key:
                await self._send_response(
                    ws, req.id, False, error="config.set: key is required"
                )
                return
            result = self._apply_config_set(key, req.params.get("value"))
            if isinstance(result, str):
                await self._send_response(ws, req.id, False, error=result)
                return
            await self._send_response(ws, req.id, True, payload=result)
        elif req.method == METHOD_SESSION_RENAME:
            # 2026-05-17 TUI-parity M1 batch 4 — set a session's title.
            # SessionDB.set_session_title upserts, so renaming a not-yet-
            # persisted session id still succeeds (idempotent).
            session_id = str(req.params.get("session_id", "")).strip()
            title = str(req.params.get("title", "")).strip()
            if not session_id:
                await self._send_response(
                    ws, req.id, False, error="session.rename: session_id is required"
                )
                return
            if not title:
                await self._send_response(
                    ws, req.id, False, error="session.rename: title must be non-empty"
                )
                return
            try:
                _loop = await self._router.get_or_load("default")
                _loop.db.set_session_title(session_id, title)
            except Exception as exc:  # noqa: BLE001
                logger.exception("session.rename: failed")
                await self._send_response(
                    ws, req.id, False, error=f"session.rename: {exc}"
                )
                return
            await self._send_response(
                ws,
                req.id,
                True,
                payload={"session_id": session_id, "title": title, "ok": True},
            )
        elif req.method == METHOD_SESSION_USAGE:
            # 2026-05-17 TUI-parity M1 batch 4 — per-session token/cost
            # totals. Unknown id → found=False (a success, not an error).
            session_id = str(req.params.get("session_id", "")).strip()
            if not session_id:
                await self._send_response(
                    ws, req.id, False, error="session.usage: session_id is required"
                )
                return
            try:
                _loop = await self._router.get_or_load("default")
                payload = self._collect_session_usage(_loop, session_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("session.usage: failed")
                await self._send_response(
                    ws, req.id, False, error=f"session.usage: {exc}"
                )
                return
            if payload is None:
                await self._send_response(
                    ws,
                    req.id,
                    True,
                    payload={"session_id": session_id, "found": False},
                )
                return
            await self._send_response(ws, req.id, True, payload=payload)
        elif req.method == METHOD_SUBAGENTS_LIST:
            # 2026-05-17 TUI-parity M1 batch 5 — spawned-subagent history
            # for an agents overlay. Old DBs without a subagents table
            # degrade to an empty list (helper swallows the error).
            limit = int(req.params.get("limit", 50))
            running_only = bool(req.params.get("running_only", False))
            try:
                _loop = await self._router.get_or_load("default")
                subs = self._collect_subagents(
                    _loop, limit=limit, running_only=running_only
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("subagents.list: failed")
                await self._send_response(
                    ws, req.id, False, error=f"subagents.list: {exc}"
                )
                return
            await self._send_response(
                ws, req.id, True, payload={"subagents": subs}
            )
        elif req.method == METHOD_SESSION_MOST_RECENT:
            # 2026-05-17 TUI-parity M1 batch 5 — the newest session, for
            # the TUI's "resume last" affordance. Empty profile → found=False.
            try:
                _loop = await self._router.get_or_load("default")
                payload = self._collect_most_recent(_loop)
            except Exception as exc:  # noqa: BLE001
                logger.exception("session.most_recent: failed")
                await self._send_response(
                    ws, req.id, False, error=f"session.most_recent: {exc}"
                )
                return
            await self._send_response(ws, req.id, True, payload=payload)
        elif req.method == METHOD_SKILL_SHOW:
            # 2026-05-17 TUI-parity M1 batch 6 — a skill's SKILL.md body
            # for the skills-hub preview. Unknown id → found=False.
            skill_id = str(req.params.get("skill_id", "")).strip()
            if not skill_id:
                await self._send_response(
                    ws, req.id, False, error="skill.show: skill_id is required"
                )
                return
            try:
                _loop = await self._router.get_or_load("default")
                payload = self._collect_skill_body(_loop, skill_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("skill.show: failed")
                await self._send_response(
                    ws, req.id, False, error=f"skill.show: {exc}"
                )
                return
            await self._send_response(ws, req.id, True, payload=payload)
        elif req.method == METHOD_SESSION_FORK:
            # 2026-05-17 TUI-parity M1 batch 6 — clone a session's history
            # into a new session id (fork-tree / branch affordance).
            session_id = str(req.params.get("session_id", "")).strip()
            if not session_id:
                await self._send_response(
                    ws, req.id, False, error="session.fork: session_id is required"
                )
                return
            title = str(req.params.get("title", "")).strip()
            record_parent = bool(req.params.get("record_parent", False))
            try:
                from opencomputer.agent.session_fork import (
                    SourceSessionNotFoundError,
                    fork_session,
                )

                _loop = await self._router.get_or_load("default")
                result = fork_session(
                    _loop.db,
                    session_id,
                    title=title or None,
                    record_parent=record_parent,
                )
            except SourceSessionNotFoundError:
                await self._send_response(
                    ws,
                    req.id,
                    False,
                    error=f"session.fork: source session {session_id} not found",
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("session.fork: failed")
                await self._send_response(
                    ws, req.id, False, error=f"session.fork: {exc}"
                )
                return
            await self._send_response(
                ws,
                req.id,
                True,
                payload={
                    "source_session_id": session_id,
                    "new_session_id": result.new_session_id,
                    "messages_copied": result.messages_copied,
                    "ok": True,
                },
            )
        elif req.method == METHOD_SESSION_INTERRUPT:
            # 2026-05-17 TUI-parity M1 batch 7 — signal a mid-run turn to
            # cancel. Sets the steer registry's per-session cancel Event,
            # which the agent loop watches between/within turns. Setting
            # it for an idle session is harmless — the loop clears a stale
            # event on its next dispatch.
            session_id = str(req.params.get("session_id", "")).strip()
            if not session_id:
                await self._send_response(
                    ws, req.id, False, error="session.interrupt: session_id is required"
                )
                return
            try:
                _steer_registry.cancel_event(session_id).set()
            except Exception as exc:  # noqa: BLE001
                logger.exception("session.interrupt: failed")
                await self._send_response(
                    ws, req.id, False, error=f"session.interrupt: {exc}"
                )
                return
            await self._send_response(
                ws, req.id, True, payload={"session_id": session_id, "ok": True}
            )
        elif req.method == METHOD_TOOLS_LIST:
            # 2026-05-17 TUI-parity M1 batch 7 — registered-tool inventory
            # for a capability-inspector overlay.
            try:
                tools = self._collect_tools()
            except Exception as exc:  # noqa: BLE001
                logger.exception("tools.list: failed")
                await self._send_response(
                    ws, req.id, False, error=f"tools.list: {exc}"
                )
                return
            await self._send_response(
                ws, req.id, True, payload={"tools": tools}
            )
        elif req.method == METHOD_CHECKPOINTS_LIST:
            # 2026-05-17 TUI-parity M1 batch 8 — a session's prompt
            # checkpoints for a rollback overlay.
            session_id = str(req.params.get("session_id", "")).strip()
            if not session_id:
                await self._send_response(
                    ws, req.id, False, error="checkpoints.list: session_id is required"
                )
                return
            limit = int(req.params.get("limit", 50))
            try:
                _loop = await self._router.get_or_load("default")
                checkpoints = self._collect_checkpoints(_loop, session_id, limit)
            except Exception as exc:  # noqa: BLE001
                logger.exception("checkpoints.list: failed")
                await self._send_response(
                    ws, req.id, False, error=f"checkpoints.list: {exc}"
                )
                return
            await self._send_response(
                ws, req.id, True, payload={"checkpoints": checkpoints}
            )
        elif req.method == METHOD_CHECKPOINTS_DELETE:
            # 2026-05-17 TUI-parity M1 batch 8 — prune one checkpoint.
            # Idempotent: unknown id → found=False (a success).
            checkpoint_id = str(req.params.get("checkpoint_id", "")).strip()
            if not checkpoint_id:
                await self._send_response(
                    ws,
                    req.id,
                    False,
                    error="checkpoints.delete: checkpoint_id is required",
                )
                return
            try:
                _loop = await self._router.get_or_load("default")
                found = bool(_loop.db.delete_prompt_checkpoint(checkpoint_id))
            except Exception as exc:  # noqa: BLE001
                logger.exception("checkpoints.delete: failed")
                await self._send_response(
                    ws, req.id, False, error=f"checkpoints.delete: {exc}"
                )
                return
            await self._send_response(
                ws,
                req.id,
                True,
                payload={"checkpoint_id": checkpoint_id, "found": found},
            )
        else:
            await self._send_response(
                ws, req.id, False, error=f"unknown method: {req.method}"
            )

    async def _handle_chat(
        self, ws: websockets.WebSocketServerProtocol, req: WireRequest
    ) -> None:
        user_message = str(req.params.get("message", "")).strip()
        session_id = req.params.get("session_id") or None
        if not user_message:
            await self._send_response(
                ws, req.id, False, error="empty message"
            )
            return

        # M3.1 follow-up (PR #523 amend): also register the wire ws in
        # _session_clients on chat dispatch — not just in hello — so a
        # client that connects, immediately calls chat, and hits a
        # Tier-2 capability mid-turn is reachable for permission
        # broadcast. Idempotent: ws already registered via hello stays
        # in the set. Cleanup happens in _handle_client.finally.
        if session_id:
            self._session_clients.setdefault(str(session_id), set()).add(ws)

        # Announce turn begin
        await self._send_event(
            ws, EVENT_TURN_BEGIN, {"request_id": req.id}
        )

        # Stream text deltas to the client as assistant messages
        async def _on_chunk(text: str) -> None:
            await self._send_event(
                ws,
                EVENT_ASSISTANT_MESSAGE,
                {"delta": text, "request_id": req.id},
            )

        # v1: always default profile; v1.1 will accept profile_id in RPC params.
        profile_id = "default"
        loop = await self._router.get_or_load(profile_id)
        profile_home = self._router._profile_home_resolver(profile_id)

        from plugin_sdk.profile_context import set_profile

        # 2026-05-11 — surface pre-first-byte retry status to WS clients.
        # Mirrors the CLI renderer's yellow retry panel: TUI / IDE /
        # dashboard clients see a real-time "retry 2/4 in 1.3s" banner
        # during the recovery window instead of a frozen spinner. The
        # AgentLoop wrapper retries regardless of whether a callback is
        # provided; this just makes the recovery visible.
        async def _emit_retry(status):
            try:
                await self._send_event(
                    ws,
                    EVENT_STREAM_RETRY,
                    {
                        "request_id": req.id,
                        "attempt": status.attempt,
                        "next_attempt": status.next_attempt,
                        "max_attempts": status.max_attempts,
                        "delay_seconds": status.delay_seconds,
                        "error_kind": status.error_kind,
                        "error_message": status.error_message,
                        "exhausted": status.exhausted,
                    },
                )
            except Exception:  # noqa: BLE001 — UI bridge mustn't wedge retry
                pass

        def _on_retry_status(status):
            # Sync callback (per the stream_retry contract) hops to the
            # event loop. Bare ``create_task`` is fine — we're already
            # inside the WS loop here.
            try:
                asyncio.create_task(_emit_retry(status))
            except Exception:  # noqa: BLE001 — fail-open: retry continues
                pass

        try:
            with set_profile(profile_home):
                result = await loop.run_conversation(
                    user_message=user_message,
                    session_id=session_id,
                    stream_callback=lambda t: asyncio.create_task(_on_chunk(t)),
                    retry_callback=_on_retry_status,
                )
        except Exception as e:  # noqa: BLE001
            await self._send_event(
                ws,
                EVENT_ERROR,
                {"request_id": req.id, "error": f"{type(e).__name__}: {e}"},
            )
            await self._send_response(
                ws, req.id, False, error=f"{type(e).__name__}: {e}"
            )
            return

        await self._send_event(
            ws,
            EVENT_TURN_END,
            {
                "request_id": req.id,
                "iterations": result.iterations,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "session_id": result.session_id,
            },
        )
        await self._send_response(
            ws,
            req.id,
            True,
            payload={
                "text": result.final_message.content,
                "session_id": result.session_id,
                "iterations": result.iterations,
            },
        )

    async def _send_response(
        self,
        ws: websockets.WebSocketServerProtocol,
        req_id: str,
        ok: bool,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        res = WireResponse(id=req_id, ok=ok, payload=payload, error=error)
        await ws.send(res.model_dump_json())

    async def _send_event(
        self,
        ws: websockets.WebSocketServerProtocol,
        event_name: str,
        payload: dict[str, Any],
    ) -> None:
        """Send one event to a single WS, stamping a per-session seq.

        M3.3 — uses ``payload.get("session_id")`` (or
        ``payload.get("request_id")`` for events that don't carry one
        natively) to key the ring buffer. Events without an
        identifiable session are sent un-replayable (no seq stamp).
        """
        session_id = payload.get("session_id") or payload.get("request_id") or ""
        seq: int | None = None
        if session_id:
            seq = self._session_seq.get(session_id, 0) + 1
            self._session_seq[session_id] = seq
        ev = WireEvent(event=event_name, payload=payload, seq=seq)
        if session_id:
            self._session_rings.setdefault(
                session_id, deque(maxlen=RING_BUFFER_MAX)
            ).append(ev)
        await ws.send(ev.model_dump_json())

    # ─── Tier-C bus→wire bridge (memory.write) ─────────────────────

    def _on_memory_write_bus_event(self, event: Any) -> None:
        """Sync bus handler — schedule a broadcast onto the wire-server loop.

        Runs on the publisher's thread (typically the agent loop's). Builds
        the typed payload synchronously, then hops into the wire-server's
        asyncio loop via ``run_coroutine_threadsafe``. Per-client send
        errors are swallowed inside ``_broadcast_global`` so a stale ws
        never blocks a memory write from reaching other clients.

        Failure-isolated: any exception is logged but never propagates to
        the publisher — a wedged TUI panel must not break a memory write.
        """
        loop = self._loop_ref
        if loop is None or loop.is_closed():
            return
        try:
            # cap_limit is wire-only — derived from the target filename so
            # a TUI panel can render percentage without an extra RPC.
            cap_limit = 2000 if event.target == "USER.md" else 4000
            payload = {
                "action": event.action,
                "target": event.target,
                "content_size": event.content_size,
                "cap_limit": cap_limit,
                "compaction_delta": event.compaction_delta,
                "dropped_paragraphs": event.dropped_paragraphs,
            }

            if os.environ.get("OPENCOMPUTER_WIRE_DEBUG_EVENTS") == "1":
                logger.debug(
                    "wire bridge: broadcasting memory.write target=%s "
                    "drop=%d delta=%d clients=%d",
                    event.target,
                    event.dropped_paragraphs,
                    event.compaction_delta,
                    len(self._session_clients_all),
                )

            asyncio.run_coroutine_threadsafe(
                self._broadcast_global(EVENT_MEMORY_WRITE, payload), loop
            )
        except RuntimeError:
            # Loop closed in the gap between is_closed() check and schedule.
            logger.debug("wire bridge: loop closed before memory.write broadcast")
        except Exception:  # noqa: BLE001 — must not break the publisher
            logger.exception("wire bridge: failed to forward memory.write event")

    # ─── 2026-05-11 bus→wire bridge (evolution.tuning_changed) ─────

    def _on_evolution_tuning_bus_event(self, event: Any) -> None:
        """Sync bus handler — schedule an evolution-tuning broadcast.

        Same shape as :meth:`_on_memory_write_bus_event`: builds a
        typed payload on the publisher thread, hops onto the
        wire-server loop via ``run_coroutine_threadsafe`` for
        per-client fanout. Per-client send errors are swallowed by
        ``_broadcast_global`` so a stale ws never blocks a tuning
        update from reaching others.

        Failure-isolated: any exception is logged but never propagates
        to the publisher — a wedged dashboard must not break the
        orchestrator's tune path.
        """
        loop = self._loop_ref
        if loop is None or loop.is_closed():
            return
        try:
            payload = {
                "confidence_threshold": int(
                    getattr(event, "confidence_threshold", 70) or 0
                ),
                "dreaming_v2_score_threshold": float(
                    getattr(event, "dreaming_v2_score_threshold", 0.65) or 0.0
                ),
                "dreaming_v2_min_recall": int(
                    getattr(event, "dreaming_v2_min_recall", 2) or 0
                ),
                "decisions_observed": int(
                    getattr(event, "decisions_observed", 0) or 0
                ),
                "changed": bool(getattr(event, "changed", False)),
            }
            if os.environ.get("OPENCOMPUTER_WIRE_DEBUG_EVENTS") == "1":
                logger.debug(
                    "wire bridge: broadcasting evolution.tuning_changed "
                    "confidence=%d changed=%s clients=%d",
                    payload["confidence_threshold"],
                    payload["changed"],
                    len(self._session_clients_all),
                )
            asyncio.run_coroutine_threadsafe(
                self._broadcast_global(
                    EVENT_EVOLUTION_TUNING_CHANGED, payload
                ),
                loop,
            )
        except RuntimeError:
            logger.debug(
                "wire bridge: loop closed before evolution.tuning_changed broadcast"
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "wire bridge: failed to forward evolution.tuning_changed event"
            )

    # ─── 2026-05-13 bus→wire bridge (profile.swap) ─────────────────

    def _on_profile_swap_bus_event(self, event: Any) -> None:
        """Sync bus handler — schedule a profile-swap broadcast.

        Same shape as :meth:`_on_memory_write_bus_event`: builds a
        typed payload on the publisher thread, hops onto the
        wire-server loop via ``run_coroutine_threadsafe`` for per-client
        fanout. Failure-isolated; a wedged WS client must not block the
        orchestrator's swap path.

        Carries enough context for a workspace SPA / TUI / IDE to
        render ``"↪ from_profile → to_profile (handoff)"`` and refresh
        profile-bound state (memory panel, plugin list, MCP catalog).
        """
        loop = self._loop_ref
        if loop is None or loop.is_closed():
            return
        try:
            payload = {
                "from_profile": getattr(event, "from_profile", "") or "",
                "to_profile": getattr(event, "to_profile", "") or "",
                "trigger": getattr(event, "trigger", "") or "auto",
                "classifier_confidence": float(
                    getattr(event, "classifier_confidence", 0.0) or 0.0,
                ),
                "classifier_reason": (
                    getattr(event, "classifier_reason", "") or ""
                )[:200],
                "has_handoff": bool(
                    getattr(event, "has_handoff", False),
                ),
            }
            if os.environ.get("OPENCOMPUTER_WIRE_DEBUG_EVENTS") == "1":
                logger.debug(
                    "wire bridge: broadcasting profile.swap %s->%s "
                    "trigger=%s handoff=%s clients=%d",
                    payload["from_profile"],
                    payload["to_profile"],
                    payload["trigger"],
                    payload["has_handoff"],
                    len(self._session_clients_all),
                )
            asyncio.run_coroutine_threadsafe(
                self._broadcast_global(EVENT_PROFILE_SWAP, payload),
                loop,
            )
        except RuntimeError:
            logger.debug(
                "wire bridge: loop closed before profile.swap broadcast"
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "wire bridge: failed to forward profile.swap event"
            )

    @staticmethod
    def _collect_evolution_status() -> dict[str, Any]:
        """Build the :data:`METHOD_EVOLUTION_STATUS` payload.

        Reads the persisted ``evolution_tuning.json`` for the active
        profile and returns the tuning state. Per-component
        failure-isolated — missing dependency / missing file → returns
        defaults, never raises out.
        """
        try:
            from opencomputer.agent.config import _home
            from opencomputer.agent.evolution_orchestrator import (
                DEFAULT_TUNING,
                load_tuning,
            )

            tuning = load_tuning(_home())
            return {
                "confidence_threshold": tuning.confidence_threshold,
                "dreaming_v2_score_threshold": tuning.dreaming_v2_score_threshold,
                "dreaming_v2_min_recall": tuning.dreaming_v2_min_recall,
                "decisions_observed": tuning.decisions_observed,
                "last_recompute_ts": tuning.last_recompute_ts,
                "schema_version": tuning.schema_version,
                "defaults": {
                    "confidence_threshold": DEFAULT_TUNING.confidence_threshold,
                    "dreaming_v2_score_threshold": DEFAULT_TUNING.dreaming_v2_score_threshold,
                    "dreaming_v2_min_recall": DEFAULT_TUNING.dreaming_v2_min_recall,
                },
            }
        except Exception:  # noqa: BLE001 — collector never raises
            logger.warning(
                "evolution.status: collector failed; returning defaults",
                exc_info=True,
            )
            return {
                "confidence_threshold": 70,
                "dreaming_v2_score_threshold": 0.65,
                "dreaming_v2_min_recall": 2,
                "decisions_observed": 0,
                "last_recompute_ts": 0.0,
                "schema_version": 0,
                "defaults": {
                    "confidence_threshold": 70,
                    "dreaming_v2_score_threshold": 0.65,
                    "dreaming_v2_min_recall": 2,
                },
            }

    async def _broadcast_global(
        self, event_name: str, payload: dict[str, Any]
    ) -> None:
        """Send an event to every WS in ``_session_clients_all``.

        Used for events without session-keyed routing (memory writes,
        future global telemetry). Per-client send failures are swallowed
        so one stale ws never blocks delivery to others.

        Memory-write events have no session_id (per-process state), so
        they are NOT recorded in ``_session_rings``. Reconnecting clients
        do NOT see replay of memory.write events — they call
        :data:`METHOD_MEMORY_STATUS` on connect to seed initial state
        from the server's view of MEMORY.md / USER.md.
        """
        clients = list(self._session_clients_all)
        if not clients:
            return
        ev = WireEvent(event=event_name, payload=payload)
        msg = ev.model_dump_json()
        for client_ws in clients:
            try:
                await client_ws.send(msg)
            except Exception:  # noqa: BLE001 — never break broadcast on stale client
                continue

    @staticmethod
    def _collect_memory_status(loop: Any) -> list[dict[str, Any]]:
        """Build the ``METHOD_MEMORY_STATUS`` payload for one AgentLoop.

        Reads MEMORY.md + USER.md from disk (single ``stat()`` + ``read_text``
        per file) and computes :class:`opencomputer.agent.memory_cap.CapStatus`
        for each. Returns the dict-of-dicts shape the wire schema expects.

        Failure modes:

        * ``loop.memory`` missing (e.g. minimal test harness with stubbed
          loop) → returns empty list. The client renders nothing rather than
          erroring.
        * One file missing on disk → that entry reports ``content_size=0,
          paragraph_count=0, pct=0.0``. The other file still reported.
        * Both files unreadable (permissions) → per-file errors logged at
          WARN; that entry is omitted from the result. Other files still
          reported. Empty list is a valid response.

        Returned entries are sorted by ``target`` for stable client-side
        rendering — MEMORY.md before USER.md alphabetically.
        """
        from opencomputer.agent.memory_cap import cap_status

        manager = getattr(loop, "memory", None)
        if manager is None:
            logger.debug("memory.status: loop has no memory manager — empty result")
            return []

        # Each entry: (target_filename, file_path, cap_limit). The pairs
        # come from MemoryManager's canonical attributes — never hardcoded
        # in this method so a future split (PROJECTS.md etc.) needs zero
        # changes here when MemoryManager grows new fields.
        targets = [
            (
                "MEMORY.md",
                getattr(manager, "declarative_path", None),
                getattr(manager, "memory_char_limit", 4000),
            ),
            (
                "USER.md",
                getattr(manager, "user_path", None),
                getattr(manager, "user_char_limit", 2000),
            ),
        ]
        entries: list[dict[str, Any]] = []
        for target, path, limit in targets:
            if path is None:
                logger.debug(
                    "memory.status: %s path missing from MemoryManager — skipping",
                    target,
                )
                continue
            try:
                text = path.read_text(encoding="utf-8") if path.exists() else ""
            except OSError as exc:
                logger.warning(
                    "memory.status: failed to read %s (%s): %s — omitting from result",
                    target,
                    path,
                    exc,
                )
                continue
            status = cap_status(text, limit=limit, file_name=target)
            entries.append(
                {
                    "target": status.file_name,
                    "content_size": status.bytes_used,
                    "cap_limit": status.bytes_limit,
                    "pct": status.pct,
                    "paragraph_count": status.paragraph_count,
                }
            )
        entries.sort(key=lambda e: e["target"])
        return entries

    @staticmethod
    def _collect_session_resume(
        loop: Any, session_id: str
    ) -> dict[str, Any] | None:
        """Build the ``METHOD_SESSION_RESUME`` payload for one AgentLoop.

        Reads the ``sessions`` row + every stored message for ``session_id``
        via :class:`opencomputer.agent.state.SessionDB`, flattening each
        :class:`plugin_sdk.core.Message` into the wire-safe
        :class:`~opencomputer.gateway.protocol_v2.TranscriptMessage` shape
        (role + text + optional tool name — reasoning / tool-call JSON is
        dropped; a resume picker renders text, not raw tool payloads).

        Failure modes (helper never raises — mirrors ``_collect_memory_status``):

        * ``loop.db`` missing (minimal test harness) → returns ``None``.
        * No session row with that id → returns ``None``. The dispatch
          case turns ``None`` into an ``ok=False`` "no session" response.
        * ``get_messages`` raising → logged at WARN, transcript returns
          empty but the session ``info`` is still delivered.
        """
        db = getattr(loop, "db", None)
        if db is None:
            logger.debug("session.resume: loop has no db — None")
            return None

        info = db.get_session(session_id)
        if info is None:
            return None

        messages: list[dict[str, Any]] = []
        try:
            for msg in db.get_messages(session_id):
                role = getattr(msg, "role", "")
                # Role may be a str enum (plugin_sdk.core.Role) — normalise.
                role_str = str(getattr(role, "value", role) or "")
                messages.append(
                    {
                        "role": role_str,
                        "text": str(getattr(msg, "content", "") or ""),
                        "name": getattr(msg, "name", None),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session.resume: failed to load messages for %s: %s — "
                "returning metadata with empty transcript",
                session_id,
                exc,
            )
            messages = []

        return {
            "session_id": session_id,
            "info": dict(info),
            "messages": messages,
            "message_count": len(messages),
        }

    @staticmethod
    def _collect_session_usage(
        loop: Any, session_id: str
    ) -> dict[str, Any] | None:
        """Build the ``METHOD_SESSION_USAGE`` payload for one AgentLoop.

        Wraps :meth:`opencomputer.agent.state.SessionDB.session_usage_summary`
        — per-session token / cache / compaction / cost totals.

        Returns ``None`` when ``loop.db`` is missing or the session id is
        unknown / empty (the dispatch case turns ``None`` into a
        ``found=False`` success response). Never raises.
        """
        db = getattr(loop, "db", None)
        if db is None:
            logger.debug("session.usage: loop has no db — None")
            return None

        row = db.session_usage_summary(session_id)
        if row is None:
            return None

        return {
            "session_id": session_id,
            "found": True,
            "model": getattr(row, "model", None),
            "input_tokens": getattr(row, "input_tokens", 0),
            "output_tokens": getattr(row, "output_tokens", 0),
            "cache_read_tokens": getattr(row, "cache_read_tokens", 0),
            "cache_write_tokens": getattr(row, "cache_write_tokens", 0),
            "compactions_count": getattr(row, "compactions_count", 0),
            "cost_usd": getattr(row, "cost_usd", None),
            "started_at": getattr(row, "started_at", None),
            "ended_at": getattr(row, "ended_at", None),
        }

    @staticmethod
    def _collect_subagents(
        loop: Any, *, limit: int, running_only: bool
    ) -> list[dict[str, Any]]:
        """Build the ``METHOD_SUBAGENTS_LIST`` payload for one AgentLoop.

        Opens a :class:`opencomputer.agent.subagent_store.SubagentStore`
        over the same DB file as the loop's SessionDB (the ``subagents``
        table lives in ``sessions.db``) and flattens each record into a
        wire-safe dict — ``datetime`` fields become ISO-8601 strings.

        Failure modes (helper never raises):

        * ``loop.db`` missing → ``[]``.
        * DB predates the ``subagents`` table (``SubagentStoreUnavailable``)
          → ``[]`` logged at debug. The overlay renders empty.
        """
        db = getattr(loop, "db", None)
        if db is None:
            logger.debug("subagents.list: loop has no db — empty")
            return []

        try:
            # Single broad except: SubagentStoreUnavailable (DB predating
            # the subagents table) is a RuntimeError subclass and is
            # caught here too. Catching it by name would risk an unbound
            # reference if the import itself failed — so catch broadly.
            from opencomputer.agent.subagent_store import SubagentStore

            store = SubagentStore(db.db_path, allow_create=False)
            # history() excludes running records by design (they live in
            # list_running()). For an "all" view the overlay needs both —
            # running first, then the most-recent terminal history. The
            # two sets are disjoint (state is exclusive) so no dedup.
            if running_only:
                records = list(store.list_running())
            else:
                records = list(store.list_running()) + list(
                    store.history(limit=max(1, limit))
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "subagents.list: store unavailable / read failed (%s) — empty",
                exc,
            )
            return []

        out: list[dict[str, Any]] = []
        for r in records:
            started = getattr(r, "started_at", None)
            ended = getattr(r, "ended_at", None)
            out.append(
                {
                    "agent_id": getattr(r, "agent_id", ""),
                    "goal": getattr(r, "goal", ""),
                    "state": getattr(r, "state", ""),
                    "display_state": getattr(r, "display_state", "")
                    or getattr(r, "state", ""),
                    "role": getattr(r, "role", ""),
                    "depth": getattr(r, "depth", 0),
                    "started_at": started.isoformat() if started else "",
                    "parent_session_id": getattr(r, "parent_session_id", None),
                    "child_session_id": getattr(r, "child_session_id", None),
                    "ended_at": ended.isoformat() if ended else None,
                    "error": getattr(r, "error", None),
                }
            )
        return out

    @staticmethod
    def _collect_skill_body(loop: Any, skill_id: str) -> dict[str, Any]:
        """Build the ``METHOD_SKILL_SHOW`` payload — one skill's body text.

        Cross-checks ``list_skills()`` for an authoritative existence test
        (``load_skill_body`` alone can't tell "missing" from "empty body"),
        then loads the SKILL.md content minus frontmatter.

        Returns ``{skill_id, body, found}``; ``found=False`` (empty body)
        when ``loop.memory`` is missing or no skill has that id. Never raises.
        """
        memory = getattr(loop, "memory", None)
        if memory is None:
            logger.debug("skill.show: loop has no memory manager — not found")
            return {"skill_id": skill_id, "body": "", "found": False}

        try:
            known = {
                getattr(s, "id", None) for s in memory.list_skills()
            }
            if skill_id not in known:
                return {"skill_id": skill_id, "body": "", "found": False}
            body = memory.load_skill_body(skill_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill.show: failed for %r: %s", skill_id, exc)
            return {"skill_id": skill_id, "body": "", "found": False}

        return {"skill_id": skill_id, "body": body, "found": True}

    @staticmethod
    def _collect_most_recent(loop: Any) -> dict[str, Any]:
        """Build the ``METHOD_SESSION_MOST_RECENT`` payload.

        Returns the newest session row's id/title/timestamp, or
        ``{"found": False}`` when ``loop.db`` is missing or the profile
        has no sessions. Never raises.
        """
        db = getattr(loop, "db", None)
        if db is None:
            return {"found": False}
        try:
            rows = db.list_sessions(limit=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("session.most_recent: list failed: %s", exc)
            return {"found": False}
        if not rows:
            return {"found": False}
        row = rows[0]
        return {
            "found": True,
            "session_id": row.get("id"),
            "title": row.get("title"),
            "started_at": row.get("started_at"),
            "source": row.get("source"),
        }

    @staticmethod
    def _collect_checkpoints(
        loop: Any, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """Build the ``METHOD_CHECKPOINTS_LIST`` payload for one AgentLoop.

        Wraps :meth:`opencomputer.agent.state.SessionDB.list_prompt_checkpoints`
        and flattens each :class:`~opencomputer.agent.state.PromptCheckpoint`
        — the snapshotted ``messages`` list becomes a ``message_count`` so
        a rollback overlay renders without shipping every transcript.

        Never raises: missing ``loop.db`` or unknown session → ``[]``.
        """
        db = getattr(loop, "db", None)
        if db is None:
            logger.debug("checkpoints.list: loop has no db — empty")
            return []
        try:
            rows = db.list_prompt_checkpoints(session_id, limit=max(1, limit))
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpoints.list: read failed: %s — empty", exc)
            return []
        return [
            {
                "id": getattr(cp, "id", ""),
                "session_id": getattr(cp, "session_id", session_id),
                "prompt_index": getattr(cp, "prompt_index", 0),
                "label": getattr(cp, "label", ""),
                "created_at": getattr(cp, "created_at", 0.0),
                "message_count": len(getattr(cp, "messages", None) or []),
            }
            for cp in rows
        ]

    @staticmethod
    def _collect_tools() -> list[dict[str, str]]:
        """Build the ``METHOD_TOOLS_LIST`` payload — every registered
        tool's ``{name, description}``.

        Reads the process-global tool registry via
        :meth:`opencomputer.tools.registry.ToolRegistry.tool_summaries`
        (descriptions truncated by the registry). Never raises — a
        registry failure degrades to ``[]``.
        """
        try:
            from opencomputer.tools.registry import registry

            return list(registry.tool_summaries())
        except Exception as exc:  # noqa: BLE001
            logger.warning("tools.list: registry unavailable (%s) — empty", exc)
            return []

    @staticmethod
    def _collect_model_options() -> dict[str, Any]:
        """Build the ``METHOD_MODEL_OPTIONS`` payload — registry snapshot.

        Enumerates every provider→model pairing from the process-global
        model registry (via ``cli_model_picker._grouped_models``) and marks
        the provider whose model is the currently-bound default.

        Failure modes (helper never raises — mirrors ``_collect_memory_status``):

        * The model registry raising → ``providers`` is ``[]`` (the picker
          renders an empty list rather than the RPC erroring).
        * ``default_config()`` raising → ``model``/``provider`` are ``None``.
        """
        current_model: str | None = None
        current_provider: str | None = None
        try:
            from opencomputer.agent.config import default_config

            cfg = default_config()
            model_section = getattr(cfg, "model", None)
            current_model = getattr(model_section, "model", None)
            current_provider = getattr(model_section, "provider", None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("model.options: default_config failed: %s", exc)

        providers: list[dict[str, Any]] = []
        try:
            from opencomputer import cli_model_picker

            grouped = cli_model_picker._grouped_models()  # noqa: SLF001
            for name in sorted(grouped):
                providers.append(
                    {
                        "name": name,
                        "models": sorted(grouped[name]),
                        "is_current": name == current_provider,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "model.options: model registry unavailable (%s) — empty list",
                exc,
            )
            providers = []

        return {
            "model": current_model,
            "provider": current_provider,
            "providers": providers,
        }

    @staticmethod
    def _collect_config_value(key: str) -> dict[str, Any]:
        """Build the ``METHOD_CONFIG_GET`` payload for one dotted key.

        Resolves ``key`` against the active profile config via
        :func:`opencomputer.agent.config_store.get_value`. An unknown key
        is reported as ``found=False`` (not an error). The value is coerced
        JSON-safe — config sections are dataclasses, so a section-level key
        (``model``) returns its ``repr`` string rather than failing the
        typed result's JSON serialization.

        Never raises: a config-load failure also degrades to ``found=False``.
        """
        try:
            from opencomputer.agent.config import default_config
            from opencomputer.agent.config_store import get_value

            cfg = default_config()
            raw = get_value(cfg, key)
        except KeyError:
            return {"key": key, "value": None, "found": False}
        except Exception as exc:  # noqa: BLE001
            logger.warning("config.get: resolve failed for %r: %s", key, exc)
            return {"key": key, "value": None, "found": False}

        # Coerce JSON-safe — dataclass config sections aren't serializable.
        try:
            json.dumps(raw)
            value: Any = raw
        except (TypeError, ValueError):
            value = repr(raw)
        return {"key": key, "value": value, "found": True}

    @staticmethod
    def _persist_config_mutation(mutate: Any, *, label: str) -> str | None:
        """Load the profile config, apply ``mutate``, persist + validate.

        ``mutate`` is a callable ``Config -> Config`` (typically built on
        :func:`opencomputer.agent.config_store.set_value`). The new config
        is written, then re-loaded to validate it round-trips; if the
        reload fails the previous ``config.yaml`` is restored from a
        ``.bak`` copy. Mirrors the dashboard ``PUT /api/v1/config`` route's
        write+validate+rollback discipline.

        Returns ``None`` on success, or a human-readable error string on
        failure (caller turns that into an ``ok=False`` wire response).
        Never raises.
        """
        import shutil

        from opencomputer.agent.config_store import (
            config_file_path,
            load_config,
            save_config,
        )

        try:
            path = config_file_path()
        except Exception as exc:  # noqa: BLE001
            return f"{label}: cannot resolve config path: {exc}"

        try:
            cfg = load_config()
        except Exception as exc:  # noqa: BLE001
            return f"{label}: cannot load current config: {exc}"

        try:
            new_cfg = mutate(cfg)
        except KeyError as exc:
            # set_value raises KeyError for an unknown/invalid key — the
            # most common caller error. Surface its message verbatim.
            return f"{label}: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"{label}: could not apply change: {exc}"

        bak = path.with_suffix(".yaml.bak")
        backed_up = False
        if path.exists():
            try:
                shutil.copy2(path, bak)
                backed_up = True
            except OSError as exc:
                return f"{label}: could not back up config before write: {exc}"

        try:
            save_config(new_cfg, path)
            load_config(path)  # validate by round-trip
        except Exception as exc:  # noqa: BLE001
            if backed_up:
                try:
                    shutil.copy2(bak, path)
                except OSError:
                    logger.exception("%s: rollback of %s failed", label, path)
            return f"{label}: write produced an invalid config (rolled back): {exc}"

        return None

    @staticmethod
    def _apply_model_set(provider: str, model: str) -> dict[str, Any] | str:
        """Persist a new default provider+model. dict on success, error str
        on failure (caller maps str → ok=False response)."""
        from opencomputer.agent.config_store import set_value

        def mutate(cfg: Any) -> Any:
            cfg = set_value(cfg, "model.provider", provider)
            return set_value(cfg, "model.model", model)

        err = WireServer._persist_config_mutation(mutate, label="model.set")
        if err is not None:
            return err
        return {"provider": provider, "model": model, "ok": True}

    @staticmethod
    def _apply_config_set(key: str, value: Any) -> dict[str, Any] | str:
        """Persist one config value by dotted key. dict on success, error
        str on failure."""
        from opencomputer.agent.config_store import set_value

        err = WireServer._persist_config_mutation(
            lambda cfg: set_value(cfg, key, value), label="config.set"
        )
        if err is not None:
            return err
        return {"key": key, "value": value, "ok": True}

    async def broadcast_permission_request(
        self,
        *,
        session_id: str,
        request_id: str,
        capability_id: str,
        scope: str | None = None,
        context: str = "",
        timeout_s: float = 300.0,
    ) -> int:
        """Producer-side helper: emit a permission.request to all clients on a session.

        M3.1 (2026-05-09) — invoked by ``WirePromptHandler`` when a
        Tier-2 capability fires on a wire-bound session. Returns the
        number of clients the event was successfully delivered to;
        zero means no client was around to ask, and the consent gate
        will fall through to its 300s timeout + auto-deny path.

        Errors per-client are swallowed (other clients keep getting
        the prompt); a stale dropped connection won't block the
        broadcast.
        """
        clients = list(self._session_clients.get(session_id, ()))
        delivered = 0
        for client_ws in clients:
            try:
                await self._send_event(
                    client_ws,
                    EVENT_PERMISSION_REQUEST,
                    {
                        "request_id": request_id,
                        "session_id": session_id,
                        "capability_id": capability_id,
                        "scope": scope,
                        "context": context,
                        "timeout_s": timeout_s,
                    },
                )
                delivered += 1
            except Exception:  # noqa: BLE001 — never break on stale client
                continue
        return delivered

    def _replay_after_hello(
        self,
        ws: websockets.WebSocketServerProtocol,
        session_id: str,
        last_event_seq: int,
    ) -> tuple[int, bool, list[WireEvent]]:
        """Pick events to replay for a reconnecting client.

        Returns ``(server_last_event_seq, gap_warning, replay_events)``.
        ``gap_warning`` is True when the client's ``last_event_seq``
        falls earlier than the oldest event still in the ring (so some
        events were lost to overflow). ``server_last_event_seq`` is
        the highest seq currently held in the ring (echoed in the
        HelloResult so the client can detect the gap immediately).

        Pure function modulo state read — no side effects so callers
        can unit-test the decision separately from network I/O.
        """
        ring = self._session_rings.get(session_id, deque())
        if not ring:
            return (self._session_seq.get(session_id, 0), False, [])
        oldest_seq = ring[0].seq or 0
        newest_seq = self._session_seq.get(session_id, 0)
        # Replay everything strictly newer than what the client has.
        replay = [ev for ev in ring if (ev.seq or 0) > last_event_seq]
        # Gap when the client missed events the buffer no longer has.
        gap = last_event_seq < oldest_seq - 1 if last_event_seq >= 0 else False
        return (newest_seq, gap, replay)


__all__ = ["RING_BUFFER_MAX", "WireServer"]
