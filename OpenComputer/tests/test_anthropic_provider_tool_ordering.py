"""V2 — tool ordering must be deterministic across calls + sorted by name."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from plugin_sdk.tool_contract import ToolSchema


def _load_provider_module():
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    name = "_anth_provider_tool_order_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, plugin_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fmt():
    return _load_provider_module()._format_tools_for_anthropic


def _ts(name: str, desc: str = "") -> ToolSchema:
    return ToolSchema(
        name=name,
        description=desc or name,
        parameters={"type": "object", "properties": {}},
    )


def test_format_tools_sorted_alphabetical(fmt):
    tools = [_ts("zoom"), _ts("apple"), _ts("mango")]
    out = fmt(tools)
    assert [t["name"] for t in out] == ["apple", "mango", "zoom"]


def test_format_tools_byte_stable_across_calls(fmt):
    tools = [_ts(f"tool_{i:02d}") for i in range(20)]
    out1 = fmt(list(tools))
    out2 = fmt(list(reversed(tools)))
    assert json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True)


def test_format_tools_empty_passthrough(fmt):
    assert fmt(None) == []
    assert fmt([]) == []


def test_format_tools_preserves_input_schema(fmt):
    """Sort doesn't mangle individual tool dicts."""
    t = ToolSchema(
        name="foo",
        description="d",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        },
    )
    out = fmt([t])
    assert out[0]["name"] == "foo"
    assert out[0]["input_schema"]["properties"]["x"]["type"] == "string"
    assert out[0]["input_schema"]["required"] == ["x"]
