"""ImageGenerateTool — first-class image generation via fal.ai.

Tier 1.B Tool 3 of 4 (per docs/refs/hermes-agent/2026-04-28-major-gaps.md
Tier 1.B). Promotes the existing ``extensions/dev-tools/fal_tool.py``
into a core tool so the agent reaches for it by reflex when the user
asks for an image instead of describing one in prose.

Architecture: thin wrapper over ``https://fal.run/<model_id>`` (the
same shape the existing FalTool uses). Default model is FLUX schnell
(fast + cheap); the model can override per-call via the ``model`` arg.
The optional ``payload`` arg lets the model pass model-specific params
(image_size, num_images, etc.) without us having to maintain a schema
per FAL model.
"""

from __future__ import annotations

import json
import os

import httpx

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

FAL_API_BASE = "https://fal.run"
FAL_API_KEY_ENV = "FAL_KEY"
DEFAULT_MODEL = "fal-ai/flux/schnell"  # Fast (~1s) + cheap; good default.
DEFAULT_TIMEOUT_S = 120.0
MAX_RESPONSE_CHARS = 4_000


def _make_async_client(timeout: float = DEFAULT_TIMEOUT_S) -> httpx.AsyncClient:
    """Test seam — replace with httpx.MockTransport in tests."""
    return httpx.AsyncClient(timeout=timeout)


def _format_response(data: dict) -> str:
    """Pretty-print FAL's JSON response. Image URL goes prominently up top."""
    lines: list[str] = []

    images = data.get("images")
    if isinstance(images, list):
        lines.append("## Generated images")
        for i, img in enumerate(images, 1):
            url = img.get("url") if isinstance(img, dict) else None
            if url:
                lines.append(f"{i}. {url}")
        lines.append("")

    image = data.get("image")
    if isinstance(image, dict) and image.get("url"):
        lines.append(f"## Generated image\n{image['url']}\n")

    for media_key in ("audio_url", "video_url"):
        if data.get(media_key):
            label = media_key.replace("_", " ").title()
            lines.append(f"## {label}\n{data[media_key]}\n")

    body = json.dumps(data, indent=2)
    if len(body) > MAX_RESPONSE_CHARS:
        body = body[:MAX_RESPONSE_CHARS] + (
            f"\n[truncated — {len(body) - MAX_RESPONSE_CHARS} chars omitted]"
        )
    lines.append("## Raw response")
    lines.append(body)
    return "\n".join(lines)


class ImageGenerateTool(BaseTool):
    parallel_safe = True

    def __init__(
        self, api_key: str | None = None, default_model: str | None = None
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model or DEFAULT_MODEL

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="ImageGenerate",
            description=(
                "Generate an image from a text prompt via fal.ai. Default "
                "model is FLUX schnell (fast + cheap). Override `model` for "
                "specific FAL models — e.g. 'fal-ai/flux/dev' for higher "
                "quality, 'fal-ai/recraft-v3' for vector/illustration, "
                "'fal-ai/grok-imagine' for xAI-flavored output. Optional "
                "`payload` dict passes model-specific params (image_size, "
                "num_images, seed). Requires FAL_KEY env var — get one at "
                "https://fal.ai/dashboard/keys. Returns the image URL(s) "
                "directly; the agent should embed or pass them along."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Text description of the image to generate. "
                            "Be specific about subject, style, lighting, "
                            "composition for best results."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            f"FAL model id. Default '{DEFAULT_MODEL}'. "
                            "Examples: 'fal-ai/flux/dev', 'fal-ai/recraft-v3', "
                            "'fal-ai/grok-imagine', 'fal-ai/stable-diffusion-v3'."
                        ),
                    },
                    "payload": {
                        "type": "object",
                        "description": (
                            "Model-specific extra params merged into the "
                            "request body. Common keys: image_size "
                            "('square_hd', '1024x1024', etc.), num_images "
                            "(int), seed (int). Consult FAL docs for the "
                            "specific model."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        prompt = args.get("prompt", "").strip()
        model = (args.get("model") or self._default_model).strip()
        extra_payload = args.get("payload") or {}

        if not prompt:
            return ToolResult(
                tool_call_id=call.id,
                content="missing required argument: prompt",
                is_error=True,
            )
        if not isinstance(extra_payload, dict):
            return ToolResult(
                tool_call_id=call.id,
                content="payload must be an object (dict)",
                is_error=True,
            )

        api_key = self._api_key or os.environ.get(FAL_API_KEY_ENV)
        if not api_key:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"no API key — set {FAL_API_KEY_ENV} env var or pass "
                    "api_key= to ImageGenerateTool. Get one at "
                    "https://fal.ai/dashboard/keys"
                ),
                is_error=True,
            )

        body = {"prompt": prompt, **extra_payload}
        url = f"{FAL_API_BASE}/{model}"
        headers = {
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with _make_async_client() as client:
                resp = await client.post(
                    url, headers=headers, content=json.dumps(body),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"fal API error {e.response.status_code}: "
                    f"{e.response.text[:500]}"
                ),
                is_error=True,
            )
        except httpx.HTTPError as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"fal API call failed: {type(e).__name__}: {e}",
                is_error=True,
            )

        return ToolResult(tool_call_id=call.id, content=_format_response(data))


__all__ = ["ImageGenerateTool"]
