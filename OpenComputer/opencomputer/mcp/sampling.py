"""T71 — MCP ``sampling/createMessage`` host bridge.

Per the MCP spec, an MCP server can ask the host (us) to run an LLM
completion via ``sampling/createMessage``. The host wires a
``sampling_callback`` into ``ClientSession`` that translates the
request into a host-side LLM call.

This bridges to :func:`opencomputer.agent.aux_llm.complete_text`, which
already handles provider resolution + auth + the T68 fallback chain —
so an MCP server's sampling request automatically benefits from the
operator's full credential and fallback config.

Currently the bridge supports text-only sampling. Image / video content
in the request would require ``complete_vision`` / ``complete_video``;
deferred until an MCP server emerges that actually exercises that path.
"""

from __future__ import annotations

import logging
from typing import Any

from opencomputer.agent.aux_llm import complete_text

logger = logging.getLogger(__name__)


def make_sampling_callback():
    """Return an MCP ``SamplingFnT`` that drives the host LLM via aux_llm.

    Lazy-imports MCP types so this module loads cleanly even when the
    SDK isn't installed (e.g. in pure unit-test environments without
    MCP). The returned callable will fail at first call if MCP types
    can't be resolved — which is the right time to surface the issue.
    """

    async def _callback(context: Any, params: Any) -> Any:
        # Lazy-import so module import doesn't depend on the MCP SDK.
        from mcp.types import (
            CreateMessageResult,
            ErrorData,
            TextContent,
        )

        # Translate MCP messages → aux_llm dict shape. Text-only for now;
        # non-text content blocks are silently dropped with a debug log.
        messages: list[dict[str, str]] = []
        for sm in params.messages:
            content = sm.content
            text = getattr(content, "text", None)
            if not isinstance(text, str):
                logger.debug(
                    "MCP sampling: dropping non-text content from %s message",
                    sm.role,
                )
                continue
            messages.append({"role": sm.role, "content": text})

        system_prompt = getattr(params, "systemPrompt", "") or ""
        max_tokens = int(getattr(params, "maxTokens", 1024) or 1024)
        temperature_raw = getattr(params, "temperature", None)
        temperature = float(temperature_raw) if temperature_raw is not None else 1.0

        try:
            text_out = await complete_text(
                messages=messages,
                system=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP sampling/createMessage failed: %s", exc)
            return ErrorData(
                code=-32603,
                message=f"sampling failed: {exc}",
            )

        return CreateMessageResult(
            role="assistant",
            content=TextContent(type="text", text=text_out or ""),
            model="opencomputer-aux",
            stopReason="endTurn",
        )

    return _callback


__all__ = ["make_sampling_callback"]
