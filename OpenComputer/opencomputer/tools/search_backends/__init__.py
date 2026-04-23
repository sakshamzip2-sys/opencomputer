"""Search backend registry — one provider per file, all behind one ABC.

Adding a new search provider means:
1. Create `opencomputer/tools/search_backends/<name>.py` with a class
   that subclasses `SearchBackend`.
2. Add it to `BACKENDS` below.
3. Document its env-var key requirement.

That's it. `web_search.py` picks one via config and calls
`backend.search(...)`. No `if/elif` chain ever grows.
"""

from __future__ import annotations

from opencomputer.tools.search_backends.base import (
    SearchBackend,
    SearchBackendError,
    SearchHit,
)
from opencomputer.tools.search_backends.brave import BraveBackend
from opencomputer.tools.search_backends.ddg import DuckDuckGoBackend
from opencomputer.tools.search_backends.exa import ExaBackend
from opencomputer.tools.search_backends.firecrawl import FirecrawlBackend
from opencomputer.tools.search_backends.tavily import TavilyBackend

#: Provider id → backend class. Add a new row + a new file under this dir.
BACKENDS: dict[str, type[SearchBackend]] = {
    "ddg": DuckDuckGoBackend,
    "brave": BraveBackend,
    "tavily": TavilyBackend,
    "exa": ExaBackend,
    "firecrawl": FirecrawlBackend,
}

#: User-facing list for `--help` / catalog output. Keep in sync with BACKENDS.
BACKEND_IDS: tuple[str, ...] = tuple(BACKENDS.keys())


def get_backend(provider: str) -> SearchBackend:
    """Instantiate a backend by id. Raises KeyError on unknown."""
    if provider not in BACKENDS:
        raise KeyError(f"unknown search provider {provider!r}. Available: {', '.join(BACKEND_IDS)}")
    return BACKENDS[provider]()


__all__ = [
    "BACKENDS",
    "BACKEND_IDS",
    "SearchBackend",
    "SearchBackendError",
    "SearchHit",
    "get_backend",
]
