"""WebSearch tool — query DuckDuckGo's HTML endpoint and return top results.

DuckDuckGo's HTML interface is intentionally scraping-friendly (no JS required,
no API key) so it's the path of least resistance for a default web-search tool.
Returns a markdown list of (title, url, snippet) for the top N results.

For commercial / heavier use, swap to Brave Search via a `--brave-api-key`
config option later — same return shape, different fetch.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DEFAULT_USER_AGENT = "OpenComputer/0.1 (+https://github.com/sakshamzip2-sys/opencomputer)"
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_RESULTS = 10


def _unwrap_ddg_redirect(href: str) -> str:
    """DDG wraps result links in /l/?uddg=<encoded-url>&rut=...; unwrap to the
    real URL when that pattern is present, else return href as-is."""
    if not href:
        return href
    parsed = urlparse(href)
    if "duckduckgo" in (parsed.netloc or "") and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return href


def _parse_results(html: str, max_results: int) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, str]] = []
    for result in soup.select(".result"):
        title_el = result.select_one(".result__title a") or result.select_one("h2 a")
        snippet_el = result.select_one(".result__snippet")
        if title_el is None:
            continue
        title = title_el.get_text(" ", strip=True)
        href = _unwrap_ddg_redirect(title_el.get("href", "") or "")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        if not title or not href:
            continue
        out.append({"title": title, "url": href, "snippet": snippet})
        if len(out) >= max_results:
            break
    return out


class WebSearchTool(BaseTool):
    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="WebSearch",
            description=(
                "Search the web via DuckDuckGo and return the top results as "
                "a markdown list. Use this when you need current information "
                "outside training data (news, recent docs, prices). Pair with "
                "WebFetch to read a specific result in detail."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            f"How many results to return. Default {DEFAULT_MAX_RESULTS}."
                        ),
                    },
                    "timeout_s": {
                        "type": "number",
                        "description": (
                            f"Request timeout in seconds. Default {DEFAULT_TIMEOUT_S}."
                        ),
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args: dict[str, Any] = call.arguments
        query = str(args.get("query", "")).strip()
        max_results = int(args.get("max_results", DEFAULT_MAX_RESULTS))
        timeout_s = float(args.get("timeout_s", DEFAULT_TIMEOUT_S))

        if not query:
            return ToolResult(
                tool_call_id=call.id, content="Error: query is required", is_error=True
            )

        try:
            async with httpx.AsyncClient(
                timeout=timeout_s,
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            ) as client:
                resp = await client.post(DDG_HTML_URL, data={"q": query})
        except httpx.TimeoutException:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: timed out after {timeout_s}s searching for {query!r}",
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
                content=f"Error: HTTP {resp.status_code} from DuckDuckGo",
                is_error=True,
            )

        results = _parse_results(resp.text, max_results)
        if not results:
            return ToolResult(
                tool_call_id=call.id,
                content=f"No results for {query!r}",
            )

        lines = [f"# Results for: {query}\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. **{r['title']}**")
            lines.append(f"   {r['url']}")
            if r["snippet"]:
                lines.append(f"   {r['snippet']}")
            lines.append("")
        return ToolResult(tool_call_id=call.id, content="\n".join(lines))


__all__ = ["WebSearchTool"]
