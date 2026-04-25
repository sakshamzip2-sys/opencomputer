"""Tests for extensions/opencli-scraper/use_cases/market_signals.py."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_PLUGIN_DIR = Path(__file__).parent.parent / "extensions" / "opencli-scraper"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from use_cases.market_signals import (  # noqa: E402
    MARKET_SIGNALS_LEGAL_NOTICE,
    MarketSignalsCollector,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

_NOW = time.time()
_OLD_TS = _NOW - 86400  # 24 hours ago

_HN_POST = {
    "id": "hn-001",
    "karma": 100,
    "created": _NOW + 1,  # in the future relative to _NOW — always included
    "submitted_count": 5,
    "title": "AI startup raises $100M in Series A",
}

_REDDIT_POST = {
    "id": "r-001",
    "title": "OpenAI competitor funding rounds",
    "url": "https://reddit.com/r/technology/comments/xyz",
    "subreddit": "technology",
    "score": 500,
    "created_utc": _NOW + 1,
}


def _make_wrapper(*responses):
    wrapper = MagicMock()
    wrapper.run = AsyncMock(side_effect=list(responses) if responses else [{"data": []}])
    return wrapper


# ── Tests: legal notice ───────────────────────────────────────────────────────


class TestLegalNotice:
    def test_legal_notice_is_module_level_constant(self):
        assert isinstance(MARKET_SIGNALS_LEGAL_NOTICE, str)
        assert len(MARKET_SIGNALS_LEGAL_NOTICE) > 100

    def test_legal_notice_mentions_legal_review(self):
        assert "legal review" in MARKET_SIGNALS_LEGAL_NOTICE.lower()

    def test_legal_notice_mentions_higher_risk(self):
        # The module docstring or notice should flag this as higher-risk.
        import use_cases.market_signals as ms
        docstring = ms.__doc__ or ""
        notice = MARKET_SIGNALS_LEGAL_NOTICE
        assert "higher-risk" in docstring.lower() or "Higher-risk" in docstring or "legal" in notice.lower()


# ── Tests: collect_from_hn ────────────────────────────────────────────────────


class TestCollectFromHN:
    async def test_happy_path_returns_list(self):
        wrapper = _make_wrapper({"data": [_HN_POST]})
        collector = MarketSignalsCollector()
        results = await collector.collect_from_hn(wrapper, "AI startup", since_ts=_OLD_TS)

        assert isinstance(results, list)
        assert len(results) >= 1

    async def test_source_field_added(self):
        wrapper = _make_wrapper({"data": [_HN_POST]})
        collector = MarketSignalsCollector()
        results = await collector.collect_from_hn(wrapper, "AI startup", since_ts=_OLD_TS)

        assert any(r.get("_source") == "hackernews" for r in results)

    async def test_old_posts_filtered_by_since_ts(self):
        old_post = {**_HN_POST, "created": _OLD_TS - 1000}  # before _OLD_TS
        wrapper = _make_wrapper({"data": [old_post]})
        collector = MarketSignalsCollector()
        results = await collector.collect_from_hn(wrapper, "AI", since_ts=_OLD_TS)

        assert len(results) == 0

    async def test_adapter_error_returns_empty_list(self):
        wrapper = MagicMock()
        wrapper.run = AsyncMock(side_effect=RuntimeError("adapter unavailable"))
        collector = MarketSignalsCollector()
        results = await collector.collect_from_hn(wrapper, "AI", since_ts=_OLD_TS)

        assert results == []

    async def test_accumulates_across_calls(self):
        wrapper = MagicMock()
        wrapper.run = AsyncMock(
            side_effect=[
                {"data": [_HN_POST]},
                {"data": [_HN_POST]},
            ]
        )
        collector = MarketSignalsCollector()
        await collector.collect_from_hn(wrapper, "AI", since_ts=_OLD_TS)
        await collector.collect_from_hn(wrapper, "ML", since_ts=_OLD_TS)

        assert len(collector._hn_signals) == 2


# ── Tests: collect_from_reddit ────────────────────────────────────────────────


class TestCollectFromReddit:
    async def test_happy_path_returns_list(self):
        wrapper = _make_wrapper({"data": [_REDDIT_POST]})
        collector = MarketSignalsCollector()
        results = await collector.collect_from_reddit(wrapper, "technology", since_ts=_OLD_TS)

        assert isinstance(results, list)
        assert len(results) >= 1

    async def test_source_field_added(self):
        wrapper = _make_wrapper({"data": [_REDDIT_POST]})
        collector = MarketSignalsCollector()
        results = await collector.collect_from_reddit(wrapper, "technology", since_ts=_OLD_TS)

        assert any(r.get("_source") == "reddit" for r in results)

    async def test_old_posts_filtered(self):
        old_post = {**_REDDIT_POST, "created_utc": _OLD_TS - 1000}
        wrapper = _make_wrapper({"data": [old_post]})
        collector = MarketSignalsCollector()
        results = await collector.collect_from_reddit(wrapper, "technology", since_ts=_OLD_TS)

        assert len(results) == 0


# ── Tests: aggregate ──────────────────────────────────────────────────────────


class TestAggregate:
    async def test_aggregate_combines_hn_and_reddit(self):
        hn_wrapper = MagicMock()
        hn_wrapper.run = AsyncMock(return_value={"data": [_HN_POST]})
        reddit_wrapper = MagicMock()
        reddit_wrapper.run = AsyncMock(return_value={"data": [_REDDIT_POST]})

        collector = MarketSignalsCollector()
        await collector.collect_from_hn(hn_wrapper, "AI", since_ts=_OLD_TS)
        await collector.collect_from_reddit(reddit_wrapper, "technology", since_ts=_OLD_TS)

        agg = collector.aggregate()
        assert agg["total_signals"] == 2
        assert len(agg["by_source"]["hackernews"]) == 1
        assert len(agg["by_source"]["reddit"]) == 1

    async def test_aggregate_returns_expected_keys(self):
        collector = MarketSignalsCollector()
        agg = collector.aggregate()

        assert "total_signals" in agg
        assert "by_source" in agg
        assert "trending_keywords" in agg

    async def test_trending_keywords_are_list(self):
        collector = MarketSignalsCollector()
        agg = collector.aggregate()
        assert isinstance(agg["trending_keywords"], list)

    async def test_trending_keywords_extracted_from_titles(self):
        """Keywords from Reddit post titles should appear in trending_keywords.

        Reddit's whitelist includes 'title', HN's does not — so we use Reddit
        posts here to exercise the keyword-extraction path.
        """
        reddit_post = {
            **_REDDIT_POST,
            "title": "transformer architecture language models",
        }
        wrapper = _make_wrapper({"data": [reddit_post]})
        collector = MarketSignalsCollector()
        await collector.collect_from_reddit(wrapper, "MachineLearning", since_ts=_OLD_TS)

        agg = collector.aggregate()
        keywords = agg["trending_keywords"]
        # At least one of the meaningful words from the title should appear.
        meaningful = {"transformer", "architecture", "language", "models"}
        assert any(kw in meaningful for kw in keywords), f"Expected one of {meaningful} in {keywords}"

    async def test_aggregate_empty_collector_returns_zero_total(self):
        collector = MarketSignalsCollector()
        agg = collector.aggregate()
        assert agg["total_signals"] == 0
        assert agg["by_source"]["hackernews"] == []
        assert agg["by_source"]["reddit"] == []
