"""Tests for extensions/opencli-scraper/field_whitelist.py."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).parent.parent / "extensions" / "opencli-scraper"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from field_whitelist import FIELD_WHITELISTS, filter_output  # noqa: E402


class TestFilterOutputDicts:
    def test_github_user_returns_only_whitelisted_fields(self):
        raw = {
            "login": "octocat",
            "name": "The Octocat",
            "email": "octocat@github.com",  # NOT whitelisted
            "bio": "I am octocat",
            "public_repos": 8,
            "followers": 9001,
            "html_url": "https://github.com/octocat",
            "private_repos": 5,  # NOT whitelisted
        }
        result = filter_output("github/user", raw)
        assert isinstance(result, dict)
        assert set(result.keys()) == FIELD_WHITELISTS["github/user"]
        assert "email" not in result
        assert "private_repos" not in result

    def test_reddit_user_filters_correctly(self):
        raw = {"name": "spez", "karma": 1000, "created_utc": 1234567890, "secret": "leak"}
        result = filter_output("reddit/user", raw)
        assert set(result.keys()) <= {"name", "karma", "created_utc"}
        assert "secret" not in result

    def test_twitter_profile_filters_correctly(self):
        raw = {
            "username": "jack",
            "name": "Jack",
            "bio": "founder",
            "followers_count": 5000000,
            "following_count": 100,
            "phone": "+1555555",  # NOT whitelisted
        }
        result = filter_output("twitter/profile", raw)
        assert "phone" not in result
        assert "username" in result

    def test_empty_dict_returns_empty(self):
        result = filter_output("github/user", {})
        assert result == {}

    def test_dict_with_no_whitelisted_fields_returns_empty(self):
        raw = {"completely_unknown_field": "value", "another": 123}
        result = filter_output("github/user", raw)
        assert result == {}


class TestFilterOutputLists:
    def test_list_of_dicts_filtered(self):
        raw = [
            {"id": "abc", "title": "Paper", "authors": ["Alice"], "email": "a@b.com"},
            {"id": "xyz", "title": "More", "summary": "cool", "phone": "123"},
        ]
        result = filter_output("arxiv/search", raw)
        assert isinstance(result, list)
        assert len(result) == 2
        assert "email" not in result[0]
        assert "phone" not in result[1]
        assert "id" in result[0]

    def test_empty_list_returns_empty_list(self):
        result = filter_output("reddit/posts", [])
        assert result == []

    def test_list_with_non_dict_items_handled_gracefully(self):
        raw = [{"id": "a", "title": "T"}, "not_a_dict"]
        # Should not raise; bad items return {}.
        result = filter_output("reddit/posts", raw)
        assert isinstance(result, list)
        assert result[0]["id"] == "a"
        assert result[1] == {}


class TestUnknownAdapter:
    def test_unknown_adapter_dict_returns_empty_dict(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = filter_output("unknown/adapter", {"key": "value"})
        assert result == {}
        assert "unknown adapter" in caplog.text.lower() or "unknown" in caplog.text

    def test_unknown_adapter_list_returns_empty_list(self):
        result = filter_output("not/registered", [{"a": 1}])
        assert result == []

    def test_unknown_adapter_non_dict_non_list_raises(self):
        with pytest.raises(TypeError):
            filter_output("unknown/adapter", "a string")  # type: ignore[arg-type]


class TestAllAdapters:
    def test_all_15_adapters_present(self):
        expected_adapters = {
            "github/user",
            "reddit/user",
            "reddit/posts",
            "reddit/comments",
            "linkedin/timeline",
            "twitter/profile",
            "twitter/tweets",
            "hackernews/user",
            "stackoverflow/user",
            "youtube/user",
            "medium/user",
            "bluesky/profile",
            "arxiv/search",
            "wikipedia/user-contributions",
            "producthunt/user",
        }
        assert expected_adapters == set(FIELD_WHITELISTS.keys())

    def test_known_adapter_non_dict_non_list_raises(self):
        with pytest.raises(TypeError):
            filter_output("github/user", 42)  # type: ignore[arg-type]
