"""Browser tool — fetch a JS-rendered page via Playwright.

Differs from WebFetch (httpx + BeautifulSoup) in one important way: this
tool runs a real headless browser, so single-page apps that build the
DOM with JavaScript actually work. Cost: Playwright is heavy (~300MB
of browser binaries on first run). Hence it's an OPTIONAL import — if
Playwright isn't installed, the tool returns a friendly error instead
of crashing the plugin load.

To enable:
    pip install playwright
    playwright install chromium

Args:
    url:        The page to load. http(s) only.
    wait_for:   Optional CSS selector to await before extracting text.
                Useful for SPAs that load content asynchronously.
    timeout_s:  How long to wait for page load. Default 30.
    max_chars:  Cap on returned text. Default 8000 (matches WebFetch).
"""

from __future__ import annotations

from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_CHARS = 8_000

#: Detect Playwright at module load — failing here would break plugin
#: registration. We defer the actual import to execute() so the plugin
#: loads fine even when Playwright is missing.
try:
    import playwright  # noqa: F401

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + (
        f"\n\n[truncated — {len(text) - max_chars} chars omitted; raise max_chars to see more]"
    )


class BrowserTool(BaseTool):
    parallel_safe = False  # one browser context per call — keep serialized

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Browser",
            description=(
                "Fetch a JavaScript-rendered web page via a headless browser. "
                "Use when WebFetch returns empty or near-empty content "
                "(usually a sign of a single-page app that builds the DOM with "
                "JS). Slower + heavier than WebFetch — prefer WebFetch first. "
                "Requires `pip install playwright && playwright install chromium`."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch. http(s) only.",
                    },
                    "wait_for": {
                        "type": "string",
                        "description": (
                            "Optional CSS selector to await before extracting "
                            "text. Useful for SPAs that load content "
                            "asynchronously."
                        ),
                    },
                    "timeout_s": {
                        "type": "number",
                        "description": (f"Total timeout in seconds. Default {DEFAULT_TIMEOUT_S}."),
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (f"Cap on returned text. Default {DEFAULT_MAX_CHARS}."),
                    },
                },
                "required": ["url"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        if not _PLAYWRIGHT_AVAILABLE:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "Error: Playwright is not installed. Install with:\n"
                    "  pip install playwright\n"
                    "  playwright install chromium\n"
                    "Or use WebFetch (httpx + BeautifulSoup) for static pages."
                ),
                is_error=True,
            )

        args: dict[str, Any] = call.arguments
        url = str(args.get("url", "")).strip()
        wait_for = str(args.get("wait_for", "")).strip()
        timeout_s = float(args.get("timeout_s", DEFAULT_TIMEOUT_S))
        max_chars = int(args.get("max_chars", DEFAULT_MAX_CHARS))

        if not url:
            return ToolResult(tool_call_id=call.id, content="Error: url is required", is_error=True)
        if not url.startswith(("http://", "https://")):
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: url must start with http:// or https:// (got {url!r})",
                is_error=True,
            )

        # Lazy import — only triggered when the tool is actually called and
        # Playwright is available.
        from playwright.async_api import async_playwright

        timeout_ms = int(timeout_s * 1000)
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                    if wait_for:
                        await page.wait_for_selector(wait_for, timeout=timeout_ms)
                    # `inner_text` of the body strips JS / style and gives the
                    # rendered, visible text the user would see.
                    body_text = await page.inner_text("body")
                finally:
                    await browser.close()
        except Exception as e:  # noqa: BLE001
            # Playwright errors are heterogeneous; fold them all into a
            # friendly message. Keep stack traces out of tool results.
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {type(e).__name__}: {e}",
                is_error=True,
            )

        body = _truncate(body_text or "", max_chars)
        return ToolResult(tool_call_id=call.id, content=f"# {url}\n\n{body}")


__all__ = ["BrowserTool"]
