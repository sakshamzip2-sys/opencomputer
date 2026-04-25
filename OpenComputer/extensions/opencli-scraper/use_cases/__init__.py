"""OpenCLI Scraper — use-case function libraries.

These are domain helpers that compose the existing tools
(ScrapeRawTool / FetchProfileTool / MonitorPageTool) into higher-level
patterns. They are NOT registered as agent tools — they are callable
from tests and from other plugin code.

Phase C4 provides five use-case modules:
    - research_automation   — arXiv/Scholar/PubMed citation-graph helper
    - content_monitoring    — poll URL + hash diff (PageMonitor)
    - context_enrichment    — user-mention-triggered profile fetch
    - competitor_research   — first-party company-page scanner
    - market_signals        — HN + Reddit signal aggregator (legal-gated)

Re-exported entry points
------------------------
"""

from __future__ import annotations

from use_cases.competitor_research import (  # type: ignore[import-not-found]
    compare_companies,
    scan_company_page,
)
from use_cases.content_monitoring import (  # type: ignore[import-not-found]
    PageMonitor,
    monitor_loop,
)
from use_cases.context_enrichment import (  # type: ignore[import-not-found]
    enrich_mentions,
    extract_mentions,
    format_for_context,
)
from use_cases.market_signals import (  # type: ignore[import-not-found]
    MARKET_SIGNALS_LEGAL_NOTICE,
    MarketSignalsCollector,
)
from use_cases.research_automation import (  # type: ignore[import-not-found]
    build_citation_graph,
    fetch_arxiv_paper_metadata,
    search_by_topic,
)

__all__ = [
    # competitor_research
    "compare_companies",
    "scan_company_page",
    # content_monitoring
    "PageMonitor",
    "monitor_loop",
    # context_enrichment
    "enrich_mentions",
    "extract_mentions",
    "format_for_context",
    # market_signals
    "MARKET_SIGNALS_LEGAL_NOTICE",
    "MarketSignalsCollector",
    # research_automation
    "build_citation_graph",
    "fetch_arxiv_paper_metadata",
    "search_by_topic",
]
