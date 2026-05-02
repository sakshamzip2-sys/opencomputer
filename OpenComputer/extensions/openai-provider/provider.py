"""
OpenAI provider — BaseProvider implementation for OpenAI Chat Completions API.

Also works with OpenAI-compatible endpoints (OpenRouter, Together AI,
local Ollama, etc.) via OPENAI_BASE_URL env var.

Auth: OpenAI's native scheme is `Authorization: Bearer <key>`, so no
proxy quirks — the official SDK already sends what proxies expect.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from openai import RateLimitError as OpenAIRateLimitError
from openai.types.chat import ChatCompletion

from opencomputer.agent.credential_pool import CredentialPool
from opencomputer.agent.rate_guard import (
    format_remaining,
    rate_limit_remaining,
    record_rate_limit,
)
from plugin_sdk.core import Message, ToolCall
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    RateLimitedError,
    StreamEvent,
    Usage,
)
from plugin_sdk.tool_contract import ToolSchema

_RATE_GUARD_PROVIDER = "openai"


def _check_rate_limit() -> None:
    """TS-T7 — short-circuit if a previous 429 hasn't reset yet."""
    remaining = rate_limit_remaining(_RATE_GUARD_PROVIDER)
    if remaining is not None:
        raise RateLimitedError(
            _RATE_GUARD_PROVIDER,
            f"OpenAI rate-limited; wait {format_remaining(remaining)}",
        )


def _record_429(exc: OpenAIRateLimitError) -> None:
    """TS-T7 — persist the 429 so concurrent sessions back off too."""
    headers: dict[str, str] | None = None
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            headers = dict(response.headers)
        except Exception:
            headers = None
    record_rate_limit(_RATE_GUARD_PROVIDER, headers=headers)


_log = logging.getLogger("opencomputer.providers.openai")


_OPENAI_SUPPORTED_IMAGE_MEDIA_TYPES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)


def _build_openai_multimodal_content(
    *, text: str, image_paths: list[str]
) -> list[dict[str, Any]]:
    """Build OpenAI Chat-Completions content array combining text + images.

    OpenAI's multimodal shape is ``[{"type": "text", "text": ...},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}]``
    — different from Anthropic's ``image / source`` shape but the same
    inputs (path on disk, base64 the bytes, infer media type via
    ``mimetypes``). Skips unreadable / unsupported / >20 MB attachments
    with a WARNING log; never raises so a bad attachment doesn't kill
    the turn.

    Order: text first, then images — OpenAI's vision examples lead with
    that shape.
    """
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for path_str in image_paths:
        path = Path(path_str)
        try:
            data = path.read_bytes()
        except OSError as exc:
            _log.warning("image attachment unreadable: %s (%s)", path, exc)
            continue
        if len(data) > 20 * 1024 * 1024:
            _log.warning(
                "image attachment over 20 MB cap; skipping: %s (%d bytes)",
                path,
                len(data),
            )
            continue
        media_type, _ = mimetypes.guess_type(str(path))
        if media_type not in _OPENAI_SUPPORTED_IMAGE_MEDIA_TYPES:
            _log.warning(
                "image attachment has unsupported media type %r; skipping: %s",
                media_type,
                path,
            )
            continue
        b64 = base64.b64encode(data).decode("ascii")
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{b64}"},
            }
        )
    if not blocks:
        # Edge case: every attachment was skipped AND text is empty.
        # Send an empty text block so the API doesn't reject the request.
        blocks.append({"type": "text", "text": text or ""})
    return blocks


def _extract_cached_tokens(usage: Any) -> int:
    """Pull ``prompt_tokens_details.cached_tokens`` off an OpenAI Usage.

    OpenAI does automatic prompt caching for >1024-token prompts on
    supported models; the cached count surfaces via the optional
    ``prompt_tokens_details.cached_tokens`` field on the response usage
    object. Older SDKs / non-cached responses lack the field.

    Returns 0 when absent. Never raises — defensive on every hop.
    """
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    return int(getattr(details, "cached_tokens", 0) or 0)


def _extract_reasoning_content(msg: Any) -> str | None:
    """Pull ``reasoning_content`` off an OpenAI choice.message.

    OpenAI o1/o3 + DeepSeek R1 + several OpenRouter-routed reasoning
    models surface reasoning_content. Field is absent on regular models;
    return None to keep ``ProviderResponse.reasoning`` at its no-thinking
    default. Empty string normalised to None for consistency with
    Anthropic's None-on-no-thinking semantics.
    """
    if msg is None:
        return None
    content = getattr(msg, "reasoning_content", None)
    if not content:
        return None
    return str(content)


class OpenAIProvider(BaseProvider):
    name = "openai"
    default_model = "gpt-5.4"

    _api_key_env: str = "OPENAI_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """
        Args:
            api_key:  Defaults to $OPENAI_API_KEY. Comma-separated value triggers pool mode (PR-A).
            base_url: Defaults to $OPENAI_BASE_URL, else OpenAI's native endpoint.
        """
        # Optional credential pool (PR-A): comma-separated env value triggers pool mode.
        # Single key (no comma) → no pool, behavior IDENTICAL to today (regression-tested).
        api_key_raw = api_key or os.environ.get(self._api_key_env, "")
        if "," in api_key_raw:
            keys = [k.strip() for k in api_key_raw.split(",") if k.strip()]
            self._credential_pool: CredentialPool | None = CredentialPool(keys=keys) if len(keys) > 1 else None
            self._api_key = keys[0] if keys else api_key_raw
        else:
            self._credential_pool = None
            self._api_key = api_key_raw.strip()

        key = self._api_key
        if not key:
            raise RuntimeError(
                "OpenAI API key not set. Export OPENAI_API_KEY or pass api_key."
            )
        base = base_url or os.environ.get("OPENAI_BASE_URL") or None
        self._base = base
        kwargs: dict[str, Any] = {"api_key": key}
        if base:
            kwargs["base_url"] = base
        self.client = AsyncOpenAI(**kwargs)

    # ─── message conversion ─────────────────────────────────────────

    def _to_openai_messages(
        self, messages: list[Message], system: str = ""
    ) -> list[dict[str, Any]]:
        """Convert our canonical Message list to OpenAI's chat format."""
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "system":
                continue  # passed separately via `system` param
            if m.role == "assistant" and m.tool_calls:
                out.append(
                    {
                        "role": "assistant",
                        "content": m.content or None,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments),
                                },
                            }
                            for tc in m.tool_calls
                        ],
                    }
                )
            elif m.role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "content": m.content,
                        "tool_call_id": m.tool_call_id,
                    }
                )
            else:
                # User / assistant text message. If the message carries
                # image attachments, build a multimodal content array
                # (OpenAI vision shape — image_url with data: URI).
                if m.attachments:
                    out.append(
                        {
                            "role": m.role,
                            "content": _build_openai_multimodal_content(
                                text=m.content, image_paths=m.attachments
                            ),
                        }
                    )
                else:
                    out.append({"role": m.role, "content": m.content})
        return out

    @property
    def capabilities(self):  # type: ignore[override]
        from plugin_sdk import CacheTokens, ProviderCapabilities

        def _extract(usage: Any) -> CacheTokens:
            details = getattr(usage, "prompt_tokens_details", None)
            cached = 0
            if details is not None:
                cached = int(getattr(details, "cached_tokens", 0) or 0)
            return CacheTokens(read=cached, write=0)

        return ProviderCapabilities(
            requires_reasoning_resend_in_tool_cycle=False,
            reasoning_block_kind=None,
            extracts_cache_tokens=_extract,
            min_cache_tokens=lambda _model: 1024,
            supports_long_ttl=False,
        )

    def _parse_response(self, resp: ChatCompletion) -> ProviderResponse:
        """Convert an OpenAI response to our canonical Message + metadata."""
        choice = resp.choices[0]
        raw_msg = choice.message

        tool_calls: list[ToolCall] = []
        if raw_msg.tool_calls:
            for tc in raw_msg.tool_calls:
                fn = tc.function
                try:
                    args = json.loads(fn.arguments) if fn.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(id=tc.id, name=fn.name, arguments=args))

        msg = Message(
            role="assistant",
            content=raw_msg.content or "",
            tool_calls=tool_calls if tool_calls else None,
        )
        usage = Usage(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            cache_read_tokens=_extract_cached_tokens(resp.usage),
        )
        # OpenAI stop_reason names aren't identical to Anthropic's; normalize.
        finish = choice.finish_reason or "stop"
        stop_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "content_filter": "end_turn",
        }
        stop_reason = stop_map.get(finish, "end_turn")
        return ProviderResponse(
            message=msg,
            stop_reason=stop_reason,
            usage=usage,
            reasoning=_extract_reasoning_content(raw_msg),
        )

    # ─── completion ────────────────────────────────────────────────

    def _build_client_for_key(self, key: str) -> AsyncOpenAI:
        """Build an AsyncOpenAI client for the given key (used in pool rotation)."""
        kwargs: dict[str, Any] = {"api_key": key}
        if self._base:
            kwargs["base_url"] = self._base
        return AsyncOpenAI(**kwargs)

    async def _do_complete(
        self,
        key: str,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        runtime_extras: dict | None = None,
        response_schema: dict | None = None,
    ) -> ProviderResponse:
        """Low-level complete using the given API key (pool-rotation target)."""
        # TS-T7 — short-circuit before the SDK so concurrent sessions
        # don't keep pinging while a 429 cools down.
        _check_rate_limit()

        client = self._build_client_for_key(key) if key != self._api_key else self.client
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages, system),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = [t.to_openai_format() for t in tools]
        # Tier 2.A — /reasoning + /fast slash commands → API kwargs.
        if runtime_extras:
            from opencomputer.agent.runtime_flags import (
                openai_kwargs_from_runtime,
            )
            kwargs.update(
                openai_kwargs_from_runtime(
                    reasoning_effort=runtime_extras.get("reasoning_effort"),
                    service_tier=runtime_extras.get("service_tier"),
                )
            )
        # Subsystem C — structured outputs.
        if response_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.get("name", "response"),
                    "schema": response_schema["schema"],
                    "strict": True,
                },
            }
        try:
            resp = await client.chat.completions.create(**kwargs)
        except OpenAIRateLimitError as exc:
            _record_429(exc)
            raise
        return self._parse_response(resp)

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stream: bool = False,
        runtime_extras: dict | None = None,
        response_schema: dict | None = None,
    ) -> ProviderResponse:
        if self._credential_pool is None:
            return await self._do_complete(
                self._api_key,
                model=model,
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                runtime_extras=runtime_extras,
                response_schema=response_schema,
            )

        def _is_auth_failure(exc: Exception) -> bool:
            return "401" in str(exc) or "authentication" in str(exc).lower()

        return await self._credential_pool.with_retry(
            lambda key: self._do_complete(
                key,
                model=model,
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                runtime_extras=runtime_extras,
                response_schema=response_schema,
            ),
            is_auth_failure=_is_auth_failure,
        )

    async def _do_stream_complete(
        self,
        key: str,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        runtime_extras: dict | None = None,
        response_schema: dict | None = None,
    ) -> ProviderResponse:
        """Low-level streaming that aggregates into a ProviderResponse (pool target)."""
        # TS-T7 — short-circuit before the SDK.
        _check_rate_limit()

        client = self._build_client_for_key(key) if key != self._api_key else self.client
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages, system),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = [t.to_openai_format() for t in tools]
        # Tier 2.A — /reasoning + /fast slash commands → API kwargs.
        if runtime_extras:
            from opencomputer.agent.runtime_flags import (
                openai_kwargs_from_runtime,
            )
            kwargs.update(
                openai_kwargs_from_runtime(
                    reasoning_effort=runtime_extras.get("reasoning_effort"),
                    service_tier=runtime_extras.get("service_tier"),
                )
            )
        # Subsystem C — structured outputs.
        if response_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.get("name", "response"),
                    "schema": response_schema["schema"],
                    "strict": True,
                },
            }

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_accum: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: Usage = Usage()

        try:
            stream = await client.chat.completions.create(**kwargs)
        except OpenAIRateLimitError as exc:
            _record_429(exc)
            raise
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                content_parts.append(delta.content)
            # OpenAI o1/o3 / DeepSeek R1 / OpenRouter reasoning routes
            # surface ``reasoning_content`` as a vendor extension on the
            # delta. Aggregate across chunks; surface on final response.
            delta_reasoning = getattr(delta, "reasoning_content", None)
            if delta_reasoning:
                reasoning_parts.append(str(delta_reasoning))
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    slot = tool_calls_accum.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            if getattr(chunk, "usage", None):
                usage = Usage(
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                    cache_read_tokens=_extract_cached_tokens(chunk.usage),
                )

        tool_calls: list[ToolCall] = []
        for slot in tool_calls_accum.values():
            try:
                args = json.loads(slot["arguments"]) if slot["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=args))

        msg = Message(
            role="assistant",
            content="".join(content_parts),
            tool_calls=tool_calls if tool_calls else None,
        )
        stop_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "content_filter": "end_turn",
        }
        return ProviderResponse(
            message=msg,
            stop_reason=stop_map.get(finish_reason, "end_turn"),
            usage=usage,
            reasoning="".join(reasoning_parts) if reasoning_parts else None,
        )

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        runtime_extras: dict | None = None,
        response_schema: dict | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream via OpenAI's chat.completions.create(stream=True)."""
        if self._credential_pool is not None:
            # Pool path: on auth failure, rotate and re-try non-streaming.
            def _is_auth_failure(exc: Exception) -> bool:
                return "401" in str(exc) or "authentication" in str(exc).lower()

            response = await self._credential_pool.with_retry(
                lambda key: self._do_stream_complete(
                    key,
                    model=model,
                    messages=messages,
                    system=system,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    runtime_extras=runtime_extras,
                    response_schema=response_schema,
                ),
                is_auth_failure=_is_auth_failure,
            )
            if response.message.content:
                yield StreamEvent(kind="text_delta", text=response.message.content)
            yield StreamEvent(kind="done", response=response)
            return

        # No pool — native streaming path (unchanged behavior).
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages, system),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = [t.to_openai_format() for t in tools]
        # Tier 2.A — /reasoning + /fast slash commands → API kwargs.
        if runtime_extras:
            from opencomputer.agent.runtime_flags import (
                openai_kwargs_from_runtime,
            )
            kwargs.update(
                openai_kwargs_from_runtime(
                    reasoning_effort=runtime_extras.get("reasoning_effort"),
                    service_tier=runtime_extras.get("service_tier"),
                )
            )
        # Subsystem C — structured outputs.
        if response_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.get("name", "response"),
                    "schema": response_schema["schema"],
                    "strict": True,
                },
            }

        # Aggregate state while streaming — used to build the final ProviderResponse
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_accum: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: Usage = Usage()

        # TS-T7 — short-circuit if a previous 429 hasn't reset.
        _check_rate_limit()
        try:
            stream = await self.client.chat.completions.create(**kwargs)
        except OpenAIRateLimitError as exc:
            _record_429(exc)
            raise
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is None:
                continue
            # OpenAI o1/o3 / DeepSeek R1 / OpenRouter reasoning routes
            # surface ``reasoning_content`` as a vendor extension on the
            # delta. Yield first so the renderer's thinking panel updates
            # before any text in the same chunk; also aggregate for the
            # final ProviderResponse.reasoning field.
            delta_reasoning = getattr(delta, "reasoning_content", None)
            if delta_reasoning:
                reasoning_parts.append(str(delta_reasoning))
                yield StreamEvent(
                    kind="thinking_delta", text=str(delta_reasoning)
                )
            if delta.content:
                content_parts.append(delta.content)
                yield StreamEvent(kind="text_delta", text=delta.content)
            # Accumulate tool calls by index
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    slot = tool_calls_accum.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            if getattr(chunk, "usage", None):
                usage = Usage(
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                    cache_read_tokens=_extract_cached_tokens(chunk.usage),
                )

        # Reconstruct the final ProviderResponse
        tool_calls: list[ToolCall] = []
        for slot in tool_calls_accum.values():
            try:
                args = json.loads(slot["arguments"]) if slot["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=args))

        msg = Message(
            role="assistant",
            content="".join(content_parts),
            tool_calls=tool_calls if tool_calls else None,
        )
        stop_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "content_filter": "end_turn",
        }
        final = ProviderResponse(
            message=msg,
            stop_reason=stop_map.get(finish_reason, "end_turn"),
            usage=usage,
            reasoning="".join(reasoning_parts) if reasoning_parts else None,
        )
        yield StreamEvent(kind="done", response=final)

    async def count_tokens(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
    ) -> int:
        """Count input tokens locally via ``tiktoken``.

        Falls back to the heuristic if ``tiktoken`` is not installed
        or doesn't recognise the model. Subsystem D, 2026-05-02.
        """
        try:
            import tiktoken
        except ImportError:
            from plugin_sdk.provider_contract import _heuristic_token_count
            return _heuristic_token_count(messages, system, tools)

        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            # Unknown model — fall back to the cl100k_base encoder used
            # by gpt-4 / gpt-3.5-turbo / gpt-4o variants.
            try:
                enc = tiktoken.get_encoding("cl100k_base")
            except Exception:  # noqa: BLE001
                from plugin_sdk.provider_contract import (
                    _heuristic_token_count,
                )
                return _heuristic_token_count(messages, system, tools)

        import json as _json
        total = len(enc.encode(system)) if system else 0
        for m in messages:
            if m.content:
                total += len(enc.encode(m.content))
            for tc in (m.tool_calls or []):
                total += len(
                    enc.encode(tc.name + _json.dumps(tc.arguments or {}))
                )
        if tools:
            for t in tools:
                total += len(enc.encode(_json.dumps(t.to_openai_format())))
        return max(1, total)

    async def submit_batch(self, requests):
        """Submit a batch via OpenAI's async-file-based batch API.

        Subsystem E follow-up (2026-05-02). Different shape from
        Anthropic — OpenAI requires:
        1. Upload a JSONL file (one request per line) via files.create
        2. Create a batch referencing the file id via batches.create

        Polling + result download happen in :meth:`get_batch_results`.

        Composes with Subsystems B (effort via runtime_extras) and C
        (response_schema). Each per-request entry carries its own
        kwargs, mirroring the live-call translator.
        """
        import io
        import json as _json

        from plugin_sdk.provider_contract import BatchRequest as _Br

        # Build the JSONL body in memory.
        lines: list[str] = []
        for req in requests:
            assert isinstance(req, _Br)
            body: dict[str, Any] = {
                "model": req.model,
                "messages": self._to_openai_messages(req.messages, req.system),
                "max_tokens": req.max_tokens,
                "temperature": 1.0,
            }
            if req.runtime_extras:
                from opencomputer.agent.runtime_flags import (
                    openai_kwargs_from_runtime,
                )
                body.update(
                    openai_kwargs_from_runtime(
                        reasoning_effort=req.runtime_extras.get("reasoning_effort"),
                        service_tier=req.runtime_extras.get("service_tier"),
                    )
                )
            if req.response_schema is not None:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": req.response_schema.get("name", "response"),
                        "schema": req.response_schema["schema"],
                        "strict": True,
                    },
                }
            lines.append(
                _json.dumps(
                    {
                        "custom_id": req.custom_id,
                        "method": "POST",
                        "url": "/v1/chat/completions",
                        "body": body,
                    }
                )
            )
        jsonl_bytes = ("\n".join(lines) + "\n").encode("utf-8")

        # Upload the JSONL as a "batch" file.
        uploaded = await self.client.files.create(
            file=("batch.jsonl", io.BytesIO(jsonl_bytes)),
            purpose="batch",
        )
        # Create the batch referencing the uploaded file.
        batch = await self.client.batches.create(
            input_file_id=uploaded.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
        )
        return batch.id

    async def get_batch_results(self, batch_id: str):
        """Fetch results for an OpenAI batch.

        Returns one ``BatchResult`` per entry once the batch is
        complete. While the batch is still in_progress / validating /
        finalizing, returns a single placeholder
        ``BatchResult(status="processing")`` — caller polls again
        later.
        """
        import json as _json

        from plugin_sdk.core import Message as _Msg
        from plugin_sdk.provider_contract import (
            BatchResult as _BResult,
        )
        from plugin_sdk.provider_contract import (
            ProviderResponse as _PResp,
        )
        from plugin_sdk.provider_contract import (
            Usage as _Usage,
        )

        batch = await self.client.batches.retrieve(batch_id)
        if batch.status in (
            "in_progress",
            "validating",
            "finalizing",
            "cancelling",
        ):
            return [_BResult(custom_id="__pending__", status="processing")]

        if batch.status == "expired":
            return [_BResult(custom_id="__pending__", status="expired")]
        if batch.status == "cancelled":
            return [_BResult(custom_id="__pending__", status="canceled")]
        if batch.status == "failed":
            return [
                _BResult(
                    custom_id="__pending__",
                    status="errored",
                    error=str(batch.errors)
                    if getattr(batch, "errors", None)
                    else "batch failed",
                )
            ]

        # status == "completed" — download output_file_id and translate.
        output_file_id = getattr(batch, "output_file_id", None)
        if not output_file_id:
            return [
                _BResult(
                    custom_id="__pending__",
                    status="errored",
                    error="batch completed but output_file_id missing",
                )
            ]

        content_response = await self.client.files.content(output_file_id)
        # OpenAI SDK returns a streamable / readable; .text reads it all.
        try:
            raw = content_response.text
        except AttributeError:
            raw = await content_response.aread()
            raw = raw.decode("utf-8")

        out: list = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            custom_id = entry.get("custom_id", "")
            response_payload = entry.get("response", {}) or {}
            error_payload = entry.get("error")

            if error_payload:
                out.append(
                    _BResult(
                        custom_id=custom_id,
                        status="errored",
                        error=str(error_payload),
                    )
                )
                continue

            body = response_payload.get("body", {}) or {}
            choices = body.get("choices") or []
            if not choices:
                out.append(
                    _BResult(
                        custom_id=custom_id,
                        status="errored",
                        error="no choices in response",
                    )
                )
                continue

            content = choices[0].get("message", {}).get("content", "")
            usage_data = body.get("usage", {}) or {}
            response = _PResp(
                message=_Msg(role="assistant", content=content or ""),
                stop_reason=choices[0].get("finish_reason", "end_turn"),
                usage=_Usage(
                    input_tokens=int(usage_data.get("prompt_tokens", 0)),
                    output_tokens=int(usage_data.get("completion_tokens", 0)),
                ),
            )
            out.append(
                _BResult(
                    custom_id=custom_id,
                    status="succeeded",
                    response=response,
                )
            )
        return out


__all__ = ["OpenAIProvider"]
