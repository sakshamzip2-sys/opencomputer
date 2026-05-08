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
import contextvars
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, SendResult

# T61 — per-request profile contextvar. Populated by every endpoint
# handler before invoking the registered agent handler; reset on the
# way out. Async tasks spawned during the request inherit the value
# via standard ``contextvars`` propagation.
_CURRENT_PROFILE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "opencomputer_api_server_request_profile", default=None
)


def get_current_request_profile() -> str | None:
    """Public helper — current request's resolved X-OC-Profile, or None.

    Handlers registered via ``set_handler`` / ``set_streaming_handler*``
    can call this to learn which OpenComputer profile the in-flight
    request targets, without needing a backreference to the adapter.
    """
    return _CURRENT_PROFILE.get()

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


class StreamHooks:
    """V2 streaming hooks — provides both text and tool-progress emission.

    Hermes-doc parity: ``event: hermes.tool.progress`` SSE events let
    frontends render in-flight tool activity (e.g. "Running grep…")
    alongside the assistant's text deltas. The adapter constructs a
    ``StreamHooks`` instance per-request and passes it to the V2
    streaming handler. Hosts that don't have tool progress to emit can
    just call ``emit_text`` and ignore the rest.
    """

    def __init__(
        self,
        *,
        emit_text: Callable[[str], Awaitable[None]],
        emit_tool_progress: Callable[[str, str, str], Awaitable[None]],
    ) -> None:
        self.emit_text = emit_text
        self.emit_tool_progress = emit_tool_progress


# StreamingChatHandlerV2 takes a StreamHooks instance instead of just
# the on_delta callback. Hosts opt in via set_streaming_handler_v2(...).
StreamingChatHandlerV2 = Callable[[str, str, StreamHooks], Awaitable[None]]


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
        # T58 — V2 streaming handler (StreamHooks-based). When set,
        # takes precedence over the V1 _streaming_handler.
        self._streaming_handler_v2: StreamingChatHandlerV2 | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        # Wave 6.A — Hermes-port (0a15dbdc4) — track in-flight chat runs
        # by run_id so POST /v1/runs/{id}/stop can cancel the underlying
        # asyncio.Task. Cleared in _handle_chat's finally block on every
        # outcome (completion / error / cancel).
        self._active_runs: dict[str, asyncio.Task[Any]] = {}
        # Hermes-doc /v1/responses chaining: ``previous_response_id`` +
        # named conversation. LRU-bounded (max 100 entries) to match
        # the Hermes spec. Each entry holds the rendered transcript so
        # follow-up calls can prepend prior turns.
        from collections import OrderedDict

        self._responses_store: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._responses_max = 100
        # name → response_id (latest in chain); allows
        # ``conversation: "my-project"`` to auto-chain.
        self._named_conversations: dict[str, str] = {}
        # T59 — Hermes-doc /v1/runs full Runs API. Each run holds:
        #   - status: pending|running|done|error|cancelled
        #   - events: list of dicts ({type, ...}) for SSE replay
        #   - task: the asyncio.Task driving execution
        #   - created_at / completed_at: monotonic timestamps
        # Bounded LRU (max 100) — older completed runs evicted; in-flight
        # runs are never evicted.
        self._runs: dict[str, dict[str, Any]] = {}
        self._runs_max = 100

    def set_handler(self, handler: ChatHandler) -> None:
        """Inject the per-request agent handler.

        The host (``opencomputer.gateway`` or a custom embed) calls this
        after registration. Without a handler set, requests return 503.
        """
        self._handler = handler

    def set_streaming_handler_v2(self, handler: StreamingChatHandlerV2) -> None:
        """Inject the V2 per-token streaming handler (T58 — Hermes-doc).

        Receives a :class:`StreamHooks` instance — both ``emit_text``
        and ``emit_tool_progress`` callbacks. Takes precedence over the
        V1 handler set via :meth:`set_streaming_handler`.
        """
        self._streaming_handler_v2 = handler

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

    # T60 — Hermes-doc /api/jobs (cron management) ──────────────────

    def _auth_check(self, request: web.Request) -> web.Response | None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != self._token:
            return web.json_response({"error": "unauthorized"}, status=401)
        return None

    def _resolve_request_profile(self, request: web.Request) -> str | None:
        """T61 — resolve the per-request profile.

        Reads the ``X-OC-Profile`` header (case-insensitive, dashes
        normalized to underscores by aiohttp). Returns ``None`` when
        absent — caller stays on the process-default profile.

        Validation: rejects path-traversal-y names. Profile must be
        ``[a-z0-9_-]+`` (Hermes profile naming convention).
        """
        import re

        raw = request.headers.get("X-OC-Profile") or request.headers.get("x-oc-profile")
        if not raw:
            return None
        if not re.match(r"^[A-Za-z0-9_-]{1,32}$", raw):
            return None  # silently ignore — never error on a missing header
        return raw

    async def _handle_jobs_list(self, request: web.Request) -> web.Response:
        deny = self._auth_check(request)
        if deny is not None:
            return deny
        from opencomputer.cron import jobs as cron_jobs

        try:
            jobs = cron_jobs.list_jobs(include_disabled=True)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"jobs": jobs})

    async def _handle_jobs_create(self, request: web.Request) -> web.Response:
        deny = self._auth_check(request)
        if deny is not None:
            return deny
        from opencomputer.cron import jobs as cron_jobs

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        schedule = payload.get("schedule")
        if not isinstance(schedule, str) or not schedule.strip():
            return web.json_response({"error": "schedule required"}, status=400)
        try:
            job = cron_jobs.create_job(
                schedule=schedule,
                name=payload.get("name"),
                prompt=payload.get("prompt"),
                skill=payload.get("skill"),
                repeat=payload.get("repeat"),
                notify=payload.get("notify"),
                plan_mode=bool(payload.get("plan_mode", True)),
                enabled_toolsets=payload.get("enabled_toolsets"),
                context_from=payload.get("context_from"),
                workdir=payload.get("workdir"),
                no_agent=bool(payload.get("no_agent", False)),
                script=payload.get("script"),
                script_timeout_seconds=payload.get("script_timeout_seconds"),
            )
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(job, status=201)

    async def _handle_jobs_get(self, request: web.Request) -> web.Response:
        deny = self._auth_check(request)
        if deny is not None:
            return deny
        from opencomputer.cron import jobs as cron_jobs

        job_id = request.match_info["job_id"]
        job = cron_jobs.get_job(job_id)
        if job is None:
            return web.json_response({"error": "job not found"}, status=404)
        return web.json_response(job)

    async def _handle_jobs_patch(self, request: web.Request) -> web.Response:
        deny = self._auth_check(request)
        if deny is not None:
            return deny
        from opencomputer.cron import jobs as cron_jobs

        job_id = request.match_info["job_id"]
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "object body required"}, status=400)
        job = cron_jobs.update_job(job_id, payload)
        if job is None:
            return web.json_response({"error": "job not found"}, status=404)
        return web.json_response(job)

    async def _handle_jobs_delete(self, request: web.Request) -> web.Response:
        deny = self._auth_check(request)
        if deny is not None:
            return deny
        from opencomputer.cron import jobs as cron_jobs

        job_id = request.match_info["job_id"]
        ok = cron_jobs.remove_job(job_id)
        if not ok:
            return web.json_response({"error": "job not found"}, status=404)
        return web.json_response({"deleted": True, "job_id": job_id})

    async def _handle_jobs_pause(self, request: web.Request) -> web.Response:
        deny = self._auth_check(request)
        if deny is not None:
            return deny
        from opencomputer.cron import jobs as cron_jobs

        job_id = request.match_info["job_id"]
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        reason = payload.get("reason") if isinstance(payload, dict) else None
        job = cron_jobs.pause_job(job_id, reason=reason)
        if job is None:
            return web.json_response({"error": "job not found"}, status=404)
        return web.json_response(job)

    async def _handle_jobs_resume(self, request: web.Request) -> web.Response:
        deny = self._auth_check(request)
        if deny is not None:
            return deny
        from opencomputer.cron import jobs as cron_jobs

        job_id = request.match_info["job_id"]
        job = cron_jobs.resume_job(job_id)
        if job is None:
            return web.json_response({"error": "job not found"}, status=404)
        return web.json_response(job)

    async def _handle_jobs_run(self, request: web.Request) -> web.Response:
        deny = self._auth_check(request)
        if deny is not None:
            return deny
        from opencomputer.cron import jobs as cron_jobs

        job_id = request.match_info["job_id"]
        job = cron_jobs.trigger_job(job_id)
        if job is None:
            return web.json_response({"error": "job not found"}, status=404)
        return web.json_response(job)

    # T59 — Hermes-doc /v1/runs full API ─────────────────────────────

    async def _handle_run_create(self, request: web.Request) -> web.Response:
        """``POST /v1/runs`` — start a background run, return ``{run_id}``.

        Body: ``{"input": "<user text>", "session_id"?: str, "model"?: str}``.
        """
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != self._token:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        user_text = payload.get("input") or payload.get("prompt") or ""
        if not isinstance(user_text, str) or not user_text.strip():
            return web.json_response({"error": "input required"}, status=400)
        if self._handler is None and self._streaming_handler is None and self._streaming_handler_v2 is None:
            return web.json_response({"error": "handler not configured"}, status=503)

        run_id = f"run-{uuid.uuid4().hex[:24]}"
        session_id = payload.get("session_id") or run_id
        loop = asyncio.get_event_loop()
        run: dict[str, Any] = {
            "id": run_id,
            "status": "pending",
            "events": [],
            "task": None,
            "queue": asyncio.Queue(),
            "created_at": loop.time(),
            "completed_at": None,
            "result": None,
            "error": None,
        }
        self._runs[run_id] = run
        self._evict_runs_if_needed()
        run["task"] = asyncio.create_task(
            self._drive_run(run_id, session_id, user_text)
        )
        return web.json_response({"run_id": run_id, "status": "pending"})

    def _evict_runs_if_needed(self) -> None:
        """LRU-evict completed runs once over cap; never evict in-flight."""
        if len(self._runs) <= self._runs_max:
            return
        completed = [
            (rid, r) for rid, r in self._runs.items()
            if r["status"] in ("done", "error", "cancelled")
        ]
        completed.sort(key=lambda kv: kv[1].get("completed_at") or 0)
        while len(self._runs) > self._runs_max and completed:
            rid, _ = completed.pop(0)
            self._runs.pop(rid, None)

    async def _drive_run(self, run_id: str, session_id: str, user_text: str) -> None:
        """Execute the agent for this run, accumulating events for SSE replay."""
        run = self._runs.get(run_id)
        if run is None:
            return
        run["status"] = "running"
        await self._enqueue_run_event(run, {"type": "run.created", "run_id": run_id})
        try:
            chunks: list[str] = []

            async def _on_text(text: str) -> None:
                if not text:
                    return
                chunks.append(text)
                await self._enqueue_run_event(run, {"type": "token", "delta": text})

            async def _on_tool_progress(name: str, status: str, detail: str = "") -> None:
                await self._enqueue_run_event(
                    run,
                    {"type": "tool.progress", "tool": name, "status": status, "detail": detail},
                )

            if self._streaming_handler_v2 is not None:
                hooks = StreamHooks(emit_text=_on_text, emit_tool_progress=_on_tool_progress)
                await self._streaming_handler_v2(session_id, user_text, hooks)
            elif self._streaming_handler is not None:
                await self._streaming_handler(session_id, user_text, _on_text)
            elif self._handler is not None:
                final = await self._handler(user_text, session_id) or ""
                chunks.append(final)
                await self._enqueue_run_event(run, {"type": "token", "delta": final})

            run["result"] = "".join(chunks)
            run["status"] = "done"
            await self._enqueue_run_event(run, {"type": "run.completed", "run_id": run_id})
        except asyncio.CancelledError:
            run["status"] = "cancelled"
            await self._enqueue_run_event(run, {"type": "run.cancelled", "run_id": run_id})
            raise
        except Exception as exc:  # noqa: BLE001
            run["status"] = "error"
            run["error"] = f"{type(exc).__name__}: {exc}"
            await self._enqueue_run_event(
                run, {"type": "run.error", "run_id": run_id, "error": run["error"]}
            )
        finally:
            run["completed_at"] = asyncio.get_event_loop().time()
            # Sentinel — wakes any waiting /events consumers so they can close.
            await run["queue"].put(None)

    async def _enqueue_run_event(self, run: dict[str, Any], event: dict[str, Any]) -> None:
        """Append to both the events log (for replay) and the live queue."""
        run["events"].append(event)
        await run["queue"].put(event)

    async def _handle_run_get(self, request: web.Request) -> web.Response:
        """``GET /v1/runs/{run_id}`` — current status snapshot."""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != self._token:
            return web.json_response({"error": "unauthorized"}, status=401)
        run_id = request.match_info["run_id"]
        run = self._runs.get(run_id)
        if run is None:
            return web.json_response({"error": "run not found"}, status=404)
        return web.json_response(
            {
                "run_id": run["id"],
                "status": run["status"],
                "result": run.get("result"),
                "error": run.get("error"),
                "event_count": len(run["events"]),
            }
        )

    async def _handle_run_events(self, request: web.Request) -> web.StreamResponse:
        """``GET /v1/runs/{run_id}/events`` — SSE stream of events.

        Replays already-buffered events first, then streams live until the
        run completes (or the connection closes).
        """
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != self._token:
            return web.json_response({"error": "unauthorized"}, status=401)
        run_id = request.match_info["run_id"]
        run = self._runs.get(run_id)
        if run is None:
            return web.json_response({"error": "run not found"}, status=404)

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        import json as _json

        # Replay buffered events.
        for ev in list(run["events"]):
            await resp.write(f"data: {_json.dumps(ev)}\n\n".encode())
        # If already terminal, close.
        if run["status"] in ("done", "error", "cancelled"):
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp
        # Live drain: pull from the queue until sentinel.
        seen = len(run["events"])
        try:
            while True:
                ev = await run["queue"].get()
                if ev is None:
                    break
                # Skip if we already replayed this event (race with replay loop).
                if seen > 0:
                    seen -= 1
                    continue
                await resp.write(f"data: {_json.dumps(ev)}\n\n".encode())
        finally:
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
        return resp

    async def _handle_run_stop(self, request: web.Request) -> web.Response:
        """``POST /v1/runs/{run_id}/stop`` — cancel an in-flight chat run.

        Wave 6.A — Hermes-port (0a15dbdc4). Returns 200 if cancelled, 404
        if the run_id is unknown (already completed or never existed).
        """
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != self._token:
            return web.json_response({"error": "unauthorized"}, status=401)
        run_id = request.match_info.get("run_id", "")
        # Try both stores: legacy /v1/chat in-flight runs AND T59 /v1/runs.
        task = self._active_runs.get(run_id)
        if task is not None:
            task.cancel()
            return web.json_response({"run_id": run_id, "stopped": True})
        run = self._runs.get(run_id)
        if run is not None and run.get("task") is not None:
            run["task"].cancel()
            return web.json_response({"run_id": run_id, "stopped": True})
        return web.json_response(
            {"error": "unknown run_id (already finished or never existed)"},
            status=404,
        )

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
        if (
            stream
            and self._streaming_handler is None
            and self._streaming_handler_v2 is None
            and self._handler is None
        ):
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
            #
            # T58 — V2 path additionally exposes ``emit_tool_progress``,
            # which writes ``event: hermes.tool.progress`` SSE events
            # for frontends to render in-flight tool activity.
            if self._streaming_handler_v2 is not None or self._streaming_handler is not None:
                async def _on_delta(text: str) -> None:
                    if not text:
                        return
                    await resp.write(
                        f"data: {streaming_delta_chunk(chunk_id, model, text)}\n\n".encode()
                    )

                async def _on_tool_progress(name: str, status: str, detail: str = "") -> None:
                    """Emit a Hermes-doc `hermes.tool.progress` SSE event.

                    SSE wire format: an explicit ``event: <name>`` line
                    plus a ``data: <json>`` line. Frontends listening
                    for ``hermes.tool.progress`` see {tool, status, detail}.
                    """
                    import json as _json

                    payload = _json.dumps(
                        {"tool": name, "status": status, "detail": detail or ""}
                    )
                    await resp.write(
                        f"event: hermes.tool.progress\ndata: {payload}\n\n".encode()
                    )

                try:
                    if self._streaming_handler_v2 is not None:
                        hooks = StreamHooks(
                            emit_text=_on_delta,
                            emit_tool_progress=_on_tool_progress,
                        )
                        await self._streaming_handler_v2(session_id, user_text, hooks)
                    else:
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
        # T61 — middleware sets `_CURRENT_PROFILE` from `X-OC-Profile`
        # for the duration of every request. Handlers (and any tasks
        # they spawn via `asyncio.create_task`) inherit the value via
        # standard contextvars propagation.
        @web.middleware
        async def _profile_middleware(request, handler):
            token = _CURRENT_PROFILE.set(self._resolve_request_profile(request))
            try:
                return await handler(request)
            finally:
                _CURRENT_PROFILE.reset(token)

        app = web.Application(
            client_max_size=self.max_message_length,
            middlewares=[_profile_middleware],
        )
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
        # T59 — Hermes-doc Runs API. Create / status / SSE-events.
        app.router.add_post("/v1/runs", self._handle_run_create)
        app.router.add_get("/v1/runs/{run_id}", self._handle_run_get)
        app.router.add_get("/v1/runs/{run_id}/events", self._handle_run_events)
        # T60 — Hermes-doc Jobs API (cron management). All routes auth-required.
        app.router.add_get("/api/jobs", self._handle_jobs_list)
        app.router.add_post("/api/jobs", self._handle_jobs_create)
        app.router.add_get("/api/jobs/{job_id}", self._handle_jobs_get)
        app.router.add_patch("/api/jobs/{job_id}", self._handle_jobs_patch)
        app.router.add_delete("/api/jobs/{job_id}", self._handle_jobs_delete)
        app.router.add_post("/api/jobs/{job_id}/pause", self._handle_jobs_pause)
        app.router.add_post("/api/jobs/{job_id}/resume", self._handle_jobs_resume)
        app.router.add_post("/api/jobs/{job_id}/run", self._handle_jobs_run)
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
        # T61 — X-OC-Profile header overrides process-default for this
        # response only. Useful for multi-tenant routers that share one
        # api-server process across profiles.
        profile = self._resolve_request_profile(request) or os.environ.get(
            "OPENCOMPUTER_PROFILE", "default"
        )
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
                "previous_response_id": True,
                "runs_api": True,
                "jobs_api": True,
                # T58 — Hermes-doc vendor extension: `event: hermes.tool.progress`
                # SSE events on streaming /v1/chat/completions and /v1/responses.
                "tool_progress": True,
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
                "profile": (
                    self._resolve_request_profile(request)
                    or os.environ.get("OPENCOMPUTER_PROFILE", "default")
                ),
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

    def _store_response(
        self,
        *,
        response_id: str,
        user_text: str,
        agent_text: str,
        previous_response_id: str | None,
        conversation: str | None,
    ) -> None:
        """Append a response to the LRU store (Hermes-doc parity).

        Entry shape: ``{user, agent, previous_response_id, conversation}``.
        LRU-evicts when length exceeds ``_responses_max``.
        """
        self._responses_store[response_id] = {
            "user": user_text,
            "agent": agent_text,
            "previous_response_id": previous_response_id,
            "conversation": conversation,
        }
        # LRU: move to end (newest); evict oldest if over cap.
        self._responses_store.move_to_end(response_id)
        while len(self._responses_store) > self._responses_max:
            self._responses_store.popitem(last=False)
        if conversation:
            self._named_conversations[conversation] = response_id

    def _build_chained_input(
        self,
        *,
        new_input: str,
        previous_response_id: str | None,
        conversation: str | None,
    ) -> tuple[str, str | None]:
        """Build the prompt by prepending prior turns from the chain.

        Returns ``(rendered_input, resolved_previous_id)``. The
        resolved id is the actual chain head used (may differ from the
        client-supplied value when ``conversation`` is set without an
        explicit previous_response_id).
        """
        chain_id = previous_response_id
        if chain_id is None and conversation:
            chain_id = self._named_conversations.get(conversation)
        if chain_id is None or chain_id not in self._responses_store:
            return new_input, None
        # Walk back through the chain, accumulating turns.
        turns: list[tuple[str, str]] = []
        cursor: str | None = chain_id
        seen: set[str] = set()
        while cursor and cursor in self._responses_store and cursor not in seen:
            seen.add(cursor)
            entry = self._responses_store[cursor]
            turns.append((entry["user"], entry["agent"]))
            cursor = entry.get("previous_response_id")
        turns.reverse()  # oldest → newest
        rendered_parts: list[str] = []
        for user, agent in turns:
            rendered_parts.append(f"[user] {user}\n[assistant] {agent}")
        rendered_parts.append(f"[user] {new_input}")
        return "\n\n".join(rendered_parts), chain_id

    async def _handle_responses_stub(self, request: web.Request) -> web.Response:
        """Hermes-doc /v1/responses with previous_response_id chaining.

        Gated on ``API_SERVER_API_TYPE=responses``. Supports:
        - ``input``: str (Responses-API native) OR ``messages`` (chat-completions form)
        - ``previous_response_id``: chain to a prior response by id
        - ``conversation``: named-conversation auto-chain
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

        previous_response_id = payload.get("previous_response_id")
        conversation = payload.get("conversation")
        prompt_text, resolved_prev = self._build_chained_input(
            new_input=user_text,
            previous_response_id=(
                previous_response_id if isinstance(previous_response_id, str) else None
            ),
            conversation=(conversation if isinstance(conversation, str) else None),
        )

        session_id = payload.get("session_id", "")
        stream = bool(payload.get("stream", False))

        # T58 — SSE streaming on /v1/responses. Emits Responses-API
        # native lifecycle events plus `event: hermes.tool.progress`
        # when a V2 streaming handler is registered. Falls back to
        # collecting text from the V2 handler, otherwise the legacy
        # synchronous _handler. Same wire shape as the non-streaming
        # path's `oc_response_to_responses_api` envelope, just split
        # across SSE deltas.
        if stream and (
            self._streaming_handler_v2 is not None or self._handler is not None
        ):
            return await self._stream_responses(
                request=request,
                prompt_text=prompt_text,
                user_text=user_text,
                session_id=session_id,
                payload=payload,
                resolved_prev=resolved_prev,
                conversation=conversation if isinstance(conversation, str) else None,
            )

        if self._handler is None:
            return web.json_response(
                {"error": {"message": "handler not configured"}}, status=503
            )

        try:
            agent_text = await self._handler(prompt_text, session_id) or ""
        except Exception as exc:  # noqa: BLE001
            logger.exception("api-server: responses-stub handler raised")
            return web.json_response(
                {"error": {"message": f"agent error: {exc}"}}, status=500
            )

        model = payload.get("model") or "opencomputer"
        envelope = oc_response_to_responses_api(
            agent_text,
            model=model,
            input_tokens=len(prompt_text.split()),
            output_tokens=len(agent_text.split()),
        )
        new_response_id = envelope["id"]
        self._store_response(
            response_id=new_response_id,
            user_text=user_text,
            agent_text=agent_text,
            previous_response_id=resolved_prev,
            conversation=(conversation if isinstance(conversation, str) else None),
        )
        # Echo previous_response_id when chained for client introspection.
        if resolved_prev is not None:
            envelope["previous_response_id"] = resolved_prev
        if isinstance(conversation, str):
            envelope["conversation"] = conversation
        return web.json_response(envelope)

    async def _stream_responses(
        self,
        *,
        request: web.Request,
        prompt_text: str,
        user_text: str,
        session_id: str,
        payload: dict[str, Any],
        resolved_prev: str | None,
        conversation: str | None,
    ) -> web.StreamResponse:
        """SSE driver for /v1/responses with hermes.tool.progress side-channel.

        Emits, in order: ``response.created``, N × ``response.output_text.delta``
        (interleaved with vendor ``hermes.tool.progress``), and a final
        ``response.completed`` carrying the full envelope. Mirrors the
        non-streaming path's storage + previous_response_id echo.
        """
        import json as _json
        import uuid as _uuid

        model = payload.get("model") or "opencomputer"
        response_id = f"resp-{_uuid.uuid4().hex[:24]}"

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)

        async def _emit(event: str, data: dict[str, Any]) -> None:
            await resp.write(
                f"event: {event}\ndata: {_json.dumps(data)}\n\n".encode()
            )

        await _emit(
            "response.created",
            {"id": response_id, "model": model, "object": "response"},
        )

        text_buf: list[str] = []

        async def _on_text(chunk: str) -> None:
            if not chunk:
                return
            text_buf.append(chunk)
            await _emit(
                "response.output_text.delta",
                {"id": response_id, "delta": chunk},
            )

        async def _on_tool_progress(name: str, status: str, detail: str = "") -> None:
            await _emit(
                "hermes.tool.progress",
                {"tool": name, "status": status, "detail": detail or ""},
            )

        try:
            if self._streaming_handler_v2 is not None:
                hooks = StreamHooks(
                    emit_text=_on_text, emit_tool_progress=_on_tool_progress
                )
                await self._streaming_handler_v2(session_id, user_text, hooks)
            else:
                # Fall back: non-streaming handler — emit its output as
                # one big delta so the SSE shape stays uniform.
                agent_text = await self._handler(prompt_text, session_id) or ""
                await _on_text(agent_text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("api-server: responses-stub streaming raised")
            await _emit("response.error", {"message": str(exc)})
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        agent_text = "".join(text_buf)
        envelope = oc_response_to_responses_api(
            agent_text,
            model=model,
            input_tokens=len(prompt_text.split()),
            output_tokens=len(agent_text.split()),
        )
        envelope["id"] = response_id
        if resolved_prev is not None:
            envelope["previous_response_id"] = resolved_prev
        if conversation is not None:
            envelope["conversation"] = conversation
        self._store_response(
            response_id=response_id,
            user_text=user_text,
            agent_text=agent_text,
            previous_response_id=resolved_prev,
            conversation=conversation,
        )
        await _emit("response.completed", envelope)
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

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
