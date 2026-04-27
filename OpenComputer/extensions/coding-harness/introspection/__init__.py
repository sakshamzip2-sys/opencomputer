"""Native cross-platform introspection tools (replaces oi_bridge subprocess wrapper)."""

from __future__ import annotations

from .tools import (
    ExtractScreenTextTool,
    ListAppUsageTool,
    ListRecentFilesTool,
    ReadClipboardOnceTool,
    ScreenshotTool,
)

ALL_TOOLS = [
    ListAppUsageTool,
    ReadClipboardOnceTool,
    ScreenshotTool,
    ExtractScreenTextTool,
    ListRecentFilesTool,
]

__all__ = [
    "ALL_TOOLS",
    "ExtractScreenTextTool",
    "ListAppUsageTool",
    "ListRecentFilesTool",
    "ReadClipboardOnceTool",
    "ScreenshotTool",
]
