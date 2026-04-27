"""
Anthropic provider — wraps the Anthropic SDK behind BaseProvider.

This is the first concrete provider. Later it can be moved to an
extension package (extensions/anthropic-provider/) for dogfooding the
plugin system, but for Phase 1 it lives in-tree so we can ship quickly.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import httpx
from anthropic import AsyncAnthropic
from anthropic import RateLimitError as AnthropicRateLimitError
from anthropic.types import Message as AnthropicMessage
from pydantic import BaseModel, Field

from opencomputer.agent.credential_pool import CredentialPool
from opencomputer.agent.prompt_caching import apply_anthropic_cache_control
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

_RATE_GUARD_PROVIDER = "anthropic"


def _check_rate_limit() -> None:
    """TS-T7 — short-circuit if a previous 429 hasn't reset yet."""
    remaining = rate_limit_remaining(_RATE_GUARD_PROVIDER)
    if remaining is not None:
        raise RateLimitedError(
            _RATE_GUARD_PROVIDER,
            f"Anthropic rate-limited; wait {format_remaining(remaining)}",
        )


def _record_429(exc: AnthropicRateLimitError) -> None:
    """TS-T7 — persist the 429 so concurrent sessions back off too."""
    headers: dict[str, str] | None = None
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            headers = dict(response.headers)
        except Exception:
            headers = None
    record_rate_limit(_RATE_GUARD_PROVIDER, headers=headers)


_log = logging.getLogger("opencomputer.providers.anthropic")


_SUPPORTED_IMAGE_MEDIA_TYPES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)


def _build_anthropic_multimodal_content(
    *, text: str, image_paths: list[str]
) -> list[dict[str, Any]]:
    """Build Anthropic content array combining text + base64-encoded images.

    Reads each path in ``image_paths``, base64-encodes the bytes, infers
    the media type via ``mimetypes``, and emits Anthropic's
    ``{"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}``
    blocks. Skips any path that fails to read, has an unsupported media type,
    or exceeds Anthropic's 5 MB per-image cap — logged at WARNING; never
    raises so a bad attachment doesn't kill the turn.

    Order: images first, then text — matches what Claude Desktop sends and
    what humans expect ("here are images, here's my question about them").
    """
    blocks: list[dict[str, Any]] = []
    for path_str in image_paths:
        path = Path(path_str)
        try:
            data = path.read_bytes()
        except OSError as exc:
            _log.warning("image attachment unreadable: %s (%s)", path, exc)
            continue
        if len(data) > 5 * 1024 * 1024:
            _log.warning(
                "image attachment over 5 MB cap; skipping: %s (%d bytes)",
                path,
                len(data),
            )
            continue
        media_type, _ = mimetypes.guess_type(str(path))
        if media_type not in _SUPPORTED_IMAGE_MEDIA_TYPES:
            _log.warning(
                "image attachment has unsupported media type %r; skipping: %s",
                media_type,
                path,
            )
            continue
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            }
        )
    if text:
        blocks.append({"type": "text", "text": text})
    if not blocks:
        # Edge case: every attachment was skipped AND text is empty. Send
        # a single empty text block so Anthropic's API doesn't reject the
        # request for empty content.
        blocks.append({"type": "text", "text": text or ""})
    return blocks


class AnthropicProviderConfig(BaseModel):
    """Pydantic schema for AnthropicProvider construction kwargs.

    Wired onto ``AnthropicProvider.config_schema`` (Task I.6). The
    plugin registry uses this to validate ``provider.config`` at
    ``register_provider`` time and raise ``ValueError`` on shape
    mismatch — catching bad config at plugin load instead of at first
    request.

    Fields mirror the ``__init__`` signature: all three are optional
    because construction also reads from env vars
    (``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``,
    ``ANTHROPIC_AUTH_MODE``).

    ``auth_mode`` accepts the legacy ``"x-api-key"`` spelling AND the
    newer ``"api_key"`` spelling. The construction logic coerces both
    to the same effective behavior (``"x-api-key"`` header mode).
    """

    api_key: str | None = Field(default=None)
    base_url: str | None = Field(default=None)
    auth_mode: Literal["api_key", "x-api-key", "bearer"] = Field(default="api_key")


async def _strip_x_api_key(request: httpx.Request) -> None:
    """httpx event hook: remove x-api-key header before sending.

    Used when talking to proxies that authenticate via Bearer tokens and
    forward x-api-key unchanged to the upstream Anthropic API (which then
    rejects it as invalid). Must run at the last moment so the Anthropic
    SDK's own auth path is undisturbed.
    """
    request.headers.pop("x-api-key", None)


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    default_model = "claude-opus-4-7"
    #: Task I.6 — schema used by the plugin registry to validate
    #: ``self.config`` at ``register_provider`` time.
    config_schema = AnthropicProviderConfig

    _api_key_env: str = "ANTHROPIC_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        auth_mode: str | None = None,
    ) -> None:
        """
        Args:
            api_key: API key / proxy key. Defaults to $ANTHROPIC_API_KEY.
                     Comma-separated value triggers pool mode (PR-A).
            base_url: Override the API endpoint (for proxies like Claude Router).
                      Defaults to $ANTHROPIC_BASE_URL, or None (direct Anthropic).
            auth_mode: "x-api-key" (Anthropic native) or "bearer"
                      (Authorization: Bearer header — for proxies that require it).
                      Defaults to $ANTHROPIC_AUTH_MODE, or "x-api-key".
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
                "Anthropic API key not set. Export ANTHROPIC_API_KEY or pass api_key."
            )
        base = base_url or os.environ.get("ANTHROPIC_BASE_URL") or None
        mode = (auth_mode or os.environ.get("ANTHROPIC_AUTH_MODE") or "x-api-key").lower()

        # Pre-validate mode with a clear RuntimeError before the pydantic
        # schema turns it into a less-helpful ValidationError. Keeps the
        # existing error message contract that callers rely on.
        if mode not in ("x-api-key", "api_key", "bearer"):
            raise RuntimeError(
                f"Unknown ANTHROPIC_AUTH_MODE: {mode!r} "
                f"(expected 'x-api-key', 'api_key', or 'bearer')"
            )

        # Task I.6: store a validated config snapshot so the plugin
        # registry can re-check it against ``config_schema`` at
        # ``register_provider`` time. The schema is permissive about
        # auth_mode spelling (accepts "x-api-key" and "api_key"), so
        # pass the effective value through.
        self.config = AnthropicProviderConfig(
            api_key=key,
            base_url=base,
            auth_mode=mode,  # type: ignore[arg-type]
        )

        self._base = base
        self._mode = mode
        kwargs: dict[str, Any] = {"api_key": key}
        if base:
            kwargs["base_url"] = base
        if mode == "bearer":
            # For proxies like Claude Router: add Authorization: Bearer AND
            # strip x-api-key on the way out (the SDK adds it automatically
            # from api_key, and some proxies forward it to upstream Anthropic
            # which then rejects the proxy key as "invalid x-api-key").
            kwargs["default_headers"] = {"Authorization": f"Bearer {key}"}
            kwargs["http_client"] = httpx.AsyncClient(
                event_hooks={"request": [_strip_x_api_key]},
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        # Otherwise mode is "x-api-key" or "api_key" → default SDK behavior
        # uses x-api-key; both spellings are equivalent here.
        self.client = AsyncAnthropic(**kwargs)

    # ─── message conversion ─────────────────────────────────────────

    def _to_anthropic_messages(self, messages: list[Message]) -> list[dict[str, Any]]:

        """Convert our canonical Message list to Anthropic's message format."""
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                # system messages are passed separately, not in messages[]
                continue
            if m.role == "assistant" and m.tool_calls:
                content: list[dict[str, Any]] = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                out.append({"role": "assistant", "content": content})
            elif m.role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
            else:
                # User / assistant text message. If the message carries
                # image attachments, build a multimodal content array
                # with text + base64-encoded images so the model can
                # actually see them.
                if m.attachments:
                    out.append(
                        {
                            "role": m.role,
                            "content": _build_anthropic_multimodal_content(
                                text=m.content, image_paths=m.attachments
                            ),
                        }
                    )
                else:
                    out.append({"role": m.role, "content": m.content})
        return out

    def _apply_cache_control(
        self,
        anthropic_messages: list[dict[str, Any]],
        system: str,
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Apply Anthropic prompt caching (system_and_3 strategy).

        Prepends ``system`` to ``anthropic_messages`` as a synthetic system
        message, applies cache_control breakpoints (system + last 3 non-system
        messages), then extracts system back out as a list of content blocks
        so it can be passed to the SDK's ``system=`` parameter with cache_control
        preserved.

        Returns:
            (system_for_sdk, messages_for_sdk) — system is a list of content
            blocks (e.g. ``[{"type": "text", "text": "...", "cache_control": ...}]``)
            when there is a system prompt, or an empty string otherwise.
        """
        # Build a unified list with system at index 0 (if any) so the
        # cache function can apply the system_and_3 strategy uniformly.
        unified: list[dict[str, Any]] = []
        if system:
            unified.append({"role": "system", "content": system})
        unified.extend(anthropic_messages)

        # Apply cache_control breakpoints. native_anthropic=True puts
        # cache_control on the message dict directly for tool messages
        # (Anthropic SDK pattern).
        cached = apply_anthropic_cache_control(unified, native_anthropic=True)

        # Extract system back out as a list of content blocks (preserves
        # cache_control). The Anthropic SDK accepts ``system=`` as either
        # a string or a list of content blocks; the list form is required
        # to carry cache_control.
        if system and cached and cached[0].get("role") == "system":
            sys_content = cached[0].get("content")
            sys_for_sdk: Any = sys_content if isinstance(sys_content, list) else system
            messages_for_sdk = cached[1:]
        else:
            sys_for_sdk = system
            messages_for_sdk = cached

        return sys_for_sdk, messages_for_sdk

    def _parse_response(self, resp: AnthropicMessage) -> ProviderResponse:
        """Convert an Anthropic response back to our canonical Message + metadata."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input) if block.input else {},
                    )
                )
        msg = Message(
            role="assistant",
            content="\n".join(text_parts),
            tool_calls=tool_calls if tool_calls else None,
        )
        usage = Usage(
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
        return ProviderResponse(
            message=msg,
            stop_reason=resp.stop_reason or "end_turn",
            usage=usage,
        )

    # ─── completion ────────────────────────────────────────────────

    def _build_client_for_key(self, key: str) -> AsyncAnthropic:
        """Build an AsyncAnthropic client for the given key (used in pool rotation)."""
        kwargs: dict[str, Any] = {"api_key": key}
        if self._base:
            kwargs["base_url"] = self._base
        if self._mode == "bearer":
            kwargs["default_headers"] = {"Authorization": f"Bearer {key}"}
            kwargs["http_client"] = httpx.AsyncClient(
                event_hooks={"request": [_strip_x_api_key]},
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return AsyncAnthropic(**kwargs)

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
    ) -> ProviderResponse:
        """Low-level complete using the given API key (pool-rotation target)."""
        # TS-T7 — short-circuit before the SDK so concurrent sessions
        # don't keep pinging while a 429 cools down.
        _check_rate_limit()

        client = self._build_client_for_key(key) if key != self._api_key else self.client
        anthropic_messages = self._to_anthropic_messages(messages)
        # TS-T1 — apply Anthropic prompt caching (system_and_3 strategy).
        # Up to 4 cache_control breakpoints (system + last 3 non-system
        # messages) for ~75% input-token cost reduction on multi-turn
        # conversations.
        sys_for_sdk, api_messages = self._apply_cache_control(anthropic_messages, system)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
        }
        if sys_for_sdk:
            kwargs["system"] = sys_for_sdk
        if tools:
            kwargs["tools"] = [t.to_anthropic_format() for t in tools]
        try:
            resp = await client.messages.create(**kwargs)
        except AnthropicRateLimitError as exc:
            # TS-T7 — record the 429 so other sessions back off, then
            # re-raise so the caller's retry/fallback logic still sees it.
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
    ) -> ProviderResponse:
        """Low-level stream_complete that aggregates into a ProviderResponse (pool target)."""
        # TS-T7 — same cross-session guard as the non-streaming path.
        _check_rate_limit()

        client = self._build_client_for_key(key) if key != self._api_key else self.client
        anthropic_messages = self._to_anthropic_messages(messages)
        # TS-T1 — apply Anthropic prompt caching (system_and_3 strategy).
        sys_for_sdk, api_messages = self._apply_cache_control(anthropic_messages, system)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
        }
        if sys_for_sdk:
            kwargs["system"] = sys_for_sdk
        if tools:
            kwargs["tools"] = [t.to_anthropic_format() for t in tools]
        try:
            async with client.messages.stream(**kwargs) as stream_ctx:
                final = await stream_ctx.get_final_message()
        except AnthropicRateLimitError as exc:
            _record_429(exc)
            raise
        return self._parse_response(final)

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> AsyncIterator[StreamEvent]:
        """Stream response events via Anthropic's `messages.stream()` context.

        Yields text_delta events as tokens arrive, then a single "done" event
        with the final ProviderResponse (including tool calls if any).
        """
        anthropic_messages = self._to_anthropic_messages(messages)
        # TS-T1 — apply Anthropic prompt caching (system_and_3 strategy).
        sys_for_sdk, api_messages = self._apply_cache_control(anthropic_messages, system)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
        }
        if sys_for_sdk:
            kwargs["system"] = sys_for_sdk
        if tools:
            kwargs["tools"] = [t.to_anthropic_format() for t in tools]

        if self._credential_pool is not None:
            # Pool path: stream_complete falls back to aggregated response on rotation.
            # Streaming is best-effort; on auth failure we rotate and re-try non-streaming.
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
                ),
                is_auth_failure=_is_auth_failure,
            )
            if response.message.content:
                yield StreamEvent(kind="text_delta", text=response.message.content)
            yield StreamEvent(kind="done", response=response)
            return

        # No pool — native streaming path (unchanged behavior).
        # TS-T7 — short-circuit if a previous 429 hasn't reset.
        _check_rate_limit()
        try:
            async with self.client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    if text:
                        yield StreamEvent(kind="text_delta", text=text)
                final = await stream.get_final_message()
        except AnthropicRateLimitError as exc:
            _record_429(exc)
            raise

        yield StreamEvent(kind="done", response=self._parse_response(final))


__all__ = ["AnthropicProvider", "AnthropicProviderConfig"]
