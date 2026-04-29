"""RecallScreen — agent-callable tool returning recent screen captures.

The agent invokes RecallScreen when it needs to reason about screen
history beyond the latest capture (which is always available via the
DynamicInjectionProvider). Returns formatted text with most-recent
first ordering and optional ``window_seconds`` time filter.

F1 ConsentGate at IMPLICIT tier — same as ScreenshotTool.
"""
from __future__ import annotations

from typing import ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

from .ring_buffer import ScreenRingBuffer


class RecallScreenTool(BaseTool):
    """Return recent screen captures from the ring buffer."""

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.recall_screen",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Return recent screen-OCR captures from the ring buffer.",
        ),
    )

    def __init__(self, *, ring_buffer: ScreenRingBuffer) -> None:
        self._ring = ring_buffer

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="RecallScreen",
            description=(
                "Return the last N screen-OCR captures from the screen-awareness "
                "ring buffer, most-recent first. Use when you need to reason about "
                "what was on screen across multiple recent moments — e.g. comparing "
                "before-and-after states, recalling a window the user mentioned. "
                "The most recent capture is always already available via the "
                "<screen_context> system reminder; this tool fetches older entries. "
                "Returns formatted text. Empty buffer returns an explanatory note."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "Max captures to return. Default 5.",
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "window_seconds": {
                        "type": "number",
                        "description": (
                            "Optional time-window filter — only return captures "
                            "from the last N seconds. Default unbounded."
                        ),
                        "minimum": 0,
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        n = int(call.arguments.get("n", 5))
        n = min(max(n, 1), 20)
        window = call.arguments.get("window_seconds")
        try:
            window_f = float(window) if window is not None else None
        except (TypeError, ValueError):
            window_f = None

        captures = list(self._ring.most_recent(n=n, window_seconds=window_f))
        if not captures:
            return ToolResult(
                tool_call_id=call.id,
                content="(no screen captures in the requested window)",
            )
        lines: list[str] = []
        for cap in captures:
            ts = f"{cap.captured_at:.1f}"
            lines.append(
                f"--- captured_at={ts} trigger={cap.trigger} sha={cap.sha256[:8]}\n"
                f"{cap.text}"
            )
        body = "\n\n".join(lines)
        return ToolResult(tool_call_id=call.id, content=body)


__all__ = ["RecallScreenTool"]
