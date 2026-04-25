"""Market-signal aggregator: HackerNews + Reddit.

Higher-risk use case — requires its own consent tier in Phase 4.
Do NOT enable without legal review.

This module is intentionally separate from the lower-risk use cases so
that Phase 4's ConsentGate can apply a distinct consent prompt and
audit trail. ``MARKET_SIGNALS_LEGAL_NOTICE`` is the text Session A's
Phase 4 will surface in the consent prompt.

Design notes
------------
* ``MarketSignalsCollector`` is stateful: each ``collect_from_*`` call
  appends to internal lists. ``aggregate()`` merges everything and
  computes simple keyword frequency for ``trending_keywords``.
* ``since_ts`` is a UNIX timestamp (float). Results with
  ``created_utc < since_ts`` are filtered out.
"""

from __future__ import annotations

import collections
import logging
import re
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import field_whitelist  # type: ignore[import-not-found]  # noqa: E402
from wrapper import OpenCLIWrapper  # type: ignore[import-not-found]  # noqa: E402

log = logging.getLogger(__name__)

# ── Legal gate ─────────────────────────────────────────────────────────────────

MARKET_SIGNALS_LEGAL_NOTICE = """
╔══════════════════════════════════════════════════════════════════════╗
║              MARKET SIGNALS — LEGAL NOTICE (Phase 4)               ║
╠══════════════════════════════════════════════════════════════════════╣
║  This use case collects publicly available discussion data from     ║
║  HackerNews and Reddit for the purpose of detecting market trends.  ║
║                                                                      ║
║  By enabling this feature you confirm that:                         ║
║    1. You will comply with HackerNews and Reddit terms of service.  ║
║    2. You will NOT use collected data for securities market          ║
║       manipulation, insider trading, or any activity prohibited      ║
║       under applicable financial regulations.                        ║
║    3. You accept sole responsibility for downstream use of the       ║
║       aggregated signals produced by this module.                    ║
║    4. You have obtained legal review appropriate to your             ║
║       jurisdiction before using this data in a production system.   ║
║                                                                      ║
║  THIS MODULE IS DISABLED BY DEFAULT. Do not enable without legal    ║
║  review. See F6 plan §C4 and Session A Phase 4 for consent wiring.  ║
╚══════════════════════════════════════════════════════════════════════╝
""".strip()

# ── Adapters ───────────────────────────────────────────────────────────────────

_HN_ADAPTER = "hackernews/user"  # closest available; real posts use a search adapter
_REDDIT_POSTS_ADAPTER = "reddit/posts"
_HN_WHITELIST = field_whitelist.FIELD_WHITELISTS.get("hackernews/user", set())
_REDDIT_WHITELIST = field_whitelist.FIELD_WHITELISTS.get("reddit/posts", set())

# Stopwords for keyword extraction — simple English set.
_STOPWORDS: frozenset[str] = frozenset(
    [
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to",
        "for", "of", "with", "by", "from", "is", "are", "was", "were",
        "be", "been", "being", "have", "has", "had", "do", "does", "did",
        "will", "would", "could", "should", "may", "might", "shall",
        "not", "no", "nor", "so", "yet", "both", "either", "neither",
        "each", "few", "more", "most", "other", "some", "such", "than",
        "too", "very", "just", "if", "then", "than", "that", "this",
        "these", "those", "it", "its", "i", "we", "they", "he", "she",
        "you", "me", "him", "her", "us", "them", "my", "our", "your",
        "his", "their", "what", "which", "who", "whom", "how", "when",
        "where", "why", "all", "any", "as", "up", "about", "into",
        "through", "during", "before", "after", "above", "below",
    ]
)


def _extract_keywords(texts: list[str], top_n: int = 10) -> list[str]:
    """Return the *top_n* most-frequent non-stopword tokens from *texts*."""
    counter: collections.Counter[str] = collections.Counter()
    word_re = re.compile(r"\b[a-zA-Z]{3,}\b")
    for text in texts:
        for word in word_re.findall(text.lower()):
            if word not in _STOPWORDS:
                counter[word] += 1
    return [word for word, _ in counter.most_common(top_n)]


class MarketSignalsCollector:
    """Stateful aggregator for market signals from public forums.

    Instantiate once per collection session. Call ``collect_from_hn`` and/or
    ``collect_from_reddit`` as many times as needed, then call ``aggregate``
    to merge everything.

    .. warning::
        This class must not be used without displaying
        ``MARKET_SIGNALS_LEGAL_NOTICE`` to the user first (enforced in
        Phase 4's ConsentGate).
    """

    def __init__(self) -> None:
        self._hn_signals: list[dict] = []
        self._reddit_signals: list[dict] = []

    # ── Collection ─────────────────────────────────────────────────────────────

    async def collect_from_hn(
        self,
        wrapper: OpenCLIWrapper,
        query: str,
        since_ts: float,
    ) -> list[dict]:
        """Scrape HackerNews for posts mentioning *query* since *since_ts*.

        Parameters
        ----------
        wrapper:
            An ``OpenCLIWrapper`` instance.
        query:
            Keyword / phrase to search for (passed as adapter argument).
        since_ts:
            UNIX timestamp. Posts older than this are excluded.

        Returns
        -------
        list[dict]
            Filtered HN post dicts (whitelisted fields only).
        """
        try:
            raw = await wrapper.run(_HN_ADAPTER, query, "--since", str(int(since_ts)))
        except Exception as exc:
            log.warning("collect_from_hn: adapter call failed — %s", exc)
            return []

        data = raw.get("data", raw) if isinstance(raw, dict) else raw

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("results", data.get("items", [data]))
        else:
            items = []

        signals = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # Apply since_ts filter if created is available.
            created = item.get("created", item.get("created_utc", 0))
            try:
                if float(created) < since_ts:
                    continue
            except (TypeError, ValueError):
                pass  # no timestamp → include item

            filtered = {k: v for k, v in item.items() if k in _HN_WHITELIST}
            filtered["_source"] = "hackernews"
            signals.append(filtered)

        self._hn_signals.extend(signals)
        return signals

    async def collect_from_reddit(
        self,
        wrapper: OpenCLIWrapper,
        subreddit: str,
        since_ts: float,
    ) -> list[dict]:
        """Scrape a subreddit for posts since *since_ts*.

        Parameters
        ----------
        wrapper:
            An ``OpenCLIWrapper`` instance.
        subreddit:
            Subreddit name without the ``r/`` prefix (e.g. ``"technology"``).
        since_ts:
            UNIX timestamp. Posts older than this are excluded.

        Returns
        -------
        list[dict]
            Filtered Reddit post dicts (whitelisted fields only).
        """
        try:
            raw = await wrapper.run(_REDDIT_POSTS_ADAPTER, subreddit, "--since", str(int(since_ts)))
        except Exception as exc:
            log.warning("collect_from_reddit: adapter call failed — %s", exc)
            return []

        data = raw.get("data", raw) if isinstance(raw, dict) else raw

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("results", data.get("items", [data]))
        else:
            items = []

        signals = []
        for item in items:
            if not isinstance(item, dict):
                continue
            created = item.get("created_utc", 0)
            try:
                if float(created) < since_ts:
                    continue
            except (TypeError, ValueError):
                pass

            filtered = {k: v for k, v in item.items() if k in _REDDIT_WHITELIST}
            filtered["_source"] = "reddit"
            signals.append(filtered)

        self._reddit_signals.extend(signals)
        return signals

    # ── Aggregation ────────────────────────────────────────────────────────────

    def aggregate(self) -> dict:
        """Merge all collected signals and compute trending keywords.

        Returns
        -------
        dict
            ``{"total_signals": int, "by_source": {"hackernews": [...],
               "reddit": [...]}, "trending_keywords": list[str]}``
        """
        all_signals = self._hn_signals + self._reddit_signals

        # Collect text for keyword extraction.
        texts: list[str] = []
        for sig in all_signals:
            for field in ("title", "body", "text", "summary", "comment"):
                val = sig.get(field)
                if isinstance(val, str) and val:
                    texts.append(val)

        trending = _extract_keywords(texts, top_n=10)

        return {
            "total_signals": len(all_signals),
            "by_source": {
                "hackernews": list(self._hn_signals),
                "reddit": list(self._reddit_signals),
            },
            "trending_keywords": trending,
        }


__all__ = [
    "MARKET_SIGNALS_LEGAL_NOTICE",
    "MarketSignalsCollector",
]
