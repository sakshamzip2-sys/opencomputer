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
from typing import TYPE_CHECKING, Any

import websockets

from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.steer import default_registry as _steer_registry
from opencomputer.gateway.protocol import (
    EVENT_ASSISTANT_MESSAGE,
    EVENT_ERROR,
    EVENT_TURN_BEGIN,
    EVENT_TURN_END,
    METHOD_CHAT,
    METHOD_HELLO,
    METHOD_SEARCH,
    METHOD_SESSION_LIST,
    METHOD_SKILLS_LIST,
    METHOD_STEER_SUBMIT,
    WireEvent,
    WireRequest,
    WireResponse,
)

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
            logger.info("wire: client %s disconnected", client_id)

    async def _dispatch(
        self, ws: websockets.WebSocketServerProtocol, req: WireRequest
    ) -> None:
        if req.method == METHOD_HELLO:
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
                    ],
                    "events": [
                        EVENT_TURN_BEGIN,
                        EVENT_TURN_END,
                        EVENT_ASSISTANT_MESSAGE,
                        EVENT_ERROR,
                    ],
                },
            )
        elif req.method == METHOD_CHAT:
            await self._handle_chat(ws, req)
        elif req.method == METHOD_SESSION_LIST:
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
        ev = WireEvent(event=event_name, payload=payload)
        await ws.send(ev.model_dump_json())


__all__ = ["WireServer"]
