"""Tests for ``@adapter`` decorator + AdapterSpec + module-level registry."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Empty the global registry before + after each test."""
    from extensions.adapter_runner import clear_registry_for_tests

    clear_registry_for_tests()
    yield
    clear_registry_for_tests()


def test_adapter_decorator_returns_underlying_callable():
    from extensions.adapter_runner import Strategy, adapter

    @adapter(
        site="hackernews",
        name="top",
        description="HN top stories",
        domain="news.ycombinator.com",
        strategy=Strategy.PUBLIC,
        args=[{"name": "limit", "type": "int", "default": 20}],
        columns=["rank", "title"],
    )
    async def run(args, ctx):
        return [{"rank": 1, "title": "hello"}]

    # Decorator returns the underlying callable so it's still directly invokable.
    assert asyncio.iscoroutinefunction(run)
    spec = getattr(run, "_adapter_spec", None)
    assert spec is not None
    assert spec.tool_name == "HackernewsTop"
    assert spec.strategy is Strategy.PUBLIC
    assert spec.columns == ("rank", "title")


def test_adapter_registers_in_module_registry():
    from extensions.adapter_runner import (
        Strategy,
        adapter,
        get_registered_adapters,
    )

    @adapter(
        site="arxiv",
        name="search",
        description="arXiv search",
        domain="arxiv.org",
        strategy=Strategy.PUBLIC,
    )
    async def run(args, ctx):
        return []

    specs = get_registered_adapters()
    assert len(specs) == 1
    assert specs[0].site == "arxiv"
    assert specs[0].name == "search"


def test_duplicate_site_name_raises():
    from extensions.adapter_runner import Strategy, adapter

    @adapter(
        site="dup",
        name="x",
        description="d",
        domain="example.com",
        strategy=Strategy.PUBLIC,
    )
    async def run1(args, ctx):
        return []

    with pytest.raises(ValueError, match="duplicate"):

        @adapter(
            site="dup",
            name="x",
            description="d2",
            domain="example.com",
            strategy=Strategy.PUBLIC,
        )
        async def run2(args, ctx):
            return []


def test_invalid_site_name_pattern_raises():
    from extensions.adapter_runner import Strategy, adapter

    with pytest.raises(ValueError, match="lowercase"):

        @adapter(
            site="Has-Caps",
            name="ok",
            description="d",
            domain="example.com",
            strategy=Strategy.PUBLIC,
        )
        async def run(args, ctx):
            return []


def test_strategy_string_accepted():
    from extensions.adapter_runner import Strategy, adapter

    @adapter(
        site="s",
        name="n",
        description="d",
        domain="e.com",
        strategy="cookie",
    )
    async def run(args, ctx):
        return []

    spec = run._adapter_spec
    assert spec.strategy is Strategy.COOKIE


def test_invalid_strategy_raises():
    from extensions.adapter_runner import adapter

    with pytest.raises(ValueError, match="strategy"):

        @adapter(
            site="s",
            name="n",
            description="d",
            domain="e.com",
            strategy="HEADER",  # not a real strategy
        )
        async def run(args, ctx):
            return []


def test_sync_func_rejected():
    from extensions.adapter_runner import Strategy, adapter

    with pytest.raises(ValueError, match="async def"):

        @adapter(
            site="sy",
            name="nc",
            description="d",
            domain="e.com",
            strategy=Strategy.PUBLIC,
        )
        def run(args, ctx):  # NOT async
            return []


def test_pascal_tool_name_for_complex_site():
    from extensions.adapter_runner import Strategy, adapter

    @adapter(
        site="apple_podcasts",
        name="search_charts",
        description="d",
        domain="itunes.apple.com",
        strategy=Strategy.PUBLIC,
    )
    async def run(args, ctx):
        return []

    spec = run._adapter_spec
    assert spec.tool_name == "ApplePodcastsSearchCharts"


def test_to_json_schema_includes_required_and_defaults():
    from extensions.adapter_runner import Strategy, adapter

    @adapter(
        site="s",
        name="n",
        description="d",
        domain="e.com",
        strategy=Strategy.PUBLIC,
        args=[
            {"name": "q", "type": "string", "required": True, "help": "query"},
            {"name": "limit", "type": "int", "default": 10},
        ],
    )
    async def run(args, ctx):
        return []

    spec = run._adapter_spec
    schema = spec.to_json_schema()
    assert schema["properties"]["q"]["type"] == "string"
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema["properties"]["limit"]["default"] == 10
    assert schema["required"] == ["q"]
