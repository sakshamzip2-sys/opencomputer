"""Fal tool — call fal.ai's REST API for image / video / audio / model gen.

Generic wrapper over `https://fal.run/<model_id>` — works with any fal.ai
model since they all share one POST shape:
    Authorization: Key <FAL_KEY>
    Content-Type: application/json
    body: model-specific JSON payload

The model name is part of the URL, not a parameter, so the agent passes
`model="fal-ai/flux/schnell"` plus a payload dict. We don't try to
validate the payload against any model's schema — that's fal.ai's job.

Get a key at https://fal.ai/dashboard/keys.

Args:
    model:    Fal model id (e.g. "fal-ai/flux/schnell", "fal-ai/whisper").
    payload:  Model-specific JSON dict. For text-to-image: {"prompt": "..."}.
    timeout_s: Request timeout. Default 120 (image gen can be slow).
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

FAL_API_BASE = "https://fal.run"
FAL_API_KEY_ENV = "FAL_KEY"
FAL_SIGNUP_URL = "https://fal.ai/dashboard/keys"
DEFAULT_TIMEOUT_S = 120.0


def _format_response(data: Any, max_chars: int = 4_000) -> str:
    """Pretty-print fal.ai's JSON response. Keeps image URLs prominent if present."""
    if not isinstance(data, dict):
        return json.dumps(data, indent=2)[:max_chars]
    lines: list[str] = []
    # Common fal patterns: top-level "images": [{"url": ...}, ...] or
    # "image": {"url": ...} or "audio_url" / "video_url".
    images = data.get("images")
    if isinstance(images, list):
        lines.append("## Images")
        for i, img in enumerate(images, 1):
            url = img.get("url") if isinstance(img, dict) else None
            if url:
                lines.append(f"{i}. {url}")
        lines.append("")
    image = data.get("image")
    if isinstance(image, dict) and image.get("url"):
        lines.append(f"## Image\n{image['url']}\n")
    for media_key in ("audio_url", "video_url"):
        if data.get(media_key):
            lines.append(f"## {media_key.replace('_', ' ').title()}\n{data[media_key]}\n")
    # Always include the full JSON for completeness — useful for non-media models.
    body = json.dumps(data, indent=2)
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n[truncated — {len(body) - max_chars} chars omitted]"
    lines.append("## Raw response")
    lines.append(body)
    return "\n".join(lines)


class FalTool(BaseTool):
    parallel_safe = True  # API calls are independent

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Fal",
            description=(
                "Call any model on fal.ai (image gen, video, audio, transcription, "
                'etc.). Pass `model` (e.g. "fal-ai/flux/schnell") and `payload` '
                "(model-specific JSON, usually {'prompt': '...'}). Requires FAL_KEY "
                "env var — get a key at https://fal.ai/dashboard/keys."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": (
                            "Fal model id (e.g. 'fal-ai/flux/schnell', "
                            "'fal-ai/whisper'). See https://fal.ai/models for the catalog."
                        ),
                    },
                    "payload": {
                        "type": "object",
                        "description": (
                            "Model-specific JSON payload. For text-to-image: "
                            '{"prompt": "a red apple"}. Other models have '
                            "their own input schema — check the model's page."
                        ),
                    },
                    "timeout_s": {
                        "type": "number",
                        "description": (
                            f"Request timeout in seconds. Default {DEFAULT_TIMEOUT_S} "
                            "(image gen can be slow on cold starts)."
                        ),
                    },
                },
                "required": ["model", "payload"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args: dict[str, Any] = call.arguments
        model = str(args.get("model", "")).strip()
        payload = args.get("payload")
        timeout_s = float(args.get("timeout_s", DEFAULT_TIMEOUT_S))

        if not model:
            return ToolResult(
                tool_call_id=call.id, content="Error: model is required", is_error=True
            )
        if not isinstance(payload, dict):
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: payload must be a JSON object (got {type(payload).__name__})",
                is_error=True,
            )

        api_key = os.environ.get(FAL_API_KEY_ENV, "").strip()
        if not api_key:
            return ToolResult(
                tool_call_id=call.id,
                content=(f"Error: {FAL_API_KEY_ENV} not set. Get a key at {FAL_SIGNUP_URL}."),
                is_error=True,
            )

        url = f"{FAL_API_BASE}/{model.lstrip('/')}"
        headers = {
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: fal.ai timed out after {timeout_s}s on {model}",
                is_error=True,
            )
        except httpx.HTTPError as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {type(e).__name__}: {e}",
                is_error=True,
            )

        if resp.status_code == 401:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: fal.ai 401 unauthorized — check {FAL_API_KEY_ENV}",
                is_error=True,
            )
        if resp.status_code == 422:
            # fal.ai returns the validation error JSON in the body
            try:
                detail = resp.json()
            except Exception:  # noqa: BLE001
                detail = resp.text
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: fal.ai 422 validation: {detail}",
                is_error=True,
            )
        if resp.status_code >= 400:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: fal.ai HTTP {resp.status_code}: {resp.text[:500]}",
                is_error=True,
            )

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: fal.ai returned non-JSON: {resp.text[:500]}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=f"# {model}\n\n{_format_response(data)}",
        )


__all__ = ["FalTool"]
