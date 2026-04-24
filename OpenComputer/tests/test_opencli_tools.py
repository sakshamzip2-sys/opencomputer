"""Tests for extensions/opencli-scraper/tools.py.

All I/O (rate limiter, robots cache, wrapper subprocess) is injected as mocks.
Tests verify: schema validity, correct call order, happy-path results,
platform mapping, error paths.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: F401

import pytest

_PLUGIN_DIR = Path(__file__).parent.parent / "extensions" / "opencli-scraper"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from tools import FetchProfileTool, MonitorPageTool, ScrapeRawTool  # noqa: E402

from plugin_sdk.core import ToolCall, ToolResult  # noqa: E402
from plugin_sdk.tool_contract import BaseTool, ToolSchema  # noqa: E402

# ── Fixtures ───────────────────────────────────────────────────────────────────


def _make_call(name: str, **kwargs) -> ToolCall:
    return ToolCall(id="test-id", name=name, arguments=kwargs)


def _make_mocks(
    wrapper_data: dict | None = None,
    robots_allowed: bool = True,
):
    """Return (wrapper, rate_limiter, robots_cache) mocks."""
    wrapper = MagicMock()
    wrapper.run = AsyncMock(return_value=wrapper_data or {"data": {"login": "octocat"}})

    rate_limiter = MagicMock()
    rate_limiter.acquire = AsyncMock(return_value=None)

    robots_cache = MagicMock()
    robots_cache.allowed = AsyncMock(return_value=robots_allowed)

    return wrapper, rate_limiter, robots_cache


# ── Schema tests ───────────────────────────────────────────────────────────────


class TestSchemas:
    def test_scrape_raw_schema_is_tool_schema(self):
        w, r, rc = _make_mocks()
        tool = ScrapeRawTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        assert isinstance(tool.schema, ToolSchema)
        assert tool.schema.name == "ScrapeRaw"

    def test_fetch_profile_schema_is_tool_schema(self):
        w, r, rc = _make_mocks()
        tool = FetchProfileTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        assert isinstance(tool.schema, ToolSchema)
        assert tool.schema.name == "FetchProfile"

    def test_monitor_page_schema_is_tool_schema(self):
        w, r, rc = _make_mocks()
        tool = MonitorPageTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        assert isinstance(tool.schema, ToolSchema)
        assert tool.schema.name == "MonitorPage"

    def test_all_schemas_have_required_fields(self):
        w, r, rc = _make_mocks()
        for tool in [
            ScrapeRawTool(wrapper=w, rate_limiter=r, robots_cache=rc),
            FetchProfileTool(wrapper=w, rate_limiter=r, robots_cache=rc),
            MonitorPageTool(wrapper=w, rate_limiter=r, robots_cache=rc),
        ]:
            schema = tool.schema
            assert schema.name
            assert schema.description
            assert "properties" in schema.parameters

    def test_all_tools_are_base_tool(self):
        w, r, rc = _make_mocks()
        for cls in [ScrapeRawTool, FetchProfileTool, MonitorPageTool]:
            assert issubclass(cls, BaseTool)


# ── ScrapeRawTool tests ────────────────────────────────────────────────────────


class TestScrapeRawTool:
    async def test_happy_path_returns_filtered_json(self):
        raw = {"data": {"login": "octocat", "name": "Octocat", "email": "secret@private.com"}}
        w, r, rc = _make_mocks(wrapper_data=raw)
        tool = ScrapeRawTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(_make_call("ScrapeRaw", adapter="github/user", args=["octocat"]))

        assert isinstance(result, ToolResult)
        assert not result.is_error
        data = json.loads(result.content)
        # email is not in the whitelist → filtered out
        assert "email" not in data
        assert "login" in data

    async def test_execute_calls_rate_limiter_first(self):
        """rate_limiter.acquire must be called before wrapper.run."""
        call_order: list[str] = []

        w, r, rc = _make_mocks()

        async def track_acquire(domain):
            call_order.append("acquire")

        async def track_run(adapter, *args, **kwargs):
            call_order.append("run")
            return {"data": {"login": "octocat"}}

        r.acquire = AsyncMock(side_effect=track_acquire)
        w.run = AsyncMock(side_effect=track_run)

        tool = ScrapeRawTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        await tool.execute(_make_call("ScrapeRaw", adapter="github/user", args=["octocat"]))

        assert call_order[0] == "acquire"
        assert "run" in call_order

    async def test_execute_checks_robots_before_wrapper(self):
        """robots_cache.allowed must be called before wrapper.run."""
        call_order: list[str] = []

        w, r, rc = _make_mocks()

        async def track_robots(url):
            call_order.append("robots")
            return True

        async def track_run(adapter, *args, **kwargs):
            call_order.append("run")
            return {}

        rc.allowed = AsyncMock(side_effect=track_robots)
        w.run = AsyncMock(side_effect=track_run)

        tool = ScrapeRawTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        await tool.execute(_make_call("ScrapeRaw", adapter="github/user", args=["octocat"]))

        robots_idx = call_order.index("robots")
        run_idx = call_order.index("run")
        assert robots_idx < run_idx

    async def test_robots_denied_returns_error(self):
        w, r, rc = _make_mocks(robots_allowed=False)
        tool = ScrapeRawTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(_make_call("ScrapeRaw", adapter="github/user", args=["x"]))
        assert result.is_error
        assert "robots" in result.content.lower()

    async def test_missing_adapter_returns_error(self):
        w, r, rc = _make_mocks()
        tool = ScrapeRawTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(_make_call("ScrapeRaw"))  # no adapter arg
        assert result.is_error
        assert "adapter" in result.content.lower()

    async def test_wrapper_exception_returns_error_result(self):
        w, r, rc = _make_mocks()
        w.run = AsyncMock(side_effect=RuntimeError("subprocess crash"))
        tool = ScrapeRawTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(
            _make_call("ScrapeRaw", adapter="github/user", args=["octocat"])
        )
        assert result.is_error
        assert "subprocess crash" in result.content


# ── FetchProfileTool tests ─────────────────────────────────────────────────────


class TestFetchProfileTool:
    async def test_github_platform_maps_to_correct_adapter(self):
        w, r, rc = _make_mocks(wrapper_data={"data": {"login": "gh-user"}})
        tool = FetchProfileTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(_make_call("FetchProfile", platform="github", user="gh-user"))

        assert not result.is_error
        # wrapper.run should have been called with github/user adapter.
        first_call_args = w.run.call_args[0]
        assert first_call_args[0] == "github/user"

    async def test_twitter_platform_maps_to_correct_adapter(self):
        w, r, rc = _make_mocks(
            wrapper_data={"data": {"username": "jack", "name": "Jack", "bio": "founder"}}
        )
        tool = FetchProfileTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(_make_call("FetchProfile", platform="twitter", user="jack"))

        assert not result.is_error
        first_call_args = w.run.call_args[0]
        assert first_call_args[0] == "twitter/profile"

    async def test_unknown_platform_returns_error(self):
        w, r, rc = _make_mocks()
        tool = FetchProfileTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(
            _make_call("FetchProfile", platform="nonexistent_platform", user="someone")
        )
        assert result.is_error
        assert "unknown platform" in result.content.lower()
        assert "nonexistent_platform" in result.content

    async def test_missing_platform_returns_error(self):
        w, r, rc = _make_mocks()
        tool = FetchProfileTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(_make_call("FetchProfile", user="someone"))
        assert result.is_error

    async def test_missing_user_returns_error(self):
        w, r, rc = _make_mocks()
        tool = FetchProfileTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(_make_call("FetchProfile", platform="github"))
        assert result.is_error

    async def test_hn_alias_works(self):
        """'hn' should be accepted as an alias for hackernews."""
        w, r, rc = _make_mocks(wrapper_data={"data": {"id": "pg", "karma": 10000}})
        tool = FetchProfileTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(_make_call("FetchProfile", platform="hn", user="pg"))
        assert not result.is_error
        first_call_args = w.run.call_args[0]
        assert first_call_args[0] == "hackernews/user"


# ── MonitorPageTool tests ──────────────────────────────────────────────────────


class TestMonitorPageTool:
    async def test_returns_content_hash_and_timestamp(self):
        w, r, rc = _make_mocks(wrapper_data={"html": "<p>hello</p>"})
        # MonitorPageTool uses _ADAPTER_DOMAINS_REVERSE for known domains;
        # use a known domain so the adapter lookup succeeds.
        w.run = AsyncMock(return_value={"html": "<p>hello</p>"})
        tool = MonitorPageTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(
            _make_call("MonitorPage", url="https://github.com/octocat", interval_s=300)
        )

        assert not result.is_error
        data = json.loads(result.content)
        assert "content_hash" in data
        assert "fetched_at" in data
        assert len(data["content_hash"]) == 64  # SHA-256 hex digest

    async def test_missing_url_returns_error(self):
        w, r, rc = _make_mocks()
        tool = MonitorPageTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(_make_call("MonitorPage", interval_s=300))
        assert result.is_error
        assert "url" in result.content.lower()

    async def test_robots_denied_returns_error(self):
        w, r, rc = _make_mocks(robots_allowed=False)
        tool = MonitorPageTool(wrapper=w, rate_limiter=r, robots_cache=rc)
        result = await tool.execute(
            _make_call("MonitorPage", url="https://denied.com/page", interval_s=60)
        )
        assert result.is_error
        assert "robots" in result.content.lower()

    async def test_different_content_produces_different_hash(self):
        """Two invocations with different content must differ in content_hash."""
        tool_call = _make_call("MonitorPage", url="https://reddit.com/", interval_s=60)

        w1, r1, rc1 = _make_mocks(wrapper_data={"html": "version A"})
        tool1 = MonitorPageTool(wrapper=w1, rate_limiter=r1, robots_cache=rc1)
        result1 = await tool1.execute(tool_call)

        w2, r2, rc2 = _make_mocks(wrapper_data={"html": "version B"})
        tool2 = MonitorPageTool(wrapper=w2, rate_limiter=r2, robots_cache=rc2)
        result2 = await tool2.execute(tool_call)

        data1 = json.loads(result1.content)
        data2 = json.loads(result2.content)
        assert data1["content_hash"] != data2["content_hash"]
