"""SearchBackend ABC — the contract every search provider implements.

A `SearchBackend` is just `async search(query, max_results, timeout_s) ->
list[SearchHit]`. The wrapping `WebSearchTool` is responsible for
formatting hits into markdown for the agent; backends only deliver data.

Why this is small:
- Each provider's API is different (Brave is GET, Tavily is POST JSON,
  Exa is POST JSON with a different field name, etc.) — the ABC must
  not leak any provider's specifics.
- The agent never sees the backend distinction. Same `WebSearch` tool
  call, same markdown response shape. Provider switch is a config flip,
  not a tool-schema change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One search result — the lowest-common-denominator across all
    providers. Title + URL are required; snippet is best-effort."""

    title: str
    url: str
    snippet: str = ""


class SearchBackendError(Exception):
    """Raised by a backend when search fails in a way the user should see.

    Distinct from generic httpx errors so `WebSearchTool` can format these
    as friendly tool-result errors rather than tracebacks.
    """


class SearchBackend(ABC):
    """All search providers subclass this."""

    #: Stable id used in config + the BACKENDS registry.
    id: str = ""

    #: Env var that holds this provider's API key, if any. Empty string for
    #: providers that don't need a key (DDG).
    env_var: str = ""

    #: Human-friendly URL where users get a key. Empty for keyless backends.
    signup_url: str = ""

    @abstractmethod
    async def search(
        self,
        *,
        query: str,
        max_results: int,
        timeout_s: float,
    ) -> list[SearchHit]:
        """Return up to `max_results` hits. Raise `SearchBackendError` on
        recoverable failures (auth, no results, rate limit, etc.). Let
        unrecoverable errors (network, code bug) bubble — `WebSearchTool`
        catches both and returns a friendly tool-result either way."""

    def needs_api_key(self) -> bool:
        return bool(self.env_var)


__all__ = ["SearchBackend", "SearchBackendError", "SearchHit"]
