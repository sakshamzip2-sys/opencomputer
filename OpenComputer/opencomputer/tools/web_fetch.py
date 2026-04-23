"""WebFetch tool — fetch a URL and return clean text content.

Uses httpx for the request and BeautifulSoup for HTML stripping. Returns the
visible text of the page with script/style/nav noise removed. Truncates to
`max_chars` (default 8000) so the agent doesn't blow its context on a giant
article it only needs the gist of.
"""

from __future__ import annotations

from typing import Any

import httpx
from bs4 import BeautifulSoup

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

DEFAULT_USER_AGENT = "OpenComputer/0.1 (+https://github.com/sakshamzip2-sys/opencomputer)"
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_CHARS = 8_000

#: Tags whose contents add no information to a textual rendering — strip them
#: BEFORE extracting text so we don't get a wall of inline JS or CSS.
_STRIP_TAGS = ("script", "style", "noscript", "iframe", "svg")


def _html_to_text(html: str) -> str:
    """Render HTML to its visible text. Collapses whitespace per-line, preserves
    paragraph boundaries with double newlines."""
    soup = BeautifulSoup(html, "html.parser")
    for tag_name in _STRIP_TAGS:
        for el in soup.find_all(tag_name):
            el.decompose()
    # Use a separator so adjacent block-level elements don't run together.
    raw = soup.get_text(separator="\n", strip=True)
    # Collapse runs of blank lines that BeautifulSoup tends to leave behind.
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    return "\n".join(lines)


class WebFetchTool(BaseTool):
    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="WebFetch",
            description=(
                "Fetch a URL and return its main text content (HTML stripped). "
                "Use this when you need to read a web page — articles, docs, "
                "blog posts, API references. Truncates long pages."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch. Must be http(s).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            "Cap the returned text at this many characters. "
                            f"Default {DEFAULT_MAX_CHARS}."
                        ),
                    },
                    "timeout_s": {
                        "type": "number",
                        "description": (
                            f"Request timeout in seconds. Default {DEFAULT_TIMEOUT_S}."
                        ),
                    },
                },
                "required": ["url"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args: dict[str, Any] = call.arguments
        url = str(args.get("url", "")).strip()
        max_chars = int(args.get("max_chars", DEFAULT_MAX_CHARS))
        timeout_s = float(args.get("timeout_s", DEFAULT_TIMEOUT_S))

        if not url:
            return ToolResult(tool_call_id=call.id, content="Error: url is required", is_error=True)
        if not url.startswith(("http://", "https://")):
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: url must start with http:// or https:// (got {url!r})",
                is_error=True,
            )

        try:
            async with httpx.AsyncClient(
                timeout=timeout_s,
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            ) as client:
                resp = await client.get(url)
        except httpx.TimeoutException:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: timed out after {timeout_s}s fetching {url}",
                is_error=True,
            )
        except httpx.HTTPError as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {type(e).__name__}: {e}",
                is_error=True,
            )

        if resp.status_code >= 400:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: HTTP {resp.status_code} from {url}",
                is_error=True,
            )

        ct = resp.headers.get("content-type", "").lower()
        # Plain text / JSON: return as-is. HTML: strip first.
        body = _html_to_text(resp.text) if "html" in ct else resp.text

        if len(body) > max_chars:
            body = body[:max_chars] + (
                f"\n\n[truncated — {len(body) - max_chars} chars omitted; "
                "raise max_chars to see more]"
            )

        return ToolResult(
            tool_call_id=call.id,
            content=f"# {url}\n\n{body}",
        )


__all__ = ["WebFetchTool"]
