"""Gemini Cloud Code Assist HTTP transport.

Talks to ``https://cloudcode-pa.googleapis.com/v1internal:*`` using the
JSON-RPC-shaped wire format Google's gemini-cli uses. Supports:

  - generateContent (non-streaming)
  - streamGenerateContent?alt=sse (server-sent events)
  - Tool calls (functionCall) + tool results (functionResponse)
  - System instructions extracted from system-role messages
  - JSON-Schema sanitization (Gemini rejects $schema / additionalProperties)

Wire shape (outer envelope):

    {
      "project": "<gcp-project-id>",
      "model":   "<model-id>",
      "user_prompt_id": "<uuid>",
      "request": {<inner gemini request>}
    }

Auth: Authorization: Bearer <google-oauth-access-token>.

Project id is resolved by ``opencomputer.auth.google_code_assist`` at
provider construction time (or first call) so the transport itself is
auth-stateless — it just calls the provider callbacks for fresh tokens.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx

from plugin_sdk.core import Message, ToolCall
from plugin_sdk.provider_contract import ProviderResponse, StreamEvent, Usage
from plugin_sdk.tool_contract import ToolSchema

ENDPOINT = "https://cloudcode-pa.googleapis.com"
GENERATE_PATH = "/v1internal:generateContent"
STREAM_PATH = "/v1internal:streamGenerateContent?alt=sse"

REQUEST_TIMEOUT_SECONDS = 120.0  # generateContent can take a while

# Map OC roles -> Gemini roles
_ROLE_MAP: dict[str, str] = {
    "user": "user",
    "assistant": "model",
}


# =============================================================================
# Schema sanitization
# =============================================================================

# Keys allowed in Gemini function parameters. Anything else gets stripped.
_ALLOWED_SCHEMA_KEYS = frozenset({
    "type",
    "format",
    "title",
    "description",
    "nullable",
    "enum",
    "properties",
    "required",
    "items",
    "anyOf",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "pattern",
})


def _sanitize_schema(schema: Any) -> Any:
    """Recursively strip JSON-Schema-only keys Gemini rejects."""
    if isinstance(schema, dict):
        cleaned: dict[str, Any] = {}
        for key, value in schema.items():
            if key not in _ALLOWED_SCHEMA_KEYS:
                continue
            cleaned[key] = _sanitize_schema(value)
        # Gemini requires enum values to be strings if type is string;
        # for non-string types, drop enum if it contains non-strings.
        if "enum" in cleaned and "type" in cleaned:
            if cleaned["type"] in {"integer", "number", "boolean"}:
                if not all(isinstance(v, str) for v in cleaned["enum"]):
                    cleaned.pop("enum", None)
        return cleaned
    if isinstance(schema, list):
        return [_sanitize_schema(v) for v in schema]
    return schema


# =============================================================================
# Message → Gemini contents
# =============================================================================

def build_gemini_contents(
    messages: list[Message],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """OC Messages → (Gemini contents[], optional systemInstruction)."""
    system_pieces: list[str] = []
    contents: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "system":
            if msg.content:
                system_pieces.append(msg.content)
            continue

        if msg.role == "tool":
            # Tool result wraps to a user-role functionResponse turn
            try:
                parsed = json.loads(msg.content) if msg.content.startswith(("{", "[")) else None
            except json.JSONDecodeError:
                parsed = None
            response = parsed if isinstance(parsed, dict) else {"output": msg.content}
            contents.append({
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "name": msg.name or msg.tool_call_id or "tool",
                        "response": response,
                    },
                }],
            })
            continue

        gemini_role = _ROLE_MAP.get(msg.role, "user")
        parts: list[dict[str, Any]] = []
        if msg.content:
            parts.append({"text": msg.content})
        if msg.tool_calls:
            for tc in msg.tool_calls:
                parts.append({
                    "functionCall": {
                        "name": tc.name,
                        "args": tc.arguments or {},
                    },
                    # Sentinel — Code Assist rejects function calls that
                    # didn't originate from its own chain without this
                    # signature. Same trick opencode-gemini-auth uses.
                    "thoughtSignature": "skip_thought_signature_validator",
                })
        if not parts:
            continue  # Gemini rejects empty parts[]
        contents.append({"role": gemini_role, "parts": parts})

    system_instruction = None
    joined = "\n".join(p for p in system_pieces if p).strip()
    if joined:
        system_instruction = {
            "role": "system",
            "parts": [{"text": joined}],
        }
    return contents, system_instruction


def translate_tools_to_gemini(
    tools: list[ToolSchema] | None,
) -> list[dict[str, Any]]:
    """OC ToolSchemas → Gemini tools[].functionDeclarations[]."""
    if not tools:
        return []
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        decl: dict[str, Any] = {"name": tool.name}
        if tool.description:
            decl["description"] = tool.description
        if tool.parameters:
            decl["parameters"] = _sanitize_schema(tool.parameters)
        declarations.append(decl)
    return [{"functionDeclarations": declarations}] if declarations else []


def build_gemini_request(
    *,
    messages: list[Message],
    tools: list[ToolSchema] | None = None,
    system: str = "",
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Build the inner Gemini request body."""
    contents, system_inst = build_gemini_contents(messages)

    # If caller supplies a system kwarg, prepend it to whatever system messages
    # exist (BaseProvider's system kwarg is the canonical way to do system
    # prompt; Hermes sometimes inlines into messages, so support both).
    if system:
        prepended = {"role": "system", "parts": [{"text": system}]}
        if system_inst is None:
            system_inst = prepended
        else:
            existing_text = system_inst["parts"][0].get("text", "")
            system_inst = {
                "role": "system",
                "parts": [{"text": f"{system}\n{existing_text}".strip()}],
            }

    body: dict[str, Any] = {"contents": contents}
    if system_inst is not None:
        body["systemInstruction"] = system_inst

    gemini_tools = translate_tools_to_gemini(tools)
    if gemini_tools:
        body["tools"] = gemini_tools

    generation_config: dict[str, Any] = {}
    if isinstance(temperature, (int, float)):
        generation_config["temperature"] = float(temperature)
    if isinstance(max_tokens, int) and max_tokens > 0:
        generation_config["maxOutputTokens"] = max_tokens
    if generation_config:
        body["generationConfig"] = generation_config
    return body


def wrap_code_assist_request(
    *,
    project_id: str,
    model: str,
    inner_request: dict[str, Any],
    user_prompt_id: str | None = None,
) -> dict[str, Any]:
    """Wrap the inner request in the Code Assist envelope."""
    return {
        "project": project_id,
        "model": model,
        "user_prompt_id": user_prompt_id or uuid.uuid4().hex,
        "request": inner_request,
    }


# =============================================================================
# Response → ProviderResponse
# =============================================================================

def map_finish_reason(reason: str) -> str:
    """Gemini finish reason → OC stop_reason vocabulary."""
    if reason == "STOP":
        return "end_turn"
    if reason == "MAX_TOKENS":
        return "max_tokens"
    if reason in {"SAFETY", "RECITATION"}:
        return "content_filter"
    return reason.lower() or "end_turn"


def translate_response(
    payload: dict[str, Any],
    *,
    model: str,
) -> ProviderResponse:
    """Cloud Code Assist response → OC ProviderResponse."""
    inner = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    candidates = inner.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        return ProviderResponse(
            message=Message(role="assistant", content=""),
            stop_reason="end_turn",
            usage=Usage(),
        )

    cand = candidates[0]
    content_obj = cand.get("content") or {} if isinstance(cand, dict) else {}
    parts = content_obj.get("parts") if isinstance(content_obj, dict) else []

    text_pieces: list[str] = []
    reasoning_pieces: list[str] = []
    tool_calls: list[ToolCall] = []

    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if part.get("thought") is True:
            if isinstance(part.get("text"), str):
                reasoning_pieces.append(part["text"])
            continue
        if isinstance(part.get("text"), str):
            text_pieces.append(part["text"])
            continue
        fc = part.get("functionCall")
        if isinstance(fc, dict) and fc.get("name"):
            args = fc.get("args") or {}
            if not isinstance(args, dict):
                args = {"_value": args}
            tool_calls.append(
                ToolCall(
                    id=f"call_{uuid.uuid4().hex[:12]}",
                    name=str(fc["name"]),
                    arguments=args,
                )
            )

    usage_meta = inner.get("usageMetadata") or {}
    usage = Usage(
        input_tokens=int(usage_meta.get("promptTokenCount") or 0),
        output_tokens=int(usage_meta.get("candidatesTokenCount") or 0),
        cache_read_tokens=int(usage_meta.get("cachedContentTokenCount") or 0),
    )

    finish_reason = (
        "tool_use"
        if tool_calls
        else map_finish_reason(str(cand.get("finishReason") or "STOP"))
    )

    message = Message(
        role="assistant",
        content="".join(text_pieces),
        tool_calls=tool_calls or None,
        reasoning="".join(reasoning_pieces) or None,
    )
    return ProviderResponse(
        message=message,
        stop_reason=finish_reason,
        usage=usage,
        reasoning="".join(reasoning_pieces) or None,
    )


# =============================================================================
# SSE parsing
# =============================================================================

def iter_sse_payloads(raw_bytes: bytes) -> Iterator[dict[str, Any]]:
    """Parse a chunk of SSE bytes; yield decoded JSON payloads.

    Stops yielding (returns) on ``data: [DONE]``. Skips comment lines
    (starting with ``:``) and non-``data:`` event lines.
    """
    text = raw_bytes.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            return
        if not payload:
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


# =============================================================================
# Transport
# =============================================================================

class CloudCodeTransport:
    """HTTP transport for Cloud Code Assist generate / stream calls.

    Constructed with two callbacks so it doesn't depend on auth state directly:

      - ``access_token_provider()``  → fresh OAuth access_token (handles refresh)
      - ``project_id_provider()``    → resolved GCP project_id

    Both are called per-request so token rotation just works.
    """

    def __init__(
        self,
        *,
        access_token_provider: Callable[[], str],
        project_id_provider: Callable[[], str],
    ) -> None:
        self._access_token_provider = access_token_provider
        self._project_id_provider = project_id_provider

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": accept,
            "Authorization": f"Bearer {self._access_token_provider()}",
            "User-Agent": "opencomputer (gemini-cli-compat)",
            "X-Goog-Api-Client": "gl-python/opencomputer",
            "x-activity-request-id": uuid.uuid4().hex,
        }

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ProviderResponse:
        inner = build_gemini_request(
            messages=messages,
            tools=tools,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        body = wrap_code_assist_request(
            project_id=self._project_id_provider(),
            model=model,
            inner_request=inner,
        )
        url = f"{ENDPOINT}{GENERATE_PATH}"

        # httpx.post is sync; keep it that way — Cloud Code Assist responses
        # come back as a single JSON blob, no streaming benefit. The agent
        # loop already runs providers in a thread when needed.
        response = httpx.post(
            url,
            headers=self._headers(),
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 401:
            raise RuntimeError(
                "Cloud Code Assist unauthorized — your Google OAuth token may "
                "be expired or revoked. Re-run `opencomputer auth login google`."
            )
        if response.status_code == 429:
            raise RuntimeError(
                f"Cloud Code Assist rate_limited: {response.text[:200]}"
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"Cloud Code Assist error {response.status_code}: "
                f"{response.text[:200]}"
            )
        return translate_response(response.json(), model=model)

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        inner = build_gemini_request(
            messages=messages,
            tools=tools,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        body = wrap_code_assist_request(
            project_id=self._project_id_provider(),
            model=model,
            inner_request=inner,
        )
        url = f"{ENDPOINT}{STREAM_PATH}"

        text_buf: list[str] = []
        tool_calls: list[ToolCall] = []
        usage = Usage()
        stop_reason = "end_turn"

        with httpx.stream(
            "POST",
            url,
            headers=self._headers(accept="text/event-stream"),
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            if response.status_code != 200:
                raw = b"".join(response.iter_bytes())
                if response.status_code == 401:
                    raise RuntimeError(
                        "Cloud Code Assist unauthorized — re-run "
                        "`opencomputer auth login google`."
                    )
                raise RuntimeError(
                    f"Cloud Code Assist error {response.status_code}: "
                    f"{raw[:200]!r}"
                )

            buffer = b""
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                buffer += chunk
                # Split on double-newline (SSE message boundary)
                while b"\n\n" in buffer:
                    msg_bytes, buffer = buffer.split(b"\n\n", 1)
                    for payload in iter_sse_payloads(msg_bytes):
                        # Each event mirrors the non-streaming shape
                        partial = translate_response(payload, model=model)
                        if partial.message.content:
                            text_buf.append(partial.message.content)
                            yield StreamEvent(
                                kind="text_delta", text=partial.message.content
                            )
                        if partial.message.tool_calls:
                            for tc in partial.message.tool_calls:
                                tool_calls.append(tc)
                                yield StreamEvent(kind="tool_call")
                        if partial.usage.input_tokens or partial.usage.output_tokens:
                            usage = partial.usage
                        if partial.stop_reason and partial.stop_reason != "end_turn":
                            stop_reason = partial.stop_reason

            # Flush trailing bytes
            if buffer:
                for payload in iter_sse_payloads(buffer):
                    partial = translate_response(payload, model=model)
                    if partial.message.content:
                        text_buf.append(partial.message.content)
                        yield StreamEvent(
                            kind="text_delta", text=partial.message.content
                        )
                    if partial.message.tool_calls:
                        for tc in partial.message.tool_calls:
                            tool_calls.append(tc)
                            yield StreamEvent(kind="tool_call")

        final_message = Message(
            role="assistant",
            content="".join(text_buf),
            tool_calls=tool_calls or None,
        )
        if tool_calls and stop_reason == "end_turn":
            stop_reason = "tool_use"
        final = ProviderResponse(
            message=final_message,
            stop_reason=stop_reason,
            usage=usage,
        )
        yield StreamEvent(kind="done", response=final)


__all__ = [
    "CloudCodeTransport",
    "build_gemini_contents",
    "build_gemini_request",
    "iter_sse_payloads",
    "map_finish_reason",
    "translate_response",
    "translate_tools_to_gemini",
    "wrap_code_assist_request",
]
