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
