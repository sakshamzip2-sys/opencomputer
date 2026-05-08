"""OpenAI Chat Completions API ↔ OpenComputer message-format converters.

Maps OpenAI's POST /v1/chat/completions request/response shape to OC's
internal handler call and back. Keeps the api-server adapter free of
format-conversion details.
"""

from __future__ import annotations

import json
import time
import uuid


def openai_to_oc_messages(openai_messages: list[dict]) -> list[dict]:
    """Convert OpenAI messages to OC's [{"role": ..., "content": ...}, ...].

    OC's handler expects role + content as plain dicts (not Message
    dataclass instances). For multimodal `content: [...]` entries we
    extract just the text parts — OC's request-response handler is
    text-only today.
    """
    out = []
    for m in openai_messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
            content = "\n".join(text_parts)
        out.append({"role": role, "content": content})
    return out


def oc_response_to_openai(
    text: str,
    *,
    model: str = "opencomputer",
    input_tokens: int = 0,
    output_tokens: int = 0,
    finish_reason: str = "stop",
) -> dict:
    """Wrap an OC handler response in OpenAI's chat.completion shape."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def streaming_delta_chunk(chunk_id: str, model: str, delta_text: str) -> str:
    """Format a single streaming delta as OpenAI SSE JSON (no `data: ` prefix)."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": delta_text},
                "finish_reason": None,
            }
        ],
    }
    return json.dumps(payload)


def streaming_final_chunk(chunk_id: str, model: str, finish_reason: str = "stop") -> str:
    """Final SSE chunk with finish_reason (followed by `data: [DONE]`)."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return json.dumps(payload)


def list_models(profile_name: str = "default", env_override: str | None = None) -> dict:
    """Hermes parity (2026-05-08): advertise the active profile name as the model id.

    Multi-profile setups run separate api-server instances per profile;
    each advertises its profile name so Open WebUI sees them as distinct
    "models". Override the advertised name via ``API_SERVER_MODEL_NAME``
    env var (passed in as ``env_override`` for testability).

    Args:
        profile_name: The active profile (e.g., "default", "alice", "coding").
        env_override: When non-empty, used as the model id (mirrors the
            Hermes ``API_SERVER_MODEL_NAME`` env override).

    Returns:
        OpenAI-compatible /v1/models response shape.
    """
    if env_override and env_override.strip():
        model_id = env_override.strip()
    elif profile_name and profile_name.strip():
        model_id = profile_name.strip()
    else:
        model_id = "opencomputer"
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "opencomputer",
            }
        ],
    }


def oc_response_to_responses_api(
    text: str,
    *,
    model: str = "opencomputer",
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> dict:
    """Hermes parity (2026-05-08): wrap a chat response in the OpenAI
    Responses-API envelope.

    Stub implementation — emits the simple ``response`` object shape that
    Open WebUI's capability probe expects. True streaming SSE event
    semantics (``function_call``, ``function_call_output``) are deferred
    to demand. The route is opt-in via ``API_SERVER_API_TYPE=responses``.
    """
    return {
        "id": f"resp-{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created": int(time.time()),
        "model": model,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": text}
                ],
            }
        ],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
