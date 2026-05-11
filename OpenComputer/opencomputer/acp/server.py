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


class _ACPRemoteError(Exception):
    """T64 — IDE returned a JSON-RPC error to one of our outbound calls."""


class ACPServer:
    """ACP JSON-RPC server.

    Stdio-mode (one connection per process) is the primary transport.
    Future: TCP/Unix-socket transports.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ACPSession] = {}
        self._initialized: bool = False
        self._client_capabilities: dict[str, Any] = {}
        # T64 — outbound RPC. When the agent calls a method on the IDE
        # (e.g. session/requestPermission), we allocate an id, write
        # the request, and stash a Future keyed by id. The dispatcher
        # routes any inbound message that has an id but no method to
        # the matching Future.
        self._outbound_counter: int = 0
        self._outbound_futures: dict[
            str, asyncio.Future[dict[str, Any]]
        ] = {}
        # Router: method name -> async handler
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
            "initialize": self._handle_initialize,
            "newSession": self._handle_new_session,
            "loadSession": self._handle_load_session,
            "prompt": self._handle_prompt,
            "cancel": self._handle_cancel,
            "listSessions": self._handle_list_sessions,
            "requestPermission": self._handle_request_permission,
            # Wave 5 T3 — Hermes-port /steer + /queue
            "steer": self._handle_steer,
            "queue": self._handle_queue,
            # PR-A Feature 3 (2026-05-07) — per-session tool gating
            "setSessionPermissions": self._handle_set_session_permissions,
            # T62 — Hermes-doc parity. ACP toolset registration: IDEs
            # call tools/list to learn the agent's tool surface before
            # any prompt, so they can render UI / approval prompts
            # against accurate schemas.
            "tools/list": self._handle_tools_list,
        }

    async def serve_stdio(self) -> None:
        """Run the JSON-RPC loop over stdin/stdout. Blocks until EOF."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_event_loop()
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        # stdout writer is sync — wrap in to_thread for non-blocking writes
        try:
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
        finally:
            # PR-A Feature 3: on ACP transport close, fire SESSION_END
            # for every active session so audit / analytics plugins see
            # the disconnect. Best-effort — a hook crash must not block
            # process shutdown.
            try:
                from opencomputer.hooks.engine import engine as _hook_engine
                from plugin_sdk.hooks import HookContext, HookEvent

                for sid in list(self._sessions.keys()):
                    try:
                        _hook_engine.fire_and_forget(HookContext(
                            event=HookEvent.SESSION_END,
                            session_id=sid,
                        ))
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                logger.debug(
                    "acp: SESSION_END hook fan-out failed", exc_info=True,
                )

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message to its handler."""
        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params", {})

        # T64 — response routing. Inbound messages without a method but
        # with an id are JSON-RPC responses to outbound requests we
        # made (e.g. session/requestPermission). Resolve the matching
        # Future. Stale / duplicate / unknown ids are silently dropped
        # so a misbehaving IDE can't crash the dispatch loop.
        if not method and msg_id is not None:
            fut = self._outbound_futures.pop(str(msg_id), None)
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(
                        _ACPRemoteError(msg["error"].get("message") or "remote error")
                    )
                else:
                    fut.set_result(msg.get("result") or {})
            return

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
        from opencomputer.acp.auth import detect_provider

        return {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "serverInfo": {"name": ACP_SERVER_NAME, "version": ACP_SERVER_VERSION},
            "serverCapabilities": {
                "streaming": True,
                "cancellation": True,
                "provider": detect_provider(),
                # T62 — Hermes-doc toolset registration. IDEs probe this
                # flag before calling tools/list or relying on the
                # session/toolset notification fired post-newSession.
                "toolset": True,
            },
        }

    async def _handle_new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        # _meta.sessionKey override (per openclaw spec) — caller-provided key beats default
        meta = params.get("_meta", {}) or {}
        session_id = meta.get("sessionKey") or f"acp:{uuid.uuid4()}"
        if session_id in self._sessions:
            raise ValueError(f"session {session_id} already exists")
        self._sessions[session_id] = ACPSession(session_id=session_id, send=self._send_notification)
        # PR-A Feature 3: bridge ACP newSession to SESSION_START hook so
        # plugins observing the lifecycle (analytics, audit log, etc.)
        # see ACP-driven sessions on the same event channel as CLI/
        # gateway-driven ones. Fire-and-forget — a hook crash must not
        # block the session creation.
        try:
            from opencomputer.hooks.engine import engine as _hook_engine
            from plugin_sdk.hooks import HookContext, HookEvent

            _hook_engine.fire_and_forget(HookContext(
                event=HookEvent.SESSION_START,
                session_id=session_id,
            ))
        except Exception:  # noqa: BLE001
            logger.debug("acp: SESSION_START hook fire failed", exc_info=True)

        # T62 — proactive toolset announcement so IDEs that don't probe
        # tools/list still see what's available. Schedule for the next
        # event-loop tick so the newSession result is delivered FIRST
        # (the session id reaches the client before the announcement
        # that names it). Best-effort: registry import failures must
        # not block session creation.
        async def _announce_toolset() -> None:
            try:
                self._send_notification(
                    "session/toolset",
                    {
                        "sessionId": session_id,
                        "tools": self._collect_tool_descriptors(),
                    },
                )
            except Exception:  # noqa: BLE001
                logger.debug("acp: session/toolset notification failed", exc_info=True)

        try:
            asyncio.create_task(_announce_toolset())
        except RuntimeError:
            # No running loop (e.g. _handle_new_session called from a
            # synchronous test harness). Fall back to inline emit so
            # the contract still holds.
            try:
                self._send_notification(
                    "session/toolset",
                    {
                        "sessionId": session_id,
                        "tools": self._collect_tool_descriptors(),
                    },
                )
            except Exception:  # noqa: BLE001
                logger.debug("acp: session/toolset notification failed", exc_info=True)
        return {"sessionId": session_id}

    def _collect_tool_descriptors(self) -> list[dict[str, Any]]:
        """Return the registered tool list as ACP-shaped descriptors.

        Each entry: ``{name, description, input_schema}`` (Anthropic-
        compatible shape). Imported lazily so the ACP module stays
        independent of the tools registry import path.
        """
        from opencomputer.tools import registry as _registry_mod

        out: list[dict[str, Any]] = []
        for tool in _registry_mod.registry._tools.values():
            schema = tool.schema
            out.append(
                {
                    "name": schema.name,
                    "description": schema.description,
                    "input_schema": schema.parameters,
                }
            )
        return out

    async def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """T62 — JSON-RPC ``tools/list`` returning all registered tools."""
        return {"tools": self._collect_tool_descriptors()}

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

    async def _handle_steer(self, params: dict[str, Any]) -> dict[str, Any]:
        """Wave 5 T3 — Hermes-port /steer.

        Interrupt the in-flight turn (or queue the next user message on
        idle) with new user text. Returns ``{status: "interrupted",
        text: <text>}``.
        """
        session_id = params.get("sessionId")
        if not session_id or session_id not in self._sessions:
            raise KeyError(f"session not found: {session_id}")
        text = params.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        await self._sessions[session_id].steer(text)
        return {"status": "interrupted", "text": text}

    async def _handle_queue(self, params: dict[str, Any]) -> dict[str, Any]:
        """Wave 5 T3 — Hermes-port /queue.

        Append user text to the per-session queue that drains after the
        current turn. Returns ``{status: "queued", text: <text>,
        pending: <count>}``.
        """
        session_id = params.get("sessionId")
        if not session_id or session_id not in self._sessions:
            raise KeyError(f"session not found: {session_id}")
        text = params.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        sess = self._sessions[session_id]
        await sess.queue(text)
        return {"status": "queued", "text": text, "pending": len(sess.queued)}

    async def _handle_request_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle IDE-side permission request. Auto-deny if no session/gate.

        PR-A Feature 3: accepts an optional ``tier`` parameter
        (``IMPLICIT`` / ``EXPLICIT`` / ``PER_ACTION`` / ``DELEGATED`` —
        mirrors ``plugin_sdk.consent.ConsentTier`` exactly) that the IDE
        can pass through to drive the consent gate's tier directly.
        Defaults to ``PER_ACTION`` for backwards compat.
        """
        session_id = params.get("sessionId", "")
        command = params.get("command", "unknown")
        description = params.get("description", "")
        tier = params.get("tier", "PER_ACTION")
        session = self._sessions.get(session_id)
        if session is None:
            return {"outcome": "deny", "reason": "session not found"}
        gate = getattr(session, "_consent_gate", None)
        if gate is None:
            logger.debug(
                "acp: requestPermission for session %s — no gate, auto-deny", session_id
            )
            return {"outcome": "deny", "reason": "no consent gate in this session"}
        from opencomputer.acp.permissions import make_approval_callback

        loop = asyncio.get_event_loop()
        try:
            cb = make_approval_callback(
                session_id, gate, loop, default_tier=tier,
            )
        except ValueError as exc:
            return {"outcome": "deny", "reason": f"invalid tier: {exc}"}
        outcome = cb(command, description)
        return {"outcome": outcome}

    async def _handle_set_session_permissions(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """PR-A Feature 3: update per-session allowed/denied tools.

        Race-safe: applies to *future* tool dispatches only; in-flight
        tools complete unaffected. ``allowedTools`` is descriptive
        metadata; ``deniedTools`` is the security gate (consulted in
        ``_dispatch_tool_calls`` via ``RuntimeContext.acp_denied_tools``).

        Either ``allowedTools`` or ``deniedTools`` may be omitted or
        ``None`` — those fields are left unchanged. Pass an empty list
        to explicitly clear.
        """
        session_id = params.get("sessionId")
        if not session_id or session_id not in self._sessions:
            raise KeyError(f"session not found: {session_id}")
        session = self._sessions[session_id]
        allowed_raw = params.get("allowedTools")
        denied_raw = params.get("deniedTools")
        allowed = (
            frozenset(allowed_raw) if allowed_raw is not None else None
        )
        denied = (
            frozenset(denied_raw) if denied_raw is not None else None
        )
        session.update_permissions(allowed=allowed, denied=denied)
        return {
            "sessionId": session_id,
            "allowedTools": list(session.allowed_tools),
            "deniedTools": list(session.denied_tools),
        }

    # --- transport ---

    async def request_permission(
        self,
        *,
        session_id: str,
        command: str,
        description: str,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """T64 — outbound `session/requestPermission` to the IDE.

        Sends a JSON-RPC *request* (with id) and awaits the IDE's
        response. The IDE is expected to reply with
        ``{"outcome": "allow"|"deny", "grantType": "once"|"always"}``.

        On timeout, JSON-RPC error response, or any internal failure,
        returns a deny verdict with a structured ``reason`` field so
        the consent gate can audit the denial cause.
        """
        self._outbound_counter += 1
        request_id = f"oc-out-{self._outbound_counter}"
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._outbound_futures[request_id] = fut

        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/requestPermission",
                "params": {
                    "sessionId": session_id,
                    "command": command,
                    "description": description,
                },
            }
        )

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self._outbound_futures.pop(request_id, None)
            return {"outcome": "deny", "reason": "timeout"}
        except _ACPRemoteError as exc:
            return {"outcome": "deny", "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"outcome": "deny", "reason": f"{type(exc).__name__}: {exc}"}

        outcome = result.get("outcome", "deny")
        verdict: dict[str, Any] = {"outcome": outcome}
        if "grantType" in result:
            verdict["grantType"] = result["grantType"]
        if "reason" in result:
            verdict["reason"] = result["reason"]
        return verdict

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


#: PascalCase canonical name (``AcpServer``). Behaviourally identical
#: to :class:`ACPServer`. Defined as a thin subclass rather than a
#: bare alias so ``__name__`` reflects the canonical spelling at
#: introspection time. Existing ``ACPServer`` import sites continue
#: to work.
class AcpServer(ACPServer):
    """PascalCase alias of :class:`ACPServer` — see parent docstring."""


__all__ = ["ACPServer", "AcpServer"]
