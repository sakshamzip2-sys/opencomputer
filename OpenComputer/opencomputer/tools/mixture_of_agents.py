"""MixtureOfAgentsTool — voting across N OpenRouter models for high-stakes reasoning.

Tier 1.B Tool 4 of 4 (per docs/refs/hermes-agent/2026-04-28-major-gaps.md
Tier 1.B + 1.E re-evaluation). The original 2026-04-22 inventory verdict
was 'skip' because Delegate covers single-spawn — but Delegate is one
worker on one task, MoA is N workers on the SAME prompt voting. Different
primitive. Pairs naturally with the cheap-route gate (route easy turns
to one model, route flagged-hard turns to MoA).

Architecture: parallel `asyncio.gather` of OpenRouter Chat Completions
calls, one per model. Returns all responses formatted side-by-side
(no automated voting in MVP — let the calling agent decide). Partial
failures don't fail the whole call; failed models are noted.

Why OpenRouter: single API, ~200+ models, single auth. Could later
extend to multi-provider (Anthropic + OpenAI + xAI in parallel) but
OpenRouter is the simplest first cut.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_TIMEOUT_S = 120.0
MAX_MODELS = 8  # bound parallel calls — cost guardrail


def _make_async_client(timeout: float = DEFAULT_TIMEOUT_S) -> httpx.AsyncClient:
    """Test seam — replace with httpx.MockTransport in tests."""
    return httpx.AsyncClient(timeout=timeout)


async def _call_one_model(
    client: httpx.AsyncClient,
    model: str,
    prompt: str,
    api_key: str,
    max_tokens: int,
) -> tuple[str, str | None]:
    """Return (model_id, response_text). On failure: (model_id, None)."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = await client.post(
            OPENROUTER_API_URL,
            headers=headers,
            content=json.dumps(body),
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            return (model, None)
        return (model, choices[0].get("message", {}).get("content"))
    except httpx.HTTPError:
        return (model, None)


class MixtureOfAgentsTool(BaseTool):
    parallel_safe = True  # API calls are independent

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="MixtureOfAgents",
            description=(
                "Send the same prompt to N different LLMs in parallel and "
                "return all responses side-by-side for comparison. Useful "
                "for high-stakes reasoning where single-model output is "
                "risky (financial advice, medical questions, irreversible "
                "operations, security-critical decisions). The calling "
                "agent decides how to weight/synthesize the responses; "
                "this tool does NOT do automated voting. Uses OpenRouter "
                "as the unified backend (200+ models, single API). "
                "Requires OPENROUTER_API_KEY env var."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The question/task to send to all models.",
                    },
                    "models": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "OpenRouter model ids — e.g. "
                            "['anthropic/claude-opus-4-7', "
                            "'openai/gpt-5', 'google/gemini-2.5-pro']. "
                            f"Capped at {MAX_MODELS} to bound cost."
                        ),
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Max tokens per response. Default 1024.",
                    },
                },
                "required": ["prompt", "models"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        prompt = (args.get("prompt") or "").strip()
        models = args.get("models") or []
        max_tokens = int(args.get("max_tokens") or 1024)

        if not prompt:
            return ToolResult(
                tool_call_id=call.id,
                content="missing required argument: prompt",
                is_error=True,
            )
        if not isinstance(models, list) or not models:
            return ToolResult(
                tool_call_id=call.id,
                content="missing or empty required argument: models (list of model ids)",
                is_error=True,
            )
        if len(models) > MAX_MODELS:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"too many models: {len(models)} exceeds limit of "
                    f"{MAX_MODELS}. Pick the most informative subset."
                ),
                is_error=True,
            )

        api_key = self._api_key or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "no API key — set OPENROUTER_API_KEY env var or pass "
                    "api_key= to MixtureOfAgentsTool. Get one at "
                    "https://openrouter.ai/keys"
                ),
                is_error=True,
            )

        async with _make_async_client() as client:
            results = await asyncio.gather(
                *[
                    _call_one_model(client, m, prompt, api_key, max_tokens)
                    for m in models
                ]
            )

        # Format: one section per model
        lines: list[str] = [f"# Mixture-of-Agents results ({len(models)} models)\n"]
        succeeded = 0
        for model_id, text in results:
            if text is None:
                lines.append(f"## {model_id}\n[error: model call failed or returned empty]\n")
            else:
                succeeded += 1
                lines.append(f"## {model_id}\n{text}\n")

        lines.append(f"\n_Summary: {succeeded}/{len(models)} models responded._")
        return ToolResult(tool_call_id=call.id, content="\n".join(lines))


__all__ = ["MixtureOfAgentsTool"]
