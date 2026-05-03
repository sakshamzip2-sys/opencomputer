"""Tests for the adapter runner — coerce_args + run_adapter + ToolResult mapping."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_registry():
    from extensions.adapter_runner import clear_registry_for_tests

    clear_registry_for_tests()
    yield
    clear_registry_for_tests()


def test_coerce_args_fills_defaults():
    from extensions.adapter_runner import Strategy, adapter
    from extensions.adapter_runner._runner import coerce_args

    @adapter(
        site="s",
        name="n",
        description="d",
        domain="e.com",
        strategy=Strategy.PUBLIC,
        args=[
            {"name": "limit", "type": "int", "default": 10},
            {"name": "verbose", "type": "bool", "default": False},
        ],
    )
    async def run(args, ctx):
        return []

    spec = run._adapter_spec
    out = coerce_args(spec, {"limit": "25", "verbose": "true"})
    assert out["limit"] == 25
    assert out["verbose"] is True


def test_coerce_args_required_missing_raises():
    from extensions.adapter_runner import Strategy, adapter
    from extensions.adapter_runner._runner import coerce_args

    @adapter(
        site="s",
        name="n",
        description="d",
        domain="e.com",
        strategy=Strategy.PUBLIC,
        args=[{"name": "q", "type": "string", "required": True}],
    )
    async def run(args, ctx):
        return []

    spec = run._adapter_spec
    with pytest.raises(ValueError, match="missing required"):
        coerce_args(spec, {})


def test_run_adapter_returns_formatted_rows(tmp_path: Path):
    from extensions.adapter_runner import Strategy, adapter
    from extensions.adapter_runner._runner import run_adapter

    @adapter(
        site="s",
        name="n",
        description="d",
        domain="e.com",
        strategy=Strategy.PUBLIC,
        columns=["a", "b"],
    )
    async def run(args, ctx):
        return [{"a": 1, "b": 2}, {"a": 3, "b": 4}]

    spec = run._adapter_spec
    result = asyncio.run(run_adapter(spec, arguments={}, profile_home=tmp_path))
    assert not result.is_error
    # JSON formatted
    assert '"a"' in result.content and '"b"' in result.content


def test_run_adapter_maps_auth_required_error(tmp_path: Path):
    from extensions.adapter_runner import Strategy, adapter
    from extensions.adapter_runner._runner import run_adapter
    from extensions.browser_control._utils.errors import AuthRequiredError

    @adapter(
        site="s",
        name="n2",
        description="d",
        domain="e.com",
        strategy=Strategy.PUBLIC,
    )
    async def run(args, ctx):
        raise AuthRequiredError("not logged in")

    spec = run._adapter_spec
    result = asyncio.run(run_adapter(spec, arguments={}, profile_home=tmp_path))
    assert result.is_error
    assert "auth required" in result.content.lower()


def test_run_adapter_timeout(tmp_path: Path):
    from extensions.adapter_runner import Strategy, adapter
    from extensions.adapter_runner._runner import run_adapter

    @adapter(
        site="s",
        name="slow",
        description="d",
        domain="e.com",
        strategy=Strategy.PUBLIC,
    )
    async def run(args, ctx):
        await asyncio.sleep(5)
        return []

    spec = run._adapter_spec
    result = asyncio.run(
        run_adapter(spec, arguments={}, profile_home=tmp_path, timeout_override=0.1)
    )
    assert result.is_error
    assert "exceeded" in result.content


def test_run_adapter_generic_exception_is_caught(tmp_path: Path):
    from extensions.adapter_runner import Strategy, adapter
    from extensions.adapter_runner._runner import run_adapter

    @adapter(
        site="s",
        name="boom",
        description="d",
        domain="e.com",
        strategy=Strategy.PUBLIC,
    )
    async def run(args, ctx):
        raise ValueError("kaboom")

    spec = run._adapter_spec
    result = asyncio.run(run_adapter(spec, arguments={}, profile_home=tmp_path))
    assert result.is_error
    assert "kaboom" in result.content
