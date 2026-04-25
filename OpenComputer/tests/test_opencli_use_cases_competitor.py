"""Tests for extensions/opencli-scraper/use_cases/competitor_research.py."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_PLUGIN_DIR = Path(__file__).parent.parent / "extensions" / "opencli-scraper"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from use_cases.competitor_research import (  # noqa: E402
    compare_companies,
    scan_company_page,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

_SECTION_RESPONSE = {
    "title": "Stripe — Online payment processing",
    "body": "Stripe is a technology company that builds economic infrastructure for the internet.",
}


def _make_wrapper(response=None):
    wrapper = MagicMock()
    wrapper.run = AsyncMock(return_value={"data": response or _SECTION_RESPONSE})
    return wrapper


# ── Tests: scan_company_page ──────────────────────────────────────────────────


class TestScanCompanyPage:
    async def test_happy_path_returns_sections_dict(self):
        wrapper = _make_wrapper()
        result = await scan_company_page(wrapper, "stripe.com")

        assert "sections" in result
        assert isinstance(result["sections"], dict)

    async def test_default_sections_are_homepage_about_blog_pricing(self):
        wrapper = _make_wrapper()
        result = await scan_company_page(wrapper, "stripe.com")

        sections = result["sections"]
        for expected in ["homepage", "about", "blog", "pricing"]:
            assert expected in sections

    async def test_each_section_has_url_title_snippet(self):
        wrapper = _make_wrapper()
        result = await scan_company_page(wrapper, "stripe.com")

        for section_name, section_data in result["sections"].items():
            assert "url" in section_data, f"section {section_name} missing 'url'"
            assert "title" in section_data, f"section {section_name} missing 'title'"
            assert "snippet" in section_data, f"section {section_name} missing 'snippet'"

    async def test_sections_override_is_respected(self):
        wrapper = _make_wrapper()
        result = await scan_company_page(wrapper, "stripe.com", sections=["homepage", "team"])

        assert set(result["sections"].keys()) == {"homepage", "team"}

    async def test_wrapper_called_once_per_section(self):
        wrapper = _make_wrapper()
        sections = ["homepage", "about"]
        await scan_company_page(wrapper, "stripe.com", sections=sections)

        assert wrapper.run.call_count == len(sections)

    async def test_snippet_is_truncated_to_500_chars(self):
        long_body = "x" * 1000
        wrapper = _make_wrapper({"title": "Test", "body": long_body})
        result = await scan_company_page(wrapper, "example.com", sections=["homepage"])

        snippet = result["sections"]["homepage"]["snippet"]
        assert len(snippet) <= 500

    async def test_url_contains_domain(self):
        wrapper = _make_wrapper()
        result = await scan_company_page(wrapper, "stripe.com", sections=["about"])

        url = result["sections"]["about"]["url"]
        assert "stripe.com" in url


# ── Tests: PUBLIC-only enforcement ────────────────────────────────────────────


class TestPublicOnlyEnforcement:
    async def test_linkedin_domain_raises_value_error(self):
        wrapper = _make_wrapper()
        with pytest.raises(ValueError, match="COOKIE"):
            await scan_company_page(wrapper, "linkedin.com")

    async def test_www_linkedin_domain_raises_value_error(self):
        wrapper = _make_wrapper()
        with pytest.raises(ValueError, match="COOKIE"):
            await scan_company_page(wrapper, "www.linkedin.com")

    async def test_facebook_domain_raises_value_error(self):
        wrapper = _make_wrapper()
        with pytest.raises(ValueError, match="COOKIE"):
            await scan_company_page(wrapper, "facebook.com")

    async def test_public_domain_does_not_raise(self):
        wrapper = _make_wrapper()
        # Should not raise.
        result = await scan_company_page(wrapper, "github.com", sections=["homepage"])
        assert "sections" in result


# ── Tests: compare_companies ──────────────────────────────────────────────────


class TestCompareCompanies:
    async def test_aggregates_multiple_domains(self):
        wrapper = _make_wrapper()
        result = await compare_companies(wrapper, ["stripe.com", "github.com"])

        assert "stripe.com" in result
        assert "github.com" in result

    async def test_blocked_domain_gets_error_entry(self):
        wrapper = _make_wrapper()
        result = await compare_companies(wrapper, ["stripe.com", "linkedin.com"])

        assert "linkedin.com" in result
        assert "error" in result["linkedin.com"]
        assert "sections" in result["stripe.com"]

    async def test_empty_domains_returns_empty_dict(self):
        wrapper = _make_wrapper()
        result = await compare_companies(wrapper, [])
        assert result == {}
