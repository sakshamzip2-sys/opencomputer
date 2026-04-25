"""ACPServer — JSON-RPC over stdio for IDE clients.

Spec reference: openclaw 2026.4.23 docs.acp.md.
Tool routing: adapted from hermes-agent acp_adapter/.

Lifecycle:
    1. IDE connects via stdio.
    2. IDE → server: initialize {clientCapabilities, mcp?}
    3. server → IDE: response with serverCapabilities
    4. IDE → server: newSession or loadSession
    5. IDE → server: prompt {sessionId, content, _meta?}
    6. server → IDE: streaming notifications (toolCall, contentDelta, done)
    7. Repeat 5-6.
    8. IDE disconnects (stdio EOF) → server cleans up.

Concurrency: one session per stdio process by default. Multi-session per
process supported via session_id routing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from opencomputer.acp.session import ACPSession

logger = logging.getLogger(__name__)

# JSON-RPC error codes
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
# App-specific (per openclaw conventions)
ERR_SESSION_NOT_FOUND = -32001
ERR_PROMPT_FAILED = -32002

# Server identity
ACP_SERVER_NAME = "opencomputer"
ACP_SERVER_VERSION = "0.1.0"
ACP_PROTOCOL_VERSION = "0.9.0"  # mirrors hermes/openclaw acp dep version


class ACPServer:
    """ACP JSON-RPC server.

    Stdio-mode (one connection per process) is the primary transport.
    Future: TCP/Unix-socket transports.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ACPSession] = {}
        self._initialized: bool = False
        self._client_capabilities: dict[str, Any] = {}
        # Router: method name -> async handler
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
            "initialize": self._handle_initialize,
            "newSession": self._handle_new_session,
            "loadSession": self._handle_load_session,
            "prompt": self._handle_prompt,
            "cancel": self._handle_cancel,
            "listSessions": self._handle_list_sessions,
        }

    async def serve_stdio(self) -> None:
        """Run the JSON-RPC loop over stdin/stdout. Blocks until EOF."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_event_loop()
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        # stdout writer is sync — wrap in to_thread for non-blocking writes
        while True:
            line_bytes = await reader.readline()
            if not line_bytes:
                # EOF
                logger.info("acp: stdin closed; shutting down")
                break
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                self._send_error(None, ERR_PARSE, f"parse error: {exc}")
                continue
            asyncio.create_task(self._dispatch(msg))

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message to its handler."""
        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params", {})

        if not method:
            self._send_error(msg_id, ERR_INVALID_REQUEST, "missing method")
            return

        # Initialize is special — must run first; other methods rejected before init
        if method != "initialize" and not self._initialized:
            self._send_error(msg_id, ERR_INVALID_REQUEST, "server not initialized")
            return

        handler = self._handlers.get(method)
        if handler is None:
            self._send_error(msg_id, ERR_METHOD_NOT_FOUND, f"unknown method: {method}")
            return

        try:
            result = await handler(params)
            self._send_result(msg_id, result)
        except KeyError as exc:
            self._send_error(msg_id, ERR_SESSION_NOT_FOUND, str(exc))
        except ValueError as exc:
            self._send_error(msg_id, ERR_INVALID_PARAMS, str(exc))
        except Exception as exc:
            logger.exception("acp: handler %s failed", method)
            self._send_error(msg_id, ERR_INTERNAL, f"{type(exc).__name__}: {exc}")

    # --- handlers ---

    async def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        self._client_capabilities = params.get("clientCapabilities", {}) or {}
        self._initialized = True
        return {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "serverName": ACP_SERVER_NAME,
            "serverVersion": ACP_SERVER_VERSION,
            "serverCapabilities": {
                "promptStreaming": True,
                "sessionPersistence": True,
                "tools": True,
                "cancel": True,
            },
        }

    async def _handle_new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        # _meta.sessionKey override (per openclaw spec) — caller-provided key beats default
        meta = params.get("_meta", {}) or {}
        session_id = meta.get("sessionKey") or f"acp:{uuid.uuid4()}"
        if session_id in self._sessions:
            raise ValueError(f"session {session_id} already exists")
        self._sessions[session_id] = ACPSession(session_id=session_id, send=self._send_notification)
        return {"sessionId": session_id}

    async def _handle_load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId")
        if not session_id or not isinstance(session_id, str):
            raise ValueError("sessionId is required")
        if session_id in self._sessions:
            return {"sessionId": session_id, "loaded": "from-memory"}
        # Restore from SessionDB if available
        session = ACPSession(session_id=session_id, send=self._send_notification)
        loaded = await session.load_from_db()
        if not loaded:
            raise KeyError(f"session not found: {session_id}")
        self._sessions[session_id] = session
        return {"sessionId": session_id, "loaded": "from-db"}

    async def _handle_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId")
        if not session_id or session_id not in self._sessions:
            raise KeyError(f"session not found: {session_id}")
        content = params.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        session = self._sessions[session_id]
        # Returns final result; streaming events emitted via session.send (notification path)
        result = await session.send_prompt(content)
        return result

    async def _handle_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId")
        if not session_id or session_id not in self._sessions:
            raise KeyError(f"session not found: {session_id}")
        cancelled = await self._sessions[session_id].cancel()
        return {"cancelled": cancelled}

    async def _handle_list_sessions(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"sessions": [{"sessionId": sid} for sid in self._sessions]}

    # --- transport ---

    def _send_result(self, msg_id: int | str | None, result: Any) -> None:
        self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _send_error(self, msg_id: int | str | None, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})

    def _send_notification(self, method: str, params: Any) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, msg: dict[str, Any]) -> None:
        try:
            sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            logger.warning("acp: stdout write failed (client disconnected)")
