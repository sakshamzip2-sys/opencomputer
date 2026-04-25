"""Tests for extensions/opencli-scraper/use_cases/context_enrichment.py."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_PLUGIN_DIR = Path(__file__).parent.parent / "extensions" / "opencli-scraper"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from use_cases.context_enrichment import (  # noqa: E402
    MENTION_PATTERN,
    enrich_mentions,
    extract_mentions,
    format_for_context,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_wrapper(return_data=None):
    wrapper = MagicMock()
    wrapper.run = AsyncMock(
        return_value={"data": return_data or {"login": "octocat", "name": "The Octocat"}}
    )
    return wrapper


# ── Tests: extract_mentions ───────────────────────────────────────────────────


class TestExtractMentions:
    def test_single_mention(self):
        mentions = extract_mentions("Tell me about @octocat on GitHub")
        assert len(mentions) == 1
        assert mentions[0] == ("octocat", "github")

    def test_multiple_mentions(self):
        text = "Compare @octocat on GitHub and @spez on Reddit"
        mentions = extract_mentions(text)
        assert len(mentions) == 2
        handles = [m[0] for m in mentions]
        platforms = [m[1] for m in mentions]
        assert "octocat" in handles
        assert "spez" in handles
        assert "github" in platforms
        assert "reddit" in platforms

    def test_case_insensitive_platform(self):
        mentions = extract_mentions("@alice on TWITTER")
        assert len(mentions) == 1
        assert mentions[0][1] == "twitter"  # normalised to lowercase

    def test_case_insensitive_handle(self):
        """Handle case is preserved (handles are case-sensitive on some platforms)."""
        mentions = extract_mentions("@Alice on GitHub")
        assert mentions[0][0] == "Alice"

    def test_no_match_returns_empty(self):
        mentions = extract_mentions("No mentions here")
        assert mentions == []

    def test_malformed_mention_not_matched(self):
        """'@octocat GitHub' without 'on' should not match."""
        mentions = extract_mentions("@octocat GitHub")
        assert len(mentions) == 0

    def test_pattern_is_compiled_regex(self):
        import re
        assert isinstance(MENTION_PATTERN, re.Pattern)


# ── Tests: enrich_mentions ────────────────────────────────────────────────────


class TestEnrichMentions:
    async def test_enrich_single_mention(self):
        wrapper = _make_wrapper({"login": "octocat", "name": "Octocat", "bio": "I'm GitHub's mascot"})
        result = await enrich_mentions(wrapper, "@octocat on GitHub")

        assert ("octocat", "github") in result
        profile = result[("octocat", "github")]
        assert "error" not in profile

    async def test_enrich_caps_at_max_fetches(self):
        """With max_fetches=1, only the first mention is fetched."""
        wrapper = _make_wrapper({"login": "octocat"})
        text = "@alice on GitHub @bob on Reddit @carol on Twitter"
        result = await enrich_mentions(wrapper, text, max_fetches=1)

        # Only 1 fetch should have been made.
        assert wrapper.run.call_count == 1
        # Only 1 key in result (the other mentions are skipped).
        fetched = [k for k, v in result.items() if "error" not in v]
        assert len(fetched) == 1

    async def test_unsupported_platform_gets_error_entry(self):
        wrapper = _make_wrapper()
        result = await enrich_mentions(wrapper, "@alice on myspace")

        key = ("alice", "myspace")
        assert key in result
        assert "error" in result[key]

    async def test_duplicate_mentions_deduplicated(self):
        """The same @handle on <platform> mentioned twice is fetched once."""
        wrapper = _make_wrapper({"login": "octocat"})
        result = await enrich_mentions(wrapper, "@octocat on GitHub and @octocat on GitHub")

        assert wrapper.run.call_count == 1

    async def test_enrich_returns_filtered_fields_only(self):
        """Only whitelisted fields should appear in the profile dict."""
        wrapper = _make_wrapper({
            "login": "octocat",
            "name": "Octocat",
            "public_repos": 10,
            "followers": 50,
            "html_url": "https://github.com/octocat",
            "evil_field": "should_be_stripped",
        })
        result = await enrich_mentions(wrapper, "@octocat on GitHub")
        profile = result.get(("octocat", "github"), {})
        assert "evil_field" not in profile


# ── Tests: format_for_context ─────────────────────────────────────────────────


class TestFormatForContext:
    def test_empty_enriched_returns_comment(self):
        output = format_for_context({})
        assert "No enriched context" in output

    def test_produces_markdown_heading(self):
        enriched = {
            ("octocat", "github"): {"login": "octocat", "name": "The Octocat"},
        }
        output = format_for_context(enriched)
        assert "## Enriched User Profiles" in output
        assert "@octocat" in output
        assert "github" in output.lower()

    def test_error_entry_renders_error_line(self):
        enriched = {
            ("alice", "myspace"): {"error": "Platform not supported"},
        }
        output = format_for_context(enriched)
        assert "Error" in output
        assert "Platform not supported" in output

    def test_fields_rendered_as_key_value(self):
        enriched = {
            ("octocat", "github"): {"login": "octocat", "followers": 42},
        }
        output = format_for_context(enriched)
        assert "login" in output
        assert "octocat" in output
        assert "42" in output
