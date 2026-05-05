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


def test_skills_augmentation_preserves_user_tool_marker_position():
    """Audit Finding 3 (2026-05-05): when skills are enabled,
    ``_augment_kwargs_for_skills`` appends ``code_execution_20250825``
    AFTER ``_apply_cache_control`` has already placed the marker on the
    alphabetically-last user tool. The cache_control then lives on
    ``tools[-2]`` in the wire payload, not ``tools[-1]`` — but the
    cached prefix region (the user tools) still matches byte-for-byte
    across turns, which is what the cache actually requires.
    """
    mod = _load_provider_module()
    augment = mod._augment_kwargs_for_skills

    # Simulate the real flow: _apply_cache_control marked the last user
    # tool's last block; then skills augmentation appends code_execution
    # without a marker.
    tools_with_marker = [
        {"name": "apple", "description": "a", "input_schema": {"type": "object", "properties": {}}},
        {
            "name": "zebra",
            "description": "z",
            "input_schema": {"type": "object", "properties": {}},
            "cache_control": {"type": "ephemeral"},
        },
    ]
    kwargs = {"tools": tools_with_marker}
    out = augment(kwargs=kwargs, skill_ids=["my-skill"])
    out_tools = out["tools"]

    # 3 tools total: 2 user + 1 code_execution
    assert len(out_tools) == 3
    # cache_control STILL on the user-tool (now tools[-2])
    assert out_tools[-2]["name"] == "zebra"
    assert "cache_control" in out_tools[-2]
    # code_execution is appended at the end with NO marker
    assert out_tools[-1]["type"] == "code_execution_20250825"
    assert "cache_control" not in out_tools[-1]
    # Earlier user tool is unmarked
    assert out_tools[0]["name"] == "apple"
    assert "cache_control" not in out_tools[0]


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
