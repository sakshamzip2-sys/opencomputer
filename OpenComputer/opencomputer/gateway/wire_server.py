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
from typing import Any

import websockets

from opencomputer.agent.loop import AgentLoop
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
    WireEvent,
    WireRequest,
    WireResponse,
)

logger = logging.getLogger("opencomputer.gateway.wire_server")


class WireServer:
    """Minimal JSON-RPC-over-WebSocket server for local clients."""

    def __init__(
        self,
        loop: AgentLoop,
        host: str = "127.0.0.1",
        port: int = 18789,
    ) -> None:
        self.loop = loop
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
            limit = int(req.params.get("limit", 20))
            rows = self.loop.db.list_sessions(limit=limit)
            await self._send_response(ws, req.id, True, payload={"sessions": rows})
        elif req.method == METHOD_SEARCH:
            query = str(req.params.get("query", ""))
            limit = int(req.params.get("limit", 20))
            hits = self.loop.db.search(query, limit=limit)
            await self._send_response(ws, req.id, True, payload={"hits": hits})
        elif req.method == METHOD_SKILLS_LIST:
            skills = self.loop.memory.list_skills()
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

        try:
            result = await self.loop.run_conversation(
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
