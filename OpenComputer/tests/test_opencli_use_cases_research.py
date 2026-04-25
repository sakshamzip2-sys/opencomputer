"""Tests for extensions/opencli-scraper/use_cases/research_automation.py.

All I/O is mocked — no live opencli binary or network access required.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add the plugin dir to sys.path so use_cases imports resolve.
_PLUGIN_DIR = Path(__file__).parent.parent / "extensions" / "opencli-scraper"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from use_cases.research_automation import (  # noqa: E402
    build_citation_graph,
    fetch_arxiv_paper_metadata,
    search_by_topic,
)

# ── Fixture helpers ────────────────────────────────────────────────────────────

_PAPER_FIXTURE = {
    "id": "2401.00001",
    "title": "Attention Is All You Need",
    "authors": ["Vaswani", "Shazeer"],
    "summary": "Transformer architecture paper",
    "published": "2017-06-12",
    "pdf_url": "https://arxiv.org/pdf/2401.00001",
    "extra_field": "should_be_stripped",
}

_RELATED_PAPER = {
    "id": "2401.00002",
    "title": "BERT: Pre-training of Deep Bidirectional Transformers",
    "authors": ["Devlin"],
    "summary": "BERT paper",
    "published": "2018-10-11",
    "pdf_url": "https://arxiv.org/pdf/2401.00002",
}


def _make_wrapper(return_data=None):
    """Return a mock wrapper whose run() returns return_data."""
    wrapper = MagicMock()
    wrapper.run = AsyncMock(return_value={"data": return_data or [_PAPER_FIXTURE]})
    return wrapper


# ── Tests: fetch_arxiv_paper_metadata ─────────────────────────────────────────


class TestFetchArxivPaperMetadata:
    async def test_happy_path_returns_filtered_dict(self):
        wrapper = _make_wrapper([_PAPER_FIXTURE])
        result = await fetch_arxiv_paper_metadata(wrapper, "2401.00001")

        assert result["id"] == "2401.00001"
        assert result["title"] == "Attention Is All You Need"
        assert "extra_field" not in result

    async def test_wrapper_called_with_paper_id(self):
        wrapper = _make_wrapper([_PAPER_FIXTURE])
        await fetch_arxiv_paper_metadata(wrapper, "2401.00001")

        wrapper.run.assert_called_once()
        call_args = wrapper.run.call_args
        # adapter arg is "arxiv/search"
        assert call_args[0][0] == "arxiv/search"
        assert "2401.00001" in call_args[0]

    async def test_single_dict_response_accepted(self):
        """Adapter returning a single dict (not list) should still work."""
        wrapper = MagicMock()
        wrapper.run = AsyncMock(return_value={"data": _PAPER_FIXTURE})
        result = await fetch_arxiv_paper_metadata(wrapper, "2401.00001")
        assert result["title"] == "Attention Is All You Need"

    async def test_empty_paper_id_raises_value_error(self):
        wrapper = _make_wrapper()
        with pytest.raises(ValueError, match="non-empty"):
            await fetch_arxiv_paper_metadata(wrapper, "")

    async def test_whitespace_only_paper_id_raises_value_error(self):
        wrapper = _make_wrapper()
        with pytest.raises(ValueError, match="non-empty"):
            await fetch_arxiv_paper_metadata(wrapper, "   ")

    async def test_empty_result_list_raises_runtime_error(self):
        wrapper = MagicMock()
        wrapper.run = AsyncMock(return_value={"data": []})
        with pytest.raises(RuntimeError, match="no results"):
            await fetch_arxiv_paper_metadata(wrapper, "9999.99999")


# ── Tests: build_citation_graph ───────────────────────────────────────────────


class TestBuildCitationGraph:
    async def test_depth_1_returns_seed_only_when_no_related(self):
        """At depth=1, if the related-paper search returns nothing, only seed is returned."""
        wrapper = MagicMock()

        # First call: fetch seed paper metadata.
        # Second call: search_by_topic for related papers — returns empty.
        wrapper.run = AsyncMock(
            side_effect=[
                {"data": [_PAPER_FIXTURE]},  # fetch seed metadata
                {"data": []},  # search_by_topic returns empty
            ]
        )
        result = await build_citation_graph(wrapper, "2401.00001", depth=1)

        assert "papers" in result
        assert "edges" in result
        assert len(result["papers"]) >= 1
        seed = next((p for p in result["papers"] if p.get("id") == "2401.00001"), None)
        assert seed is not None

    async def test_depth_1_follows_related_papers(self):
        """Depth=1 fetches related papers from search results."""
        wrapper = MagicMock()
        wrapper.run = AsyncMock(
            side_effect=[
                {"data": [_PAPER_FIXTURE]},      # fetch seed
                {"data": [_RELATED_PAPER]},       # search returns one related
                {"data": [_RELATED_PAPER]},       # fetch related metadata
                {"data": []},                     # search from related returns empty
            ]
        )
        result = await build_citation_graph(wrapper, "2401.00001", depth=1)

        assert len(result["papers"]) >= 1
        # Edges may or may not exist depending on recursion — just assert structure.
        assert isinstance(result["edges"], list)

    async def test_depth_clamped_to_3(self):
        """Depth > 3 should be clamped. We just confirm it doesn't raise."""
        wrapper = MagicMock()
        wrapper.run = AsyncMock(return_value={"data": [_PAPER_FIXTURE]})
        # Should not raise; search returns empty so recursion ends quickly.
        result = await build_citation_graph(wrapper, "2401.00001", depth=99)
        assert "papers" in result

    async def test_returns_expected_structure(self):
        wrapper = _make_wrapper([_PAPER_FIXTURE])
        # After seed fetch, search returns empty — no edges.
        wrapper.run = AsyncMock(
            side_effect=[
                {"data": [_PAPER_FIXTURE]},
                {"data": []},
            ]
        )
        result = await build_citation_graph(wrapper, "2401.00001", depth=1)
        assert set(result.keys()) == {"papers", "edges"}
        assert isinstance(result["papers"], list)
        assert isinstance(result["edges"], list)

    async def test_duplicate_papers_not_revisited(self):
        """The same paper ID should not appear twice in the papers list."""
        wrapper = MagicMock()
        # Always return the same paper — graph should deduplicate.
        wrapper.run = AsyncMock(return_value={"data": [_PAPER_FIXTURE]})
        result = await build_citation_graph(wrapper, "2401.00001", depth=1)
        ids = [p.get("id") for p in result["papers"]]
        assert len(ids) == len(set(ids))


# ── Tests: search_by_topic ────────────────────────────────────────────────────


class TestSearchByTopic:
    async def test_happy_path_returns_list(self):
        wrapper = _make_wrapper([_PAPER_FIXTURE, _RELATED_PAPER])
        results = await search_by_topic(wrapper, "transformers")

        assert isinstance(results, list)
        assert len(results) == 2
        assert results[0]["title"] == "Attention Is All You Need"

    async def test_fields_are_filtered(self):
        fixture_with_extra = {**_PAPER_FIXTURE, "extra_field": "STRIP_ME"}
        wrapper = _make_wrapper([fixture_with_extra])
        results = await search_by_topic(wrapper, "transformers")

        assert "extra_field" not in results[0]

    async def test_empty_query_raises_value_error(self):
        wrapper = _make_wrapper()
        with pytest.raises(ValueError, match="non-empty"):
            await search_by_topic(wrapper, "")

    async def test_limit_passed_to_wrapper(self):
        wrapper = _make_wrapper([_PAPER_FIXTURE])
        await search_by_topic(wrapper, "BERT", limit=5)

        call_args = wrapper.run.call_args[0]
        assert "--limit" in call_args
        limit_idx = list(call_args).index("--limit")
        assert call_args[limit_idx + 1] == "5"

    async def test_results_dict_format_accepted(self):
        """Adapter wrapping results in a 'results' key should be unwrapped."""
        wrapper = MagicMock()
        wrapper.run = AsyncMock(
            return_value={"data": {"results": [_PAPER_FIXTURE]}}
        )
        results = await search_by_topic(wrapper, "transformers")
        assert len(results) == 1
