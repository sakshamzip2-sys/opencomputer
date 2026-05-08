"""APIServerAdapter — REST endpoint exposing the agent over HTTP (G.28 / Tier 4.x).

Differs from the other Tier 4 adapters: it doesn't connect TO an
external service — it EXPOSES an HTTP server callers POST to, like
``opencomputer wire`` but over plain JSON-over-HTTP rather than
WebSocket.

Endpoint shape::

    POST /v1/chat
    Authorization: Bearer <token>
    Content-Type: application/json

    {"session_id": "<optional>", "message": "<user text>"}

Response::

    {"session_id": "<id>", "response": "<agent reply>"}

Currently the adapter is a thin ``aiohttp`` server that only exposes
the endpoint contract — wiring it into the actual agent loop happens
when the host calls ``set_handler(callable)`` after registration. This
keeps the SDK boundary clean: the adapter doesn't import from
``opencomputer.*``, the host injects the handler.

Bind defaults to ``127.0.0.1`` so a misconfigured install doesn't
expose the agent to the public internet. To bind publicly the user
must explicitly set ``API_SERVER_HOST=0.0.0.0`` AND set a strong
``API_SERVER_TOKEN``.

Capabilities: none of the message-shaping flags apply — this is a
request/response surface, not a streaming chat channel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

# T3 — adapter start time for uptime computation. Module-level so the
# value survives across requests + adapter rebuilds within a process.
_ADAPTER_START_TIME: float = time.monotonic()


def _count_active_sessions() -> int | None:
    """SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL.

    Returns None on any failure (DB missing, schema drift, contention).
    """
    try:
        import sqlite3
        from pathlib import Path

        profile = os.environ.get("OPENCOMPUTER_PROFILE", "default")
        db_path = Path.home() / ".opencomputer" / profile / "sessions.db"
        if not db_path.exists():
            return None
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL"
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return None


def _count_total_sessions() -> int | None:
    try:
        import sqlite3
        from pathlib import Path

        profile = os.environ.get("OPENCOMPUTER_PROFILE", "default")
        db_path = Path.home() / ".opencomputer" / profile / "sessions.db"
        if not db_path.exists():
            return None
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM sessions")
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return None


def _process_memory_mb() -> float | None:
    try:
        import psutil

        return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001
        return None

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, SendResult


def _load_openai_format():
    """Load the sibling openai_format.py — robust to how adapter.py is
    loaded (plugin-loader, package import, or `spec_from_file_location`
    in tests)."""
    import importlib.util as _ilu
    from pathlib import Path as _Path
    _path = _Path(__file__).resolve().parent / "openai_format.py"
    _spec = _ilu.spec_from_file_location("api_server_openai_format", _path)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    return _mod


_of = _load_openai_format()
oc_response_to_openai = _of.oc_response_to_openai
openai_to_oc_messages = _of.openai_to_oc_messages
streaming_delta_chunk = _of.streaming_delta_chunk
streaming_final_chunk = _of.streaming_final_chunk
# Hermes parity (2026-05-08): multi-profile model name + Responses-API stub.
list_models = _of.list_models
oc_response_to_responses_api = _of.oc_response_to_responses_api

logger = logging.getLogger("opencomputer.ext.api_server")


def _safe_json_str(s: str) -> str:
    """JSON-encode a string for safe inclusion in an SSE error chunk."""
    import json as _json

    return _json.dumps(s)


# Type alias for the handler the host injects. Takes (session_id, text)
# and returns the agent's reply.
ChatHandler = Callable[[str, str], Awaitable[str]]

# E.2 (2026-05-05) — per-token streaming handler. Takes (session_id,
# text, on_delta_async). Drives the agent loop; calls on_delta for each
# emitted token/text chunk. Returns when generation is complete (no
# return value — caller closes the SSE stream).
StreamingChatHandler = Callable[
    [str, str, Callable[[str], Awaitable[None]]],
    Awaitable[None],
]


class APIServerAdapter(BaseChannelAdapter):
    """REST API channel — exposes /v1/chat for external callers."""

    platform = Platform.WEB
    max_message_length = 200_000
    """Doubled from 100_000 (2026-05-05) in the cap-doubling sweep.
    REST callers may legitimately POST larger payloads than chat
    platforms (e.g. a CI-system POSTing a build log). Still bounded so
    a misbehaving caller can't OOM the process."""

    capabilities = ChannelCapabilities(0)
    """No message-shaping capabilities — request/response surface only."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._host: str = config.get("host", "127.0.0.1")
        self._port: int = int(config.get("port", 18791))
        self._token: str = config["token"]
        self._handler: ChatHandler | None = None
        # E.2 — per-token streaming handler. When set AND request has
        # stream=True, the OpenAI-compat endpoint emits one SSE chunk
        # per delta. Falls back to single-chunk path when None.
        self._streaming_handler: StreamingChatHandler | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        # Wave 6.A — Hermes-port (0a15dbdc4) — track in-flight chat runs
        # by run_id so POST /v1/runs/{id}/stop can cancel the underlying
        # asyncio.Task. Cleared in _handle_chat's finally block on every
        # outcome (completion / error / cancel).
        self._active_runs: dict[str, asyncio.Task[Any]] = {}

    def set_handler(self, handler: ChatHandler) -> None:
        """Inject the per-request agent handler.

        The host (``opencomputer.gateway`` or a custom embed) calls this
        after registration. Without a handler set, requests return 503.
        """
        self._handler = handler

    def set_streaming_handler(self, handler: StreamingChatHandler) -> None:
        """Inject the per-token streaming handler (E.2).

        When set AND the OpenAI-compat endpoint receives ``stream=True``,
        the response emits one SSE chunk per token (or text delta) the
        agent produces. When unset, ``stream=True`` falls back to the
        legacy single-chunk path so existing clients keep working.
        """
        self._streaming_handler = handler

    # ─── HTTP handler ───────────────────────────────────────────────

    async def _handle_chat(self, request: web.Request) -> web.Response:
        # Auth: Bearer token must match the configured value exactly.
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != self._token:
            return web.json_response(
                {"error": "unauthorized"}, status=401
            )
        if request.content_length and request.content_length > self.max_message_length:
            return web.json_response(
                {"error": "payload too large"}, status=413
            )
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response(
                {"error": "invalid json body"}, status=400
            )
        message = payload.get("message", "")
        session_id = payload.get("session_id", "")
        if not isinstance(message, str) or not message.strip():
            return web.json_response(
                {"error": "missing or empty 'message' field"}, status=400
            )
        if self._handler is None:
            return web.json_response(
                {"error": "agent handler not bound"}, status=503
            )
        # Wave 6.A — Hermes-port (0a15dbdc4 POST /v1/runs/{id}/stop).
        # Generate a run_id and track the underlying asyncio task so a
        # client can cancel it. Cleanup happens unconditionally in the
        # finally block.
        import asyncio as _asyncio
        import uuid as _uuid

        run_id = _uuid.uuid4().hex
        task = _asyncio.create_task(self._handler(session_id, message))
        self._active_runs[run_id] = task
        try:
            reply = await task
        except _asyncio.CancelledError:
            return web.json_response(
                {"session_id": session_id, "run_id": run_id, "stopped": True},
                status=499,  # client-closed-request convention
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("api-server handler raised")
            return web.json_response(
                {"error": f"handler error: {type(e).__name__}"}, status=500
            )
        finally:
            self._active_runs.pop(run_id, None)
        return web.json_response(
            {"session_id": session_id, "run_id": run_id, "response": reply}
        )

    async def _handle_run_stop(self, request: web.Request) -> web.Response:
        """``POST /v1/runs/{run_id}/stop`` — cancel an in-flight chat run.

        Wave 6.A — Hermes-port (0a15dbdc4). Returns 200 if cancelled, 404
        if the run_id is unknown (already completed or never existed).
        """
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != self._token:
            return web.json_response({"error": "unauthorized"}, status=401)
        run_id = request.match_info.get("run_id", "")
        task = self._active_runs.get(run_id)
        if task is None:
            return web.json_response(
                {"error": "unknown run_id (already finished or never existed)"},
                status=404,
            )
        task.cancel()
        return web.json_response({"run_id": run_id, "stopped": True})

    # ─── Server lifecycle ───────────────────────────────────────────

    async def _handle_openai_chat_completions(
        self, request: web.Request
    ) -> web.Response:
        """OpenAI-compatible ``POST /v1/chat/completions`` handler.

        T2 of tier-2 trio (2026-05-04). Lets external tools that speak the
        OpenAI Chat Completions API (Cursor, aider, LibreChat, anything
        using the OpenAI SDK) plug OC in by setting ``OPENAI_API_BASE`` to
        this server's URL.

        v1 simplifications (deferred to follow-ups):
          - All input messages collapse into a single annotated user
            string ("[role] content"). True multi-turn history would
            need agent-loop integration.
          - Streaming returns the full response as a single SSE chunk
            (not per-token). Token-by-token streaming needs deeper hook
            into the agent loop.
          - Token counts are whitespace-split estimates.
        """
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != self._token:
            return web.json_response(
                {
                    "error": {
                        "message": "Invalid API key",
                        "type": "invalid_request_error",
                    }
                },
                status=401,
            )

        if (
            request.content_length
            and request.content_length > self.max_message_length
        ):
            return web.json_response(
                {"error": {"message": "payload too large"}}, status=413
            )

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response(
                {"error": {"message": "invalid json body"}}, status=400
            )

        messages = payload.get("messages", [])
        model = payload.get("model", "opencomputer")
        stream = bool(payload.get("stream", False))
        if not isinstance(messages, list) or not messages:
            return web.json_response(
                {"error": {"message": "messages required"}}, status=400
            )

        # E.2 — when streaming is requested, the streaming_handler is
        # sufficient; otherwise we need the legacy handler. 503 only when
        # neither path is viable for the requested mode.
        if stream and self._streaming_handler is None and self._handler is None:
            return web.json_response(
                {"error": {"message": "handler not configured"}}, status=503
            )
        if not stream and self._handler is None:
            return web.json_response(
                {"error": {"message": "handler not configured"}}, status=503
            )

        oc_messages = openai_to_oc_messages(messages)
        user_text = "\n".join(
            f"[{m['role']}] {m['content']}" for m in oc_messages
        )
        session_id = payload.get("session_id", "")

        if stream:
            chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            resp = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
            await resp.prepare(request)

            # E.2 — per-token streaming path. When a streaming handler is
            # registered, drive it with an on_delta callback that pushes
            # one SSE chunk per text delta. Fall through to the legacy
            # single-chunk path when only the synchronous handler exists.
            if self._streaming_handler is not None:
                async def _on_delta(text: str) -> None:
                    if not text:
                        return
                    await resp.write(
                        f"data: {streaming_delta_chunk(chunk_id, model, text)}\n\n".encode()
                    )

                try:
                    await self._streaming_handler(session_id, user_text, _on_delta)
                except Exception as e:  # noqa: BLE001
                    logger.exception("openai-compat streaming handler raised")
                    # We've already written the SSE headers; we can't
                    # rewrite them as a 500. Emit an error chunk + DONE
                    # so the client sees the failure cleanly.
                    err_payload = (
                        '{"error":{"message":'
                        + _safe_json_str(str(e))
                        + ',"type":"server_error"}}'
                    )
                    await resp.write(f"data: {err_payload}\n\n".encode())
                    await resp.write(b"data: [DONE]\n\n")
                    await resp.write_eof()
                    return resp

                await resp.write(
                    f"data: {streaming_final_chunk(chunk_id, model)}\n\n".encode()
                )
                await resp.write(b"data: [DONE]\n\n")
                await resp.write_eof()
                return resp

            # Legacy single-chunk fallback (back-compat).
            try:
                agent_text = await self._handler(session_id, user_text)
            except Exception as e:  # noqa: BLE001
                logger.exception("openai-compat handler raised")
                return web.json_response(
                    {"error": {"message": str(e)}}, status=500
                )
            await resp.write(
                f"data: {streaming_delta_chunk(chunk_id, model, agent_text)}\n\n".encode()
            )
            await resp.write(
                f"data: {streaming_final_chunk(chunk_id, model)}\n\n".encode()
            )
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        try:
            agent_text = await self._handler(session_id, user_text)
        except Exception as e:  # noqa: BLE001
            logger.exception("openai-compat handler raised")
            return web.json_response(
                {"error": {"message": str(e)}}, status=500
            )

        return web.json_response(
            oc_response_to_openai(
                agent_text,
                model=model,
                input_tokens=len(user_text.split()),
                output_tokens=len(agent_text.split()),
            )
        )

    def _build_app(self) -> web.Application:
        # Limit per-request body size at the framework level so large
        # uploads don't even reach the handler.
        app = web.Application(client_max_size=self.max_message_length)
        app.router.add_post("/v1/chat", self._handle_chat)
        # T2 (tier-2 trio, 2026-05-04) — OpenAI Chat Completions compat.
        app.router.add_post(
            "/v1/chat/completions", self._handle_openai_chat_completions
        )
        # Hermes parity (2026-05-08) — Open-WebUI multi-profile model
        # discovery via GET /v1/models. Auth-required.
        app.router.add_get("/v1/models", self._handle_list_models)
        # Hermes parity (2026-05-08) — opt-in Responses-API stub.
        # Returns 404 unless API_SERVER_API_TYPE=responses.
        app.router.add_post("/v1/responses", self._handle_responses_stub)
        # Wave 6.A — Hermes-port (0a15dbdc4) — POST /v1/runs/{id}/stop
        app.router.add_post("/v1/runs/{run_id}/stop", self._handle_run_stop)
        # T2 — Hermes-doc parity. Public capability probe (no auth).
        app.router.add_get("/v1/capabilities", self._handle_capabilities)
        # T3 — Hermes-doc parity. Public detailed health probe (no auth).
        app.router.add_get("/health/detailed", self._handle_health_detailed)
        return app

    # ─── T2 — /v1/capabilities ──────────────────────────────────────

    async def _handle_capabilities(self, request: web.Request) -> web.Response:
        """Machine-readable feature flag dict (Hermes-doc parity).

        Public — no Bearer token required, so frontends can probe
        capability before negotiating auth. Honest about deferred items
        (``runs_api`` / ``jobs_api`` / ``previous_response_id``).
        """
        profile = os.environ.get("OPENCOMPUTER_PROFILE", "default")
        payload = {
            "version": "1",
            "model": profile,
            "profile": profile,
            "features": {
                "chat_completions": True,
                "responses": True,  # stub exists at /v1/responses
                "streaming": True,
                "tool_calls": True,
                "vision": True,
                "system_prompt": True,
                "previous_response_id": False,
                "runs_api": False,
                "jobs_api": False,
            },
        }
        return web.json_response(payload)

    # ─── T3 — /health/detailed ──────────────────────────────────────

    async def _handle_health_detailed(self, request: web.Request) -> web.Response:
        """Detailed health probe — sessions / agents / uptime / memory.

        Never returns 5xx. Sub-lookup failures surface as ``null`` fields
        so monitoring agents don't false-alarm on transient DB contention.
        """
        # Read the helpers from the module namespace so monkeypatched
        # versions in tests are honored. ``__name__`` resolves to the
        # actual module (works whether loaded by package or by file path).
        import importlib
        mod = importlib.import_module(__name__)
        try:
            sessions_active = mod._count_active_sessions()
        except Exception:  # noqa: BLE001
            sessions_active = None
        try:
            sessions_total = mod._count_total_sessions()
        except Exception:  # noqa: BLE001
            sessions_total = None
        try:
            memory_mb = mod._process_memory_mb()
        except Exception:  # noqa: BLE001
            memory_mb = None

        if sessions_active is None and sessions_total is None:
            sessions_block: dict[str, Any] | None = None
        else:
            sessions_block = {"active": sessions_active, "total": sessions_total}

        running_agents = (
            len(self._active_runs) if hasattr(self, "_active_runs") else 0
        )
        uptime = max(0.0, time.monotonic() - _ADAPTER_START_TIME)

        payload = {
            "status": "ok",
            "uptime_seconds": round(uptime, 1),
            "sessions": sessions_block,
            "running_agents": running_agents,
            "memory_mb": memory_mb,
            "api_server": {
                "host": self._host,
                "port": self._port,
                "profile": os.environ.get("OPENCOMPUTER_PROFILE", "default"),
            },
        }
        return web.json_response(payload)

    async def _handle_list_models(self, request: web.Request) -> web.Response:
        """Hermes parity (2026-05-08): advertise active profile as model id.

        GET /v1/models returns the OpenAI-compatible models list, advertising
        the active OC profile name so Open-WebUI sees per-profile servers as
        distinct models. Override via API_SERVER_MODEL_NAME env var.
        """
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != self._token:
            return web.json_response(
                {
                    "error": {
                        "message": "Invalid API key",
                        "type": "invalid_request_error",
                    }
                },
                status=401,
            )
        # Resolve active profile lazily — fall back to "default" if not set.
        profile = (
            os.environ.get("OPENCOMPUTER_PROFILE")
            or os.environ.get("OC_PROFILE")
            or "default"
        )
        env_override = os.environ.get("API_SERVER_MODEL_NAME")
        return web.json_response(
            list_models(profile_name=profile, env_override=env_override)
        )

    async def _handle_responses_stub(self, request: web.Request) -> web.Response:
        """Hermes parity (2026-05-08): opt-in Responses-API stub.

        Gated on ``API_SERVER_API_TYPE=responses`` env var; returns 404
        otherwise. Stub builds a Responses-shaped envelope from the
        chat-completions response. Full SSE event semantics
        (``function_call``, ``function_call_output``) deferred to demand.
        """
        if os.environ.get("API_SERVER_API_TYPE", "").lower() != "responses":
            return web.json_response(
                {"error": {"message": "Responses API disabled. Set API_SERVER_API_TYPE=responses to enable."}},
                status=404,
            )
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != self._token:
            return web.json_response(
                {
                    "error": {
                        "message": "Invalid API key",
                        "type": "invalid_request_error",
                    }
                },
                status=401,
            )
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response(
                {"error": {"message": "invalid json body"}}, status=400
            )

        # Responses-API uses `input` (str) instead of chat-completions `messages`.
        # Accept both for ergonomics.
        if isinstance(payload.get("input"), str):
            user_text = payload["input"]
        else:
            messages = payload.get("messages", [])
            if not isinstance(messages, list) or not messages:
                return web.json_response(
                    {"error": {"message": "input or messages required"}},
                    status=400,
                )
            oc_messages = openai_to_oc_messages(messages)
            user_text = "\n".join(
                f"[{m['role']}] {m['content']}" for m in oc_messages
            )

        if self._handler is None:
            return web.json_response(
                {"error": {"message": "handler not configured"}}, status=503
            )

        session_id = payload.get("session_id", "")
        try:
            agent_text = await self._handler(user_text, session_id) or ""
        except Exception as exc:  # noqa: BLE001
            logger.exception("api-server: responses-stub handler raised")
            return web.json_response(
                {"error": {"message": f"agent error: {exc}"}}, status=500
            )

        model = payload.get("model") or "opencomputer"
        return web.json_response(
            oc_response_to_responses_api(
                agent_text,
                model=model,
                input_tokens=len(user_text.split()),
                output_tokens=len(agent_text.split()),
            )
        )

    async def connect(self) -> None:
        """Start the aiohttp server bound to host:port."""
        if self._runner is not None:
            return
        app = self._build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info(
            "api-server listening on http://%s:%d/v1/chat", self._host, self._port
        )

    async def disconnect(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ─── Outbound: not applicable ───────────────────────────────────

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        # API server is request/response — there's no "outbound" send
        # outside of the response to an active request. Return a clear
        # not-implemented so any caller that mistakenly tries to use
        # this adapter as a chat channel sees a useful error.
        return SendResult(
            success=False,
            error=(
                "api-server is a REST endpoint, not a push channel — "
                "callers receive responses synchronously via POST /v1/chat"
            ),
        )
