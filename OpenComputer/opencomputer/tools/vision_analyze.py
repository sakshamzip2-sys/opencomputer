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
import os
from pathlib import Path

import httpx

from opencomputer.agent.anthropic_client import build_anthropic_async_client
from opencomputer.agent.config import _home
from opencomputer.security.url_safety import is_safe_url
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_PROMPT = "Describe this image in detail."
DEFAULT_TIMEOUT_S = 60.0


# Re-exported for backwards compat with existing tests that patch
# ``opencomputer.tools.vision_analyze._build_anthropic_async_client``.
# New callers should import from ``opencomputer.agent.anthropic_client``.
_build_anthropic_async_client = build_anthropic_async_client
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


def _is_safe_image_path(path: Path) -> bool:
    """True iff ``path`` resolves under ``<profile_home>/tool_result_storage/``.

    This is the safe-set for ``image_path``: anything the agent's own
    tools wrote to disk (screenshots, browser snapshots, persisted
    oversize outputs) lives here, and it's the only directory we
    allow this tool to read from. Arbitrary file reads via ``image_path``
    would be a privilege escalation — the agent has no business reading
    ``/etc/shadow`` or the user's home through this surface.

    Resolved via ``Path.resolve()`` so symlink-traversal tricks
    (``<storage>/../etc/passwd``) collapse to their real path before
    the prefix check. ``relative_to`` raises ``ValueError`` when the
    resolved path isn't under the safe root — caught and reported as
    unsafe.
    """
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        # Resolve can raise on broken symlinks or permission errors.
        return False
    safe_root = (_home() / "tool_result_storage").resolve()
    try:
        resolved.relative_to(safe_root)
    except ValueError:
        return False
    return True


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


async def analyze_image_bytes(
    *,
    image_b64: str,
    mime: str,
    prompt: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1024,
) -> str | tuple[str, bool]:
    """Send a base64-encoded image + prompt to Anthropic vision; return text.

    Goes through the SAME ``AsyncAnthropic`` SDK + same auth resolution
    as the chat layer (extensions/anthropic-provider/provider.py). One
    code path to Anthropic, not two: any future fix to base-URL or
    auth-mode handling that lands on the chat side carries over here
    via :func:`_build_anthropic_async_client`.

    Returns the text response on success, or ``(error_string, True)`` on
    failure. The 2-tuple signature matches the legacy
    ``VisionAnalyzeTool._call_anthropic`` so error-path branches stay
    identical between the helper and its wrapper.
    """
    client = _build_anthropic_async_client(api_key)
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
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
        )
    except Exception as e:  # noqa: BLE001 — surface any SDK error as text
        return (f"vision API call failed: {type(e).__name__}: {e}", True)

    # Anthropic SDK returns objects with a ``content`` list of typed blocks.
    text_parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        block_type = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if block_type != "text":
            continue
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            text_parts.append(str(text))
    if not text_parts:
        return ("vision API returned no text content", True)
    return "".join(text_parts)


class VisionAnalyzeTool(BaseTool):
    parallel_safe = True  # API call is independent
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="VisionAnalyze",
            description=(
                "Analyze an image at a URL, file path, or as base64 and return "
                "a text description. Supply EXACTLY ONE of: `image_url` (HTTPS "
                "recommended; private/internal addresses are blocked), "
                "`image_path` (absolute path to a file inside "
                "<profile_home>/tool_result_storage/ — typically the path "
                "returned by the screenshot tool or a browser snapshot), or "
                "`image_base64` (raw base64, no data: prefix). Prefer "
                "`image_path` when an upstream tool already wrote the image "
                "to disk: it avoids round-tripping ~280K tokens of base64 "
                "through your context. Optionally provide `prompt` to steer "
                "the analysis (default: 'Describe this image in detail.'). "
                "Supports PNG, JPEG, GIF, WebP. Hard cap 10MB. Requires "
                "ANTHROPIC_API_KEY env var or constructor injection."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "image_url": {
                        "type": "string",
                        "description": "HTTPS URL of the image to analyze. SSRF-guarded.",
                    },
                    "image_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to an image file inside "
                            "<profile_home>/tool_result_storage/. Typically the "
                            "path the screenshot tool surfaced. Paths outside "
                            "that directory are rejected for security."
                        ),
                    },
                    "image_base64": {
                        "type": "string",
                        "description": (
                            "Base64-encoded image bytes (raw, no `data:image/...,` "
                            "prefix). Use this when the image isn't reachable via "
                            "URL or path."
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
                # No `required` — it's an OR between url, path, base64,
                # validated at runtime.
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
        """Return the response text on success, or (error, True) on failure.

        Thin wrapper around :func:`analyze_image_bytes` so existing tests
        that monkey-patch this method keep working. New callers should
        use the module-level helper directly — no class context required.
        """
        return await analyze_image_bytes(
            image_b64=image_b64, mime=mime, prompt=prompt,
            api_key=api_key, model=self._model,
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        image_url = args.get("image_url")
        image_path = args.get("image_path")
        image_b64 = args.get("image_base64")
        prompt = args.get("prompt") or DEFAULT_PROMPT

        provided = [s for s in (image_url, image_path, image_b64) if s]
        if not provided:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "must provide one of `image_url`, `image_path`, or "
                    "`image_base64`"
                ),
                is_error=True,
            )
        if len(provided) > 1:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "exactly ONE of `image_url`, `image_path`, `image_base64` "
                    "must be set; got multiple"
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
        elif image_path:
            path = Path(image_path)
            if not _is_safe_image_path(path):
                return ToolResult(
                    tool_call_id=call.id,
                    content=(
                        f"image_path rejected: {image_path!r} is outside the "
                        f"safe set (<profile_home>/tool_result_storage/). "
                        f"Tools that produce images write there; arbitrary "
                        f"file reads via this tool are not permitted."
                    ),
                    is_error=True,
                )
            try:
                data = path.read_bytes()
            except OSError as exc:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"failed to read image_path {image_path!r}: {exc}",
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
                    content=(
                        f"image_path content is not an image "
                        f"(magic-byte sniff failed): {image_path!r}"
                    ),
                    is_error=True,
                )
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


__all__ = ["VisionAnalyzeTool", "analyze_image_bytes"]
