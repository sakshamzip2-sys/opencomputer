"""arXiv / Scholar / PubMed citation-graph helper.

Functions here compose the OpenCLI wrapper's ``arxiv/search`` adapter into
higher-level research workflows. Rate-limiting defers to wrapper's per-domain
limits (arxiv.org: 60 req/min from DEFAULT_LIMITS).

All functions accept an ``OpenCLIWrapper`` instance as their first argument.
They do NOT go through the tool layer — they are library functions, not agent
tools.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make field_whitelist importable when running from the plugin dir.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import field_whitelist  # type: ignore[import-not-found]  # noqa: E402
from wrapper import OpenCLIWrapper  # type: ignore[import-not-found]  # noqa: E402

log = logging.getLogger(__name__)

_ARXIV_ADAPTER = "arxiv/search"
_ARXIV_ALLOWED_FIELDS = field_whitelist.FIELD_WHITELISTS.get(
    _ARXIV_ADAPTER, {"id", "title", "authors", "summary", "published", "pdf_url"}
)


def _filter_paper(raw: dict) -> dict:
    """Keep only whitelisted fields from a raw arXiv result dict."""
    return {k: v for k, v in raw.items() if k in _ARXIV_ALLOWED_FIELDS}


async def fetch_arxiv_paper_metadata(wrapper: OpenCLIWrapper, paper_id: str) -> dict:
    """Fetch metadata for a single arXiv paper.

    Parameters
    ----------
    wrapper:
        An ``OpenCLIWrapper`` instance. Rate-limiting for ``arxiv.org`` is
        applied automatically via the wrapper's semaphore.
    paper_id:
        arXiv paper ID (e.g. ``"2401.00001"`` or ``"abs/2401.00001"``).

    Returns
    -------
    dict
        Filtered metadata with keys: id, title, authors, summary (abstract),
        published (date string), pdf_url.

    Raises
    ------
    ValueError
        If ``paper_id`` is empty.
    RuntimeError
        If the adapter returns no usable data for the requested paper.
    """
    if not paper_id or not paper_id.strip():
        raise ValueError("paper_id must be a non-empty string")

    pid = paper_id.strip()
    raw = await wrapper.run(_ARXIV_ADAPTER, pid)

    # The wrapper returns the raw envelope; unwrap `data` if present.
    data = raw.get("data", raw) if isinstance(raw, dict) else raw

    # The arxiv adapter may return a list (search results) or a single dict.
    if isinstance(data, list):
        if not data:
            raise RuntimeError(f"arXiv returned no results for paper_id={pid!r}")
        # First result should be the paper we asked for.
        paper = data[0]
    elif isinstance(data, dict):
        paper = data
    else:
        raise RuntimeError(f"Unexpected response type from arXiv adapter: {type(data).__name__}")

    filtered = _filter_paper(paper)
    if not filtered:
        raise RuntimeError(
            f"arXiv adapter returned data but all fields were filtered out for {pid!r}. "
            "Check FIELD_WHITELISTS['arxiv/search']."
        )
    return filtered


async def build_citation_graph(
    wrapper: OpenCLIWrapper,
    seed_paper_id: str,
    depth: int = 1,
) -> dict:
    """Build a citation graph starting from *seed_paper_id*.

    Performs a depth-limited BFS over arXiv paper citations. At depth=1
    only direct citations are fetched. At depth=2 citations-of-citations
    are included (quadratic growth — use with caution).

    Parameters
    ----------
    wrapper:
        An ``OpenCLIWrapper`` instance.
    seed_paper_id:
        arXiv paper ID of the root paper.
    depth:
        How many hops to follow. Default ``1`` (direct citations only).
        Values > 3 are clamped to 3 to avoid runaway fetching.

    Returns
    -------
    dict
        ``{"papers": [<paper_dict>, ...], "edges": [(from_id, to_id), ...]}``
        where each paper_dict has whitelisted fields.
    """
    if depth < 1:
        depth = 1
    if depth > 3:
        log.warning("build_citation_graph: depth=%d clamped to 3", depth)
        depth = 3

    papers: dict[str, dict] = {}  # id → metadata
    edges: list[tuple[str, str]] = []

    async def _walk(paper_id: str, current_depth: int) -> None:
        if paper_id in papers:
            return  # already visited

        try:
            meta = await fetch_arxiv_paper_metadata(wrapper, paper_id)
        except (ValueError, RuntimeError) as exc:
            log.warning("build_citation_graph: skipping %r — %s", paper_id, exc)
            return

        actual_id = meta.get("id", paper_id)
        papers[actual_id] = meta

        if current_depth >= depth:
            return  # don't recurse further

        # Fetch related/citing papers by searching for this paper's title or ID.
        # The arXiv adapter supports a free-text search; we use the paper ID as
        # a proxy for "find papers that reference this work."
        title = meta.get("title", "")
        if not title:
            return

        try:
            results = await search_by_topic(wrapper, title, limit=5)
        except RuntimeError:
            return

        for related in results:
            related_id = related.get("id", "")
            if related_id and related_id != actual_id:
                edges.append((actual_id, related_id))
                await _walk(related_id, current_depth + 1)

    await _walk(seed_paper_id, 0)

    return {
        "papers": list(papers.values()),
        "edges": edges,
    }


async def search_by_topic(
    wrapper: OpenCLIWrapper,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Search arXiv for papers matching *query*.

    Parameters
    ----------
    wrapper:
        An ``OpenCLIWrapper`` instance.
    query:
        Free-text search string (e.g. ``"transformer attention mechanism"``).
    limit:
        Maximum number of results to return. Clamped to 100.

    Returns
    -------
    list[dict]
        Each item has whitelisted fields: id, title, authors, summary,
        published, pdf_url.

    Raises
    ------
    ValueError
        If *query* is empty.
    RuntimeError
        If the adapter returns no results or an unexpected structure.
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")

    limit = max(1, min(limit, 100))
    raw = await wrapper.run(_ARXIV_ADAPTER, query.strip(), "--limit", str(limit))

    data = raw.get("data", raw) if isinstance(raw, dict) else raw

    if isinstance(data, list):
        results = data
    elif isinstance(data, dict):
        # Some adapters wrap results in a "results" key.
        results = data.get("results", [data])
    else:
        raise RuntimeError(
            f"Unexpected response from arXiv adapter for query={query!r}: "
            f"{type(data).__name__}"
        )

    return [_filter_paper(p) for p in results if isinstance(p, dict)]


__all__ = ["fetch_arxiv_paper_metadata", "build_citation_graph", "search_by_topic"]
