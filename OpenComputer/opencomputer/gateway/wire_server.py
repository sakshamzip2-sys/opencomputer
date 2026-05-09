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
import uuid
from collections import deque
from typing import TYPE_CHECKING, Any

import websockets

from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.steer import default_registry as _steer_registry
from opencomputer.gateway.protocol import (
    EVENT_ASSISTANT_MESSAGE,
    EVENT_ERROR,
    EVENT_PERMISSION_REQUEST,
    EVENT_TURN_BEGIN,
    EVENT_TURN_END,
    METHOD_CHAT,
    METHOD_HELLO,
    METHOD_PERMISSION_RESPONSE,
    METHOD_SEARCH,
    METHOD_SESSION_LIST,
    METHOD_SKILLS_LIST,
    METHOD_SLASH_DISPATCH,
    METHOD_SLASH_LIST,
    METHOD_STEER_SUBMIT,
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

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handle_client, self.host, self.port
        )
        logger.info("wire: listening on ws://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(
        self, ws: websockets.WebSocketServerProtocol
    ) -> None:
        client_id = str(uuid.uuid4())[:8]
        logger.info("wire: client %s connected", client_id)
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
                    ],
                    "events": [
                        EVENT_TURN_BEGIN,
                        EVENT_TURN_END,
                        EVENT_ASSISTANT_MESSAGE,
                        EVENT_ERROR,
                        EVENT_PERMISSION_REQUEST,
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

        try:
            with set_profile(profile_home):
                result = await loop.run_conversation(
                    user_message=user_message,
                    session_id=session_id,
                    stream_callback=lambda t: asyncio.create_task(_on_chunk(t)),
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
