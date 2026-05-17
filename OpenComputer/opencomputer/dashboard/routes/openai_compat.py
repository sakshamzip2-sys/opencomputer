"""OpenAI-compatible chat completions surface for OC's dashboard.

Hermes Workspace (and any other OpenAI-API-compatible client) needs three
endpoints to operate against OC as its backend:

* ``GET  /v1/health``           — liveness probe (public)
* ``GET  /v1/models``           — model catalogue in OpenAI list shape (public)
* ``POST /v1/chat/completions`` — chat completion, streaming + non-streaming,
                                  Bearer-token gated against the dashboard's
                                  ephemeral session token

The routes deliberately use the ``/v1`` prefix (not ``/api/v1``) so the URL
matches the OpenAI base-URL convention out of the box — any OpenAI SDK
configured with ``OPENAI_BASE_URL=http://127.0.0.1:9119/v1`` works against
OC without further translation.

Conversation handling is stateless per request. The ``messages`` array is
treated as the prior transcript (system/user/assistant turns). The LAST user
message drives a fresh :class:`AgentLoop.run_conversation` invocation; prior
turns are passed via ``initial_messages``. Tool calls happen inside the
loop but are not surfaced as OpenAI ``tool_calls`` deltas in v1 — only the
agent's terminal text response is streamed back. This matches what
hermes-workspace expects from a vanilla OpenAI-compatible backend.

Failure model: every error path returns the OpenAI error envelope
``{"error": {"message": str, "type": str, "code": str | null}}`` with an
HTTP status that mirrors OpenAI's own conventions (400/401/404/413/500/503).
SSE streams that fail mid-flight emit one final ``data: {error: ...}`` chunk
followed by ``data: [DONE]`` so the client can render the failure inline
instead of seeing a silent socket close.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from opencomputer.dashboard._auth import require_session_token

logger = logging.getLogger("opencomputer.dashboard.openai_compat")

router = APIRouter(prefix="/v1", tags=["openai-compat"])

# ---------------------------------------------------------------------------
# Limits / sentinels
# ---------------------------------------------------------------------------

#: Maximum body size accepted on chat completions. Anything bigger is almost
#: certainly a runaway client or a probe — refuse early before allocating
#: the parse buffer.
MAX_BODY_BYTES = 4 * 1024 * 1024  # 4 MiB

#: Wall-clock cap on a single completion. Hermes-workspace's UI already
#: surfaces a "this took a while" indicator; the cap exists to prevent a
#: stuck provider from holding the SSE socket open indefinitely.
COMPLETION_TIMEOUT_SECONDS = 10 * 60  # 10 minutes


# ---------------------------------------------------------------------------
# Request / response models (OpenAI shape, pydantic-validated)
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    """One entry of the OpenAI ``messages`` array."""

    role: Literal["system", "user", "assistant", "tool", "developer"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    #: ``tool_call_id`` is OpenAI's response-side correlation id; we accept
    #: it for forward compatibility but do not consume it in v1.
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI ``POST /v1/chat/completions`` request body."""

    model: str
    messages: list[ChatMessage] = Field(..., min_length=1)
    stream: bool = False
    #: Standard OpenAI sampling knobs. Accepted for compatibility; whether
    #: they are honoured depends on the resolved provider. We forward them
    #: to the loop's config when the provider supports them.
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    #: Custom OC extension: pin the call to an existing OC session id. When
    #: present and resolvable, the loop reuses that session's persisted
    #: history instead of treating the request as one-shot.
    oc_session_id: str | None = None


# ---------------------------------------------------------------------------
# Error envelope helpers
# ---------------------------------------------------------------------------


def _error_envelope(
    message: str,
    *,
    error_type: str,
    code: str | None = None,
) -> dict[str, Any]:
    """Build OpenAI-style error envelope."""
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": code,
        }
    }


def _error_response(
    message: str,
    *,
    status_code: int,
    error_type: str,
    code: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_error_envelope(message, error_type=error_type, code=code),
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict[str, Any]:
    """Public liveness probe. Mirrors hermes-workspace's expected shape."""
    try:
        from opencomputer import __version__ as _version
    except Exception:  # noqa: BLE001 — never break the probe
        _version = "unknown"
    return {"status": "ok", "version": _version}


# ---------------------------------------------------------------------------
# Models list
# ---------------------------------------------------------------------------


@router.get("/models")
async def list_models() -> dict[str, Any]:
    """Return all (provider, model) pairs flattened into the OpenAI list shape.

    Mirrors ``GET https://api.openai.com/v1/models``::

        {"object": "list", "data": [
            {"id": "...", "object": "model", "owned_by": "...", "created": int},
            ...
        ]}

    Public — matches OpenAI's behaviour where the model list does not
    require auth.
    """
    try:
        from opencomputer import cli_model_picker

        grouped = cli_model_picker._grouped_models()  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        logger.warning("openai_compat: models registry unavailable: %s", exc)
        return _error_envelope(
            f"models registry unavailable: {exc}",
            error_type="server_error",
            code="models_unavailable",
        )

    now = int(time.time())
    data: list[dict[str, Any]] = []
    seen: set[str] = set()
    for provider, models in sorted(grouped.items()):
        for model in sorted(models):
            # OpenAI's list-models response uses bare model ids ("gpt-4o").
            # Some OC providers expose models with provider-qualified ids
            # already; we don't strip them — the workspace renders whatever
            # comes back. Dedup on the final id so we don't double-list
            # when two providers happen to expose the same model name.
            if model in seen:
                continue
            seen.add(model)
            data.append(
                {
                    "id": model,
                    "object": "model",
                    "created": now,
                    "owned_by": provider or "opencomputer",
                }
            )
    return {"object": "list", "data": data}


# ---------------------------------------------------------------------------
# Chat completions
# ---------------------------------------------------------------------------


def _last_user_message(messages: list[ChatMessage]) -> ChatMessage | None:
    for msg in reversed(messages):
        if msg.role == "user":
            return msg
    return None


def _content_as_text(content: str | list[dict[str, Any]] | None) -> str:
    """OpenAI accepts string content OR a content-parts array (multimodal).

    We collapse to plain text for the AgentLoop input. Non-text parts are
    surfaced as a ``[non-text-content: <type>]`` marker so the loop sees
    *something* rather than silently losing the part.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type", "")
        if ptype == "text":
            text = part.get("text", "")
            if isinstance(text, str):
                parts.append(text)
        else:
            parts.append(f"[non-text-content: {ptype}]")
    return "\n".join(parts)


def _build_initial_history(
    messages: list[ChatMessage],
) -> tuple[str | None, list[Any]]:
    """Split the request's ``messages`` array into (system_prompt, history).

    Returns:
        system_prompt: concatenated system messages (None if none present)
        history: list of :class:`plugin_sdk.core.Message` for everything
            BEFORE the final user turn. The final user turn is consumed by
            the caller and passed as ``user_message`` separately.
    """
    from plugin_sdk.core import Message

    if not messages:
        return None, []

    # Find the index of the final user message; everything after it is
    # noise (an empty trailing assistant slot, etc.) and gets dropped.
    last_user_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].role == "user":
            last_user_idx = idx
            break
    if last_user_idx < 0:
        return None, []

    system_parts: list[str] = []
    history: list[Any] = []
    for idx, msg in enumerate(messages[:last_user_idx]):
        text = _content_as_text(msg.content)
        if not text and msg.role != "tool":
            continue
        if msg.role in ("system", "developer"):
            system_parts.append(text)
            continue
        # plugin_sdk.core.Message accepts role in {"user","assistant","system","tool"}
        # — map "developer" to "system" above; tool messages without
        # tool_use_id pairing are dropped to avoid Anthropic 400s.
        if msg.role == "tool":
            continue
        history.append(Message(role=msg.role, content=text))

    system_prompt = "\n\n".join(p for p in system_parts if p) or None
    return system_prompt, history


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def _chunk_payload(
    *,
    completion_id: str,
    model: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ],
    }


def _sse_line(payload: Any) -> bytes:
    """Encode one OpenAI-format SSE chunk."""
    return f"data: {json.dumps(payload)}\n\n".encode()


def _sse_done() -> bytes:
    return b"data: [DONE]\n\n"


async def _run_agent_completion(
    *,
    user_message: str,
    history: list[Any],
    system_prompt: str | None,
    model: str,
    oc_session_id: str | None,
    stream_callback: Any | None = None,
) -> str:
    """Drive AgentLoop once and return the final assistant text.

    ``stream_callback`` matches AgentLoop's contract: a SYNC callable
    invoked with each text-delta string (incremental, not cumulative).
    The streaming path wraps a sync shim around an asyncio.Queue so the
    SSE pump can read deltas from a coroutine context.
    """
    from opencomputer.agent.config import _home as profile_home_fn
    from opencomputer.gateway.agent_loop_factory import (
        build_agent_loop_for_profile,
    )

    profile_home = profile_home_fn()
    profile_id = profile_home.name
    loop = build_agent_loop_for_profile(profile_id, profile_home)

    # Register OC's built-in injection providers for the webui surface
    # (serves both ``oc webui`` and ``oc workspace``). One call wires
    # ThinkingInjector, PathGlobRulesProvider, HandoffInjectionProvider
    # (ContextVar-aware resolver) and LifeEventInjectionProvider. This
    # route runs per request; every registration is idempotent so
    # per-request (re)registration is fine + fail-soft.
    from opencomputer.agent.injection_registration import (
        register_default_injection_providers,
    )
    register_default_injection_providers("webui")

    # Per-request model override: a workspace user may pick a model from
    # the dropdown that differs from the profile's default. We respect
    # that override for the duration of this call only — never persist it.
    if model and getattr(loop.config.model, "model", "") != model:
        try:
            loop.config.model.model = model
        except Exception:  # noqa: BLE001 — frozen-dataclass edge cases
            logger.debug(
                "openai_compat: cannot override model on loop config; "
                "continuing with profile default %s",
                getattr(loop.config.model, "model", "?"),
            )

    result = await loop.run_conversation(
        user_message=user_message,
        session_id=oc_session_id,
        system_override=system_prompt,
        initial_messages=history or None,
        stream_callback=stream_callback,
    )
    # ConversationResult.final_message is a plugin_sdk.core.Message with
    # ``content: str``. Fall back to walking ``messages[-1]`` if a future
    # refactor moves the terminal reply elsewhere; never crash on the
    # response path.
    final_msg = getattr(result, "final_message", None)
    if final_msg is not None and getattr(final_msg, "content", None) is not None:
        return str(final_msg.content)
    messages = getattr(result, "messages", None) or []
    if messages:
        tail = messages[-1]
        return str(getattr(tail, "content", "") or "")
    return ""


async def _stream_completion(
    *,
    user_message: str,
    history: list[Any],
    system_prompt: str | None,
    model: str,
    oc_session_id: str | None,
    request: Request,
) -> AsyncIterator[bytes]:
    """Yield OpenAI-format SSE chunks for a streaming chat completion."""
    completion_id = _completion_id()
    created = int(time.time())

    # Opening role chunk — matches OpenAI's first SSE frame on stream=true.
    yield _sse_line(
        _chunk_payload(
            completion_id=completion_id,
            model=model,
            created=created,
            delta={"role": "assistant", "content": ""},
        )
    )

    # Bridge AgentLoop's text-callback to SSE. AgentLoop's contract
    # (loop.py:4734) is: ``stream_callback(event.text)`` called once per
    # ``text_delta`` event with the incremental delta string — NOT
    # cumulative. We accumulate locally only to surface a trailing tail in
    # case the loop emits a non-streaming final response after the stream
    # closes. The callback itself is SYNC (the loop calls it without
    # await), so we use ``asyncio.Queue.put_nowait`` and never block the
    # loop's iteration.
    pump: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2048)
    received_text: list[str] = []
    backpressure_drops = 0

    def _on_chunk(text: str) -> None:
        nonlocal backpressure_drops
        if not isinstance(text, str) or not text:
            return
        received_text.append(text)
        chunk = _chunk_payload(
            completion_id=completion_id,
            model=model,
            created=created,
            delta={"content": text},
        )
        try:
            pump.put_nowait(_sse_line(chunk))
        except asyncio.QueueFull:
            # Pump is full — the client is reading slower than the model
            # is producing. We can't block the model thread; drop the
            # delta but increment a counter so a tail "[delta-overflow]"
            # marker can be appended once the queue drains.
            backpressure_drops += 1
            logger.warning(
                "openai_compat: SSE pump full; dropping delta (drops=%d)",
                backpressure_drops,
            )

    async def _runner() -> str | BaseException:
        try:
            return await asyncio.wait_for(
                _run_agent_completion(
                    user_message=user_message,
                    history=history,
                    system_prompt=system_prompt,
                    model=model,
                    oc_session_id=oc_session_id,
                    stream_callback=_on_chunk,
                ),
                timeout=COMPLETION_TIMEOUT_SECONDS,
            )
        except BaseException as exc:  # noqa: BLE001 — surface every failure
            return exc
        finally:
            await pump.put(None)  # sentinel: stream is done

    task = asyncio.create_task(_runner())

    try:
        while True:
            if await request.is_disconnected():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                return
            try:
                item = await asyncio.wait_for(pump.get(), timeout=15.0)
            except TimeoutError:
                # Keepalive comment line — keeps proxies from idling out.
                yield b": keepalive\n\n"
                continue
            if item is None:
                break
            yield item

        outcome = await task
        if isinstance(outcome, BaseException):
            # Surface the failure in-band so the client renders an error
            # instead of seeing a silent socket close.
            err_chunk = _chunk_payload(
                completion_id=completion_id,
                model=model,
                created=created,
                delta={},
                finish_reason="stop",
            )
            err_chunk["error"] = {
                "message": str(outcome) or outcome.__class__.__name__,
                "type": "server_error",
                "code": outcome.__class__.__name__,
            }
            yield _sse_line(err_chunk)
            logger.error(
                "openai_compat: streaming completion failed: %s",
                outcome,
                exc_info=isinstance(outcome, Exception),
            )
        else:
            final_text = outcome
            streamed_text = "".join(received_text)
            # Emit any trailing portion of the final text that wasn't
            # delivered via stream_callback. Happens when the provider
            # finalises the message after the streaming loop has already
            # closed (e.g. Anthropic's text-block-stop carrying the
            # consolidated text), or when the agent's terminal reply is
            # produced by a non-streaming sub-call (compaction, recovery).
            tail = ""
            if final_text:
                if streamed_text and final_text.startswith(streamed_text):
                    tail = final_text[len(streamed_text):]
                elif not streamed_text or final_text != streamed_text:
                    tail = final_text
            if tail:
                yield _sse_line(
                    _chunk_payload(
                        completion_id=completion_id,
                        model=model,
                        created=created,
                        delta={"content": tail},
                    )
                )
            if backpressure_drops:
                yield _sse_line(
                    _chunk_payload(
                        completion_id=completion_id,
                        model=model,
                        created=created,
                        delta={
                            "content": (
                                f"\n[note: {backpressure_drops} stream "
                                "chunk(s) dropped due to client backpressure]"
                            )
                        },
                    )
                )
            yield _sse_line(
                _chunk_payload(
                    completion_id=completion_id,
                    model=model,
                    created=created,
                    delta={},
                    finish_reason="stop",
                )
            )
    finally:
        yield _sse_done()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    _: None = Depends(require_session_token),
) -> Any:
    """OpenAI-compatible chat completion. Streaming + non-streaming."""
    # Body-size guard before parse — refuse runaway clients.
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        # Starlette's status module renamed 413 between versions; resolve
        # at runtime so we don't break on either side of the rename.
        too_large = getattr(
            status,
            "HTTP_413_CONTENT_TOO_LARGE",
            getattr(status, "HTTP_413_REQUEST_ENTITY_TOO_LARGE", 413),
        )
        return _error_response(
            f"request body exceeds {MAX_BODY_BYTES} bytes",
            status_code=too_large,
            error_type="invalid_request_error",
            code="payload_too_large",
        )

    if not raw:
        return _error_response(
            "request body is empty",
            status_code=status.HTTP_400_BAD_REQUEST,
            error_type="invalid_request_error",
            code="empty_body",
        )

    try:
        body_obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _error_response(
            f"invalid JSON: {exc}",
            status_code=status.HTTP_400_BAD_REQUEST,
            error_type="invalid_request_error",
            code="malformed_json",
        )
    if not isinstance(body_obj, dict):
        return _error_response(
            "request body must be a JSON object",
            status_code=status.HTTP_400_BAD_REQUEST,
            error_type="invalid_request_error",
            code="bad_body_shape",
        )

    try:
        body = ChatCompletionRequest.model_validate(body_obj)
    except ValidationError as exc:
        # Pydantic's default message is informative enough; surface as 400.
        return _error_response(
            f"request validation failed: {exc.errors()[0].get('msg', 'invalid request')}",
            status_code=status.HTTP_400_BAD_REQUEST,
            error_type="invalid_request_error",
            code="validation_error",
        )

    last_user = _last_user_message(body.messages)
    if last_user is None:
        return _error_response(
            "messages must contain at least one user message",
            status_code=status.HTTP_400_BAD_REQUEST,
            error_type="invalid_request_error",
            code="no_user_message",
        )

    user_text = _content_as_text(last_user.content)
    if not user_text.strip():
        return _error_response(
            "the final user message is empty",
            status_code=status.HTTP_400_BAD_REQUEST,
            error_type="invalid_request_error",
            code="empty_user_message",
        )

    # Validate the model exists — workspace's UI lets users type arbitrary
    # ids, and a downstream provider would otherwise burn a request before
    # discovering the typo.
    try:
        from opencomputer import cli_model_picker

        grouped = cli_model_picker._grouped_models()  # noqa: SLF001
        known: set[str] = {m for models in grouped.values() for m in models}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "openai_compat: model registry unavailable during /chat/completions: %s",
            exc,
        )
        known = set()
    if known and body.model not in known:
        # Best-effort soft validation — a registry that lists nothing means
        # we have no signal to reject, so skip the check entirely. Only
        # 404 when the registry returned a non-empty list AND the model
        # is not in it.
        return _error_response(
            f"model {body.model!r} not found",
            status_code=status.HTTP_404_NOT_FOUND,
            error_type="invalid_request_error",
            code="model_not_found",
        )

    system_prompt, history = _build_initial_history(body.messages)

    logger.info(
        "openai_compat: /v1/chat/completions model=%s stream=%s msgs=%d session=%s",
        body.model,
        body.stream,
        len(body.messages),
        body.oc_session_id or "-",
    )

    if body.stream:
        gen = _stream_completion(
            user_message=user_text,
            history=history,
            system_prompt=system_prompt,
            model=body.model,
            oc_session_id=body.oc_session_id,
            request=request,
        )
        return StreamingResponse(
            gen,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",  # disable nginx/proxy buffering
                "Connection": "keep-alive",
            },
        )

    # Non-streaming path — wait for the final text, return one JSON.
    completion_id = _completion_id()
    created = int(time.time())
    try:
        final_text = await asyncio.wait_for(
            _run_agent_completion(
                user_message=user_text,
                history=history,
                system_prompt=system_prompt,
                model=body.model,
                oc_session_id=body.oc_session_id,
            ),
            timeout=COMPLETION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return _error_response(
            f"completion exceeded {COMPLETION_TIMEOUT_SECONDS}s wall-clock cap",
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            error_type="server_error",
            code="completion_timeout",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("openai_compat: non-streaming completion failed")
        return _error_response(
            f"{exc}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_type="server_error",
            code=exc.__class__.__name__,
        )

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": body.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": final_text},
                "finish_reason": "stop",
            }
        ],
        # Token counts are not tracked here in v1 — the AgentLoop already
        # records usage into the ``llm_calls`` SessionDB table, where it is
        # surfaced via ``/api/v1/analytics/usage``. Returning zeros keeps
        # OpenAI-SDK callers from crashing on a missing field.
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


__all__ = ["router"]
