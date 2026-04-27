"""WebSearch tool — config-selectable provider chain.

Phase 12d.2 refactor: was a single DDG path; now picks one of 5 backends
(DDG / Brave / Tavily / Exa / Firecrawl) per `config.tools.web_search.provider`.
The agent invokes `WebSearch(query=...)`; the backend distinction is
invisible in the tool schema.

Per-call override: the model can pass `provider="brave"` to swap for
one query. Useful when the configured default doesn't have a key set
and the model wants to retry with a known-keyless one.

Backends live in `opencomputer/tools/search_backends/` — one file each,
all behind `SearchBackend`. To add a new provider: drop another file in
that directory and add a row to `BACKENDS`.
"""

from __future__ import annotations

from typing import Any

import httpx

from opencomputer.agent.config import default_config
from opencomputer.security.url_safety import is_safe_url
from opencomputer.tools.search_backends import (
    BACKEND_IDS,
    SearchBackendError,
    SearchHit,
    get_backend,
)
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_RESULTS = 10


def _format_hits_as_markdown(query: str, hits: list[SearchHit], provider: str) -> str:
    lines = [f"# Results for: {query}  [provider: {provider}]\n"]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. **{h.title}**")
        lines.append(f"   {h.url}")
        if h.snippet:
            lines.append(f"   {h.snippet}")
        lines.append("")
    return "\n".join(lines)


class WebSearchTool(BaseTool):
    parallel_safe = True

    def __init__(self, default_provider: str | None = None) -> None:
        # Default provider is read from config, but tests + downstream
        # callers may pin one explicitly via constructor override.
        if default_provider is None:
            cfg = default_config()
            default_provider = cfg.tools.web_search.provider
        self._default_provider = default_provider

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="WebSearch",
            description=(
                "Search the web; return the top results as a markdown list. Use this "
                "when you need information that's likely beyond the model's training "
                "cutoff — news, recent docs, current prices, today's headlines. The "
                "result is a list of links and snippets; pair with WebFetch to read "
                "the full text of a chosen result. Provider chain (DDG, Brave, Tavily, "
                "Exa, Firecrawl) is configured via `opencomputer config set tools."
                "web_search.provider <name>`; the model can override per-call via "
                "`provider`. For library/SDK docs, prefer the Context7 MCP — it "
                "returns curated, version-aware docs better than a generic web search."
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
                    "provider": {
                        "type": "string",
                        "enum": list(BACKEND_IDS),
                        "description": (
                            "Override the configured default provider for "
                            "this one query. Useful when the default needs an "
                            "API key that isn't set."
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
        provider = str(args.get("provider", "") or self._default_provider).strip()

        if not query:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: query is required",
                is_error=True,
            )

        try:
            backend = get_backend(provider)
        except KeyError as e:
            return ToolResult(tool_call_id=call.id, content=f"Error: {e}", is_error=True)

        try:
            hits = await backend.search(query=query, max_results=max_results, timeout_s=timeout_s)
        except SearchBackendError as e:
            # Provider-friendly error (auth missing, rate limit, etc.).
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {e}",
                is_error=True,
            )
        except httpx.TimeoutException:
            return ToolResult(
                tool_call_id=call.id,
                content=(f"Error: timed out after {timeout_s}s searching {query!r} via {provider}"),
                is_error=True,
            )
        except httpx.HTTPError as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {type(e).__name__}: {e}",
                is_error=True,
            )

        # SSRF guard (TS-T4): drop result hits that resolve to private/internal
        # IPs or cloud metadata endpoints. Search providers occasionally surface
        # links into private VPN-routed namespaces; keeping them in the list
        # invites the model to follow up with a WebFetch that would (rightly)
        # be blocked, wasting a turn. Filtering at the search layer is cheaper.
        hits = [h for h in hits if is_safe_url(h.url)]

        if not hits:
            return ToolResult(
                tool_call_id=call.id,
                content=f"No results for {query!r} via {provider}",
            )
        return ToolResult(
            tool_call_id=call.id,
            content=_format_hits_as_markdown(query, hits, provider),
        )


__all__ = ["WebSearchTool"]
