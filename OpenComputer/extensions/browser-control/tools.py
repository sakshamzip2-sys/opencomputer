"""Browser control tools — 5 BaseTool implementations.

All tools are async. Each invocation gets a fresh isolated browser session
unless OPENCOMPUTER_BROWSER_PROFILE_PATH is set.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from extensions.browser_control.browser import (
    BrowserError,
    PageSnapshot,
    click_element,
    fill_input,
    navigate_and_snapshot,
    scrape_url,
)

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


def _result_from_snapshot(call: ToolCall, snap: PageSnapshot) -> ToolResult:
    if snap.error:
        return ToolResult(tool_call_id=call.id, content=f"Error: {snap.error}", is_error=True)
    payload = {
        "url": snap.url,
        "title": snap.title,
        "accessibility_tree": snap.accessibility_tree,
        "text_content": snap.text_content,
    }
    return ToolResult(tool_call_id=call.id, content=json.dumps(payload))


class BrowserNavigateTool(BaseTool):
    """Open URL in a fresh isolated browser; return text snapshot."""

    consent_tier: int = 2
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="browser.navigate",
            tier_required=ConsentTier.EXPLICIT,
            human_description="Open a URL in an isolated browser session.",
        ),
    )

    def __init__(self, *, consent_gate: Any | None = None, sandbox: Any | None = None, audit: Any | None = None) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="browser_navigate",
            description=(
                "Navigate to a URL in an isolated headless browser. Returns "
                "title, URL after redirects, accessibility tree (text), and "
                "visible text content. Use for verifying a page loaded, "
                "extracting text from JavaScript-rendered pages, or as a "
                "starting step before click/fill. Sessions are isolated by "
                "default — no shared cookies or login. Cross-platform via "
                "Playwright (chromium). Under F1 ConsentGate (EXPLICIT tier)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Target URL (http/https)"},
                },
                "required": ["url"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        url = call.arguments.get("url", "")
        if not url:
            return ToolResult(tool_call_id=call.id, content="Error: missing url", is_error=True)
        try:
            snap = await navigate_and_snapshot(url)
        except BrowserError as exc:
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        return _result_from_snapshot(call, snap)


class BrowserClickTool(BaseTool):
    """Navigate to URL + click a CSS selector + return post-click snapshot."""

    consent_tier: int = 2
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="browser.click",
            tier_required=ConsentTier.EXPLICIT,
            human_description="Click an element on a page.",
        ),
    )

    def __init__(self, *, consent_gate: Any | None = None, sandbox: Any | None = None, audit: Any | None = None) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="browser_click",
            description=(
                "Navigate to a URL and click an element by CSS selector. "
                "Returns the post-click page snapshot. Use to traverse a "
                "site to a deeper page (e.g. click 'Sign In', click a "
                "specific link). CAUTION: this can submit forms — use "
                "browser_navigate first to inspect the page if unsure. "
                "Under F1 ConsentGate (EXPLICIT tier)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "selector": {"type": "string", "description": "CSS selector for the element to click"},
                },
                "required": ["url", "selector"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        url = call.arguments.get("url", "")
        selector = call.arguments.get("selector", "")
        if not url or not selector:
            return ToolResult(tool_call_id=call.id, content="Error: missing url or selector", is_error=True)
        try:
            snap = await click_element(url, selector)
        except BrowserError as exc:
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        return _result_from_snapshot(call, snap)


class BrowserFillTool(BaseTool):
    """Navigate + fill a text input + return snapshot."""

    consent_tier: int = 2
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="browser.fill",
            tier_required=ConsentTier.EXPLICIT,
            human_description="Fill a text input on a page.",
        ),
    )

    def __init__(self, *, consent_gate: Any | None = None, sandbox: Any | None = None, audit: Any | None = None) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="browser_fill",
            description=(
                "Navigate to a URL and fill a text input by CSS selector. "
                "Returns the post-fill snapshot. CAUTION: never fill "
                "passwords or credit-card numbers — this tool submits them "
                "to the page's JS, where they may be transmitted. Use only "
                "for benign text (search queries, names, etc.). Under F1 "
                "ConsentGate (EXPLICIT tier)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "selector": {"type": "string"},
                    "value": {"type": "string", "description": "Text to fill"},
                },
                "required": ["url", "selector", "value"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        url = call.arguments.get("url", "")
        selector = call.arguments.get("selector", "")
        value = call.arguments.get("value", "")
        if not url or not selector:
            return ToolResult(tool_call_id=call.id, content="Error: missing url or selector", is_error=True)
        try:
            snap = await fill_input(url, selector, value)
        except BrowserError as exc:
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        return _result_from_snapshot(call, snap)


class BrowserSnapshotTool(BaseTool):
    """Snapshot a URL — alias for navigate_and_snapshot, no side effects."""

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="browser.snapshot",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Read-only browser snapshot of a URL.",
        ),
    )

    def __init__(self, *, consent_gate: Any | None = None, sandbox: Any | None = None, audit: Any | None = None) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="browser_snapshot",
            description=(
                "Read-only snapshot of a URL: title, accessibility tree, "
                "visible text. Differs from browser_navigate in INTENT — "
                "snapshot is for scraping / inspecting; navigate is the "
                "first step of an interactive session. Under F1 "
                "ConsentGate (IMPLICIT tier — read-only)."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        url = call.arguments.get("url", "")
        if not url:
            return ToolResult(tool_call_id=call.id, content="Error: missing url", is_error=True)
        try:
            snap = await navigate_and_snapshot(url)
        except BrowserError as exc:
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        return _result_from_snapshot(call, snap)


class BrowserScrapeTool(BaseTool):
    """Scrape text from a URL with optional CSS selector."""

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="browser.scrape",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Read-only scrape of a URL with optional CSS selector.",
        ),
    )

    def __init__(self, *, consent_gate: Any | None = None, sandbox: Any | None = None, audit: Any | None = None) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="browser_scrape",
            description=(
                "Scrape text from a URL. Optional css_selector returns just "
                "the matched elements' text. Without selector, returns full "
                "visible text. Use for extracting data from JS-rendered "
                "pages where WebFetch can't see the rendered content. "
                "Under F1 ConsentGate (IMPLICIT tier — read-only)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "css_selector": {"type": "string", "description": "Optional CSS selector"},
                },
                "required": ["url"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        url = call.arguments.get("url", "")
        css = call.arguments.get("css_selector") or None
        if not url:
            return ToolResult(tool_call_id=call.id, content="Error: missing url", is_error=True)
        try:
            snap = await scrape_url(url, css)
        except BrowserError as exc:
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        return _result_from_snapshot(call, snap)


ALL_TOOLS = [
    BrowserNavigateTool,
    BrowserClickTool,
    BrowserFillTool,
    BrowserSnapshotTool,
    BrowserScrapeTool,
]

__all__ = [
    "ALL_TOOLS",
    "BrowserNavigateTool",
    "BrowserClickTool",
    "BrowserFillTool",
    "BrowserSnapshotTool",
    "BrowserScrapeTool",
]
