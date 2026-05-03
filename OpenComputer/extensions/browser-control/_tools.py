"""Browser control tools — 5 BaseTool implementations.

All tools are async. Each invocation gets a fresh isolated browser session
unless OPENCOMPUTER_BROWSER_PROFILE_PATH is set.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from _browser_session import (  # type: ignore[import-not-found]
    BrowserError,
    PageSnapshot,
    click_element,
    fill_input,
    get_console_messages,
    get_images,
    go_back,
    navigate_and_snapshot,
    press_key,
    scrape_url,
    scroll_page,
    vision_screenshot,
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


# ─── Hermes-parity Batch 1 (2026-05-01) — 6 new browser tools ───────


class BrowserScrollTool(BaseTool):
    """Scroll up/down/top/bottom on a navigated page."""

    consent_tier: int = 2
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="browser.scroll",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Scroll a navigated page up/down/top/bottom.",
        ),
    )

    def __init__(self, *, consent_gate: Any | None = None, sandbox: Any | None = None, audit: Any | None = None) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="browser_scroll",
            description=(
                "Scroll a page up/down/top/bottom and return a fresh "
                "snapshot. Useful for triggering lazy-loaded content. "
                "direction is 'up'|'down'|'top'|'bottom'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "top", "bottom"],
                    },
                    "amount_px": {"type": "integer"},
                },
                "required": ["url"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        url = call.arguments.get("url", "")
        direction = call.arguments.get("direction", "down")
        amount_px = int(call.arguments.get("amount_px") or 500)
        if not url:
            return ToolResult(tool_call_id=call.id, content="Error: missing url", is_error=True)
        try:
            snap = await scroll_page(url, direction=direction, amount_px=amount_px)
        except BrowserError as exc:
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        return _result_from_snapshot(call, snap)


class BrowserBackTool(BaseTool):
    """Navigate, then go back, return post-back snapshot."""

    consent_tier: int = 2
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="browser.navigate",
            tier_required=ConsentTier.EXPLICIT,
            human_description="Navigate then go back in browser history.",
        ),
    )

    def __init__(self, *, consent_gate: Any | None = None, sandbox: Any | None = None, audit: Any | None = None) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="browser_back",
            description=(
                "Open a URL, then click the browser-back button and return "
                "the post-back snapshot. Useful when an automated flow needs "
                "to undo a click. Each call starts a fresh isolated browser "
                "session — no persistent history across calls. Under F1 "
                "ConsentGate (EXPLICIT tier — navigation)."
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
            snap = await go_back(url)
        except BrowserError as exc:
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        return _result_from_snapshot(call, snap)


class BrowserPressTool(BaseTool):
    """Press a key on a navigated page."""

    consent_tier: int = 2
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="browser.fill",
            tier_required=ConsentTier.EXPLICIT,
            human_description="Press a key in a navigated page.",
        ),
    )

    def __init__(self, *, consent_gate: Any | None = None, sandbox: Any | None = None, audit: Any | None = None) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="browser_press",
            description=(
                "Open URL, optionally focus a CSS selector, press a key. "
                "key is any Playwright keyname (Enter, Escape, Tab, "
                "ArrowDown, etc)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "key": {"type": "string"},
                    "selector": {"type": "string"},
                },
                "required": ["url", "key"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        url = call.arguments.get("url", "")
        key = call.arguments.get("key", "")
        selector = call.arguments.get("selector") or None
        if not url or not key:
            return ToolResult(tool_call_id=call.id, content="Error: url + key required", is_error=True)
        try:
            snap = await press_key(url, key, selector=selector)
        except BrowserError as exc:
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        return _result_from_snapshot(call, snap)


class BrowserGetImagesTool(BaseTool):
    """List <img> elements on a page (read-only)."""

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="browser.scrape",
            tier_required=ConsentTier.IMPLICIT,
            human_description="List images on a page.",
        ),
    )

    def __init__(self, *, consent_gate: Any | None = None, sandbox: Any | None = None, audit: Any | None = None) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="browser_get_images",
            description=(
                "Open a URL; return a list of every <img> element on the "
                "rendered page with (src, alt, width, height). Capped at "
                "max_images (default 20) to keep tool results sane. Useful "
                "for picking images to feed into a vision model, or for "
                "verifying alt-text coverage. Read-only — IMPLICIT tier."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_images": {"type": "integer"},
                },
                "required": ["url"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        url = call.arguments.get("url", "")
        max_images = int(call.arguments.get("max_images") or 20)
        if not url:
            return ToolResult(tool_call_id=call.id, content="Error: missing url", is_error=True)
        try:
            result = await get_images(url, max_images=max_images)
        except BrowserError as exc:
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        if "error" in result:
            return ToolResult(tool_call_id=call.id, content=f"Error: {result['error']}", is_error=True)
        return ToolResult(tool_call_id=call.id, content=json.dumps(result))


class BrowserVisionTool(BaseTool):
    """Take a base64 PNG screenshot for vision models."""

    consent_tier: int = 2
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="browser.screenshot",
            tier_required=ConsentTier.EXPLICIT,
            human_description="Capture a rendered screenshot of a page.",
        ),
    )

    def __init__(self, *, consent_gate: Any | None = None, sandbox: Any | None = None, audit: Any | None = None) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="browser_vision",
            description=(
                "Open a URL and return a base64-encoded PNG screenshot of "
                "the rendered viewport. Useful when a vision model needs "
                "the visual layout (charts, screenshots, designs) rather "
                "than the text. Returns image_base64 + image_format=png + "
                "image_size_bytes. Caller wraps the b64 in an image content "
                "block. Under F1 ConsentGate (EXPLICIT tier)."
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
            result = await vision_screenshot(url)
        except BrowserError as exc:
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        if "error" in result:
            return ToolResult(tool_call_id=call.id, content=f"Error: {result['error']}", is_error=True)
        return ToolResult(tool_call_id=call.id, content=json.dumps(result))


class BrowserConsoleTool(BaseTool):
    """Capture console.log/warn/error from a page during load."""

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="browser.scrape",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Capture console messages from a page.",
        ),
    )

    def __init__(self, *, consent_gate: Any | None = None, sandbox: Any | None = None, audit: Any | None = None) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="browser_console",
            description=(
                "Open URL; capture console.log/warn/error + pageerror "
                "events emitted during the first ~500ms of load. Useful "
                "for debugging JS errors invisible in the rendered DOM."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_messages": {"type": "integer"},
                },
                "required": ["url"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        url = call.arguments.get("url", "")
        max_messages = int(call.arguments.get("max_messages") or 50)
        if not url:
            return ToolResult(tool_call_id=call.id, content="Error: missing url", is_error=True)
        try:
            result = await get_console_messages(url, max_messages=max_messages)
        except BrowserError as exc:
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        if "error" in result:
            return ToolResult(tool_call_id=call.id, content=f"Error: {result['error']}", is_error=True)
        return ToolResult(tool_call_id=call.id, content=json.dumps(result))


ALL_TOOLS = [
    BrowserNavigateTool,
    BrowserClickTool,
    BrowserFillTool,
    BrowserSnapshotTool,
    BrowserScrapeTool,
    # Hermes-parity Batch 1 (2026-05-01)
    BrowserScrollTool,
    BrowserBackTool,
    BrowserPressTool,
    BrowserGetImagesTool,
    BrowserVisionTool,
    BrowserConsoleTool,
]

__all__ = [
    "ALL_TOOLS",
    "BrowserNavigateTool",
    "BrowserClickTool",
    "BrowserFillTool",
    "BrowserSnapshotTool",
    "BrowserScrapeTool",
    "BrowserScrollTool",
    "BrowserBackTool",
    "BrowserPressTool",
    "BrowserGetImagesTool",
    "BrowserVisionTool",
    "BrowserConsoleTool",
]
