"""WebFetch tool — fetch a URL and return clean text content.

Uses httpx for the request and BeautifulSoup for HTML stripping. Returns the
visible text of the page with script/style/nav noise removed. Truncates to
`max_chars` (default 8000) so the agent doesn't blow its context on a giant
article it only needs the gist of.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from opencomputer.security.url_safety import is_safe_url
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


#: URL patterns that suggest the page is an article (auto-mode trigger).
_ARTICLE_HOST_RE = re.compile(
    r"(?:medium\.com|substack\.com|^blog\.|/blog/|/article/|/articles/|/news/|/posts/|/post/|hackernews|techcrunch|wired)",
    re.IGNORECASE,
)


def _is_likely_article_url(url: str) -> bool:
    """Heuristic: does this URL look like a news article / blog post?"""
    parsed = urlparse(url)
    if _ARTICLE_HOST_RE.search(parsed.netloc):
        return True
    return bool(_ARTICLE_HOST_RE.search(parsed.path))


def _html_to_article(html: str) -> str:
    """Extract just the article body using Mozilla's Readability algorithm.

    Returns empty string if extraction fails or yields too little content
    (caller can fall back to full text). Imports readability lazily so
    web_fetch import doesn't pay the lxml load cost when the readability
    branch is never triggered.
    """
    try:
        from readability import Document  # readability-lxml

        doc = Document(html)
        article_html = doc.summary(html_partial=True) or ""
        if len(article_html) < 50:
            return ""
        return _html_to_text(article_html)
    except Exception:  # noqa: BLE001
        return ""


class WebFetchTool(BaseTool):
    parallel_safe = True
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="WebFetch",
            description=(
                "Fetch a URL over HTTP(S) and return the visible text with HTML chrome "
                "(scripts, styles, nav) stripped. Use this when you need to read an "
                "article, doc page, blog post, or API reference end-to-end. Pair with "
                "WebSearch — search returns links, fetch reads one. CAUTION: the page "
                "is truncated to `max_chars` (default 8000) so very long pages lose "
                "their tail; pass a larger cap if needed. Don't use WebFetch as a "
                "browser — it can't run JS. For dynamic content, render via the "
                "chrome-devtools or playwright MCP instead."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
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
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "full", "readability"],
                        "description": (
                            "auto = readability for article URLs, full otherwise. "
                            "full = strip nav/script/style (existing behaviour). "
                            "readability = extract article body only. "
                            "Default 'auto'."
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

        # SSRF guard (TS-T4): pre-check the URL before any network round-trip.
        # Blocks private IPs, cloud metadata endpoints, DNS-resolution failures.
        if not is_safe_url(url):
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Blocked: URL {url} fails SSRF safety check "
                    "(private IP, cloud metadata, or DNS resolution failure)."
                ),
                is_error=True,
            )

        async def _validate_redirect(response: httpx.Response) -> None:
            """Re-validate every redirect target — DNS rebinding / chained
            redirects could otherwise reach private space after the initial
            check passed."""
            if response.is_redirect:
                location = response.headers.get("location", "")
                if location and location.startswith(("http://", "https://")) and not is_safe_url(location):
                    raise httpx.RequestError(
                        f"Blocked redirect to unsafe URL: {location}",
                        request=response.request,
                    )

        try:
            async with httpx.AsyncClient(
                timeout=timeout_s,
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_USER_AGENT},
                event_hooks={"response": [_validate_redirect]},
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
        # Plain text / JSON: return as-is. HTML: render per `mode`.
        if "html" in ct:
            mode = str(args.get("mode", "auto")).lower()
            if mode not in ("auto", "full", "readability"):
                mode = "auto"
            if mode == "auto":
                mode = "readability" if _is_likely_article_url(url) else "full"
            if mode == "readability":
                body = _html_to_article(resp.text)
                if not body:  # graceful fallback when readability returns nothing
                    body = _html_to_text(resp.text)
            else:  # full
                body = _html_to_text(resp.text)
        else:
            body = resp.text

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
