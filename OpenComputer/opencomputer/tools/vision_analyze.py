"""VisionAnalyzeTool — first-class image-analysis tool.

Tier 1.B Tool 2 of 4 (per docs/refs/hermes-agent/2026-04-28-major-gaps.md
Tier 1.B). Closes the "agent has multimodal capability via clipboard
paste but no standalone tool" gap. After this, the agent can analyze an
image at a URL or base64 directly via ``VisionAnalyze(image_url=..., prompt=...)``
without the user having to paste the image into chat first.

Architecture: validates image source (SSRF guard on URLs, magic-byte
sniff to reject non-image content, hard size cap), base64-encodes,
sends a multimodal Anthropic Messages API request with the requested
prompt, returns the text response. Anthropic-only for MVP; OpenAI
multimodal can come in a follow-up commit if demand surfaces.
"""

from __future__ import annotations

import base64
import json
import os

import httpx

from opencomputer.security.url_safety import is_safe_url
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_PROMPT = "Describe this image in detail."
DEFAULT_TIMEOUT_S = 60.0
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB hard cap

# Magic bytes for common image formats. Used to verify a fetched URL
# actually serves an image (defense against an attacker pointing at
# HTML or text masquerading as an image).
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # WebP starts with RIFF, then WEBP at offset 8
)


def _make_async_client(timeout: float = DEFAULT_TIMEOUT_S) -> httpx.AsyncClient:
    """Test seam — replace with httpx.MockTransport in tests."""
    return httpx.AsyncClient(timeout=timeout)


def _sniff_image_type(data: bytes) -> str | None:
    if len(data) < 4:
        return None
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            if mime == "image/webp":
                # Verify WEBP marker at offset 8
                if len(data) >= 12 and data[8:12] == b"WEBP":
                    return mime
            else:
                return mime
    return None


class VisionAnalyzeTool(BaseTool):
    parallel_safe = True  # API call is independent

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="VisionAnalyze",
            description=(
                "Analyze an image at a URL or as base64 and return a text "
                "description. Supply EITHER `image_url` (HTTPS recommended; "
                "private/internal addresses are blocked) OR `image_base64` "
                "(raw base64, no data: prefix). Optionally provide `prompt` "
                "to steer the analysis (default: 'Describe this image in "
                "detail.'). Supports PNG, JPEG, GIF, WebP. Hard cap 10MB. "
                "Requires ANTHROPIC_API_KEY env var or constructor injection."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "image_url": {
                        "type": "string",
                        "description": "HTTPS URL of the image to analyze. SSRF-guarded.",
                    },
                    "image_base64": {
                        "type": "string",
                        "description": (
                            "Base64-encoded image bytes (raw, no `data:image/...,` "
                            "prefix). Use this when the image isn't reachable via URL."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Question or instruction to steer the analysis. Default: "
                            "'Describe this image in detail.'"
                        ),
                    },
                },
                # No `required` — it's an OR between url and base64, validated
                # at runtime.
            },
        )

    async def _fetch_image(self, url: str) -> tuple[bytes, str] | str:
        """Return (bytes, mime) on success; an error string on failure."""
        if not is_safe_url(url):
            return f"unsafe URL blocked by SSRF guard: {url}"
        try:
            async with _make_async_client() as client:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
                data = resp.content
        except httpx.HTTPError as e:
            return f"failed to fetch image: {type(e).__name__}: {e}"

        if len(data) > MAX_IMAGE_BYTES:
            return (
                f"image too large: {len(data)} bytes exceeds limit of "
                f"{MAX_IMAGE_BYTES} ({MAX_IMAGE_BYTES // 1024 // 1024} MB)"
            )

        mime = _sniff_image_type(data)
        if mime is None:
            return (
                "fetched content is not an image (magic-byte sniff failed). "
                "Verify the URL serves PNG, JPEG, GIF, or WebP."
            )
        return (data, mime)

    async def _call_anthropic(
        self, image_b64: str, mime: str, prompt: str, api_key: str
    ) -> str | tuple[str, bool]:
        """Return the response text on success, or (error, True) on failure."""
        body = {
            "model": self._model,
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }
        try:
            async with _make_async_client() as client:
                resp = await client.post(
                    ANTHROPIC_API_URL,
                    headers=headers,
                    content=json.dumps(body),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as e:
            return (f"vision API call failed: {type(e).__name__}: {e}", True)

        # Anthropic returns content blocks; extract text
        blocks = data.get("content", [])
        text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        if not text_parts:
            return ("vision API returned no text content", True)
        return "".join(text_parts)

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        image_url = args.get("image_url")
        image_b64 = args.get("image_base64")
        prompt = args.get("prompt") or DEFAULT_PROMPT

        if not image_url and not image_b64:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "must provide either `image_url` or `image_base64`"
                ),
                is_error=True,
            )

        # API key
        api_key = self._api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "no API key — set ANTHROPIC_API_KEY env var or pass "
                    "api_key= to VisionAnalyzeTool"
                ),
                is_error=True,
            )

        # Resolve image bytes + mime
        if image_url:
            fetched = await self._fetch_image(image_url)
            if isinstance(fetched, str):
                # Error message
                return ToolResult(
                    tool_call_id=call.id, content=fetched, is_error=True,
                )
            data, mime = fetched
            b64 = base64.b64encode(data).decode("ascii")
        else:
            try:
                data = base64.b64decode(image_b64, validate=True)
            except Exception as e:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"invalid base64: {type(e).__name__}: {e}",
                    is_error=True,
                )
            if len(data) > MAX_IMAGE_BYTES:
                return ToolResult(
                    tool_call_id=call.id,
                    content=(
                        f"image too large: {len(data)} bytes exceeds limit "
                        f"of {MAX_IMAGE_BYTES}"
                    ),
                    is_error=True,
                )
            mime = _sniff_image_type(data)
            if mime is None:
                return ToolResult(
                    tool_call_id=call.id,
                    content="base64 content is not an image (magic-byte sniff failed)",
                    is_error=True,
                )
            b64 = image_b64

        # Call vision API
        result = await self._call_anthropic(b64, mime, prompt, api_key)
        if isinstance(result, tuple):
            text, _is_err = result
            return ToolResult(
                tool_call_id=call.id, content=text, is_error=True,
            )
        return ToolResult(tool_call_id=call.id, content=result)


__all__ = ["VisionAnalyzeTool"]
