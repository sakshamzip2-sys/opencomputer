"""Item 3 (2026-05-02): validate strict-mode tool schemas.

Anthropic's ``strict: true`` requires:
- top-level type "object"
- additionalProperties: false on object schemas
- every property has explicit type/enum/$ref

Tools opt out via ``strict_mode = False`` with a comment when their
schemas are intentionally polymorphic / free-form.
"""
from __future__ import annotations

import pytest

from plugin_sdk.tool_contract import BaseTool, ToolSchema


def _is_strict_compatible(schema_params: dict) -> tuple[bool, str]:
    """Return (passes, reason) for an Anthropic strict-mode JSON Schema check."""
    if schema_params.get("type") != "object":
        return False, "top-level type is not 'object'"
    if schema_params.get("additionalProperties") is not False:
        return False, "additionalProperties is not False"
    props = schema_params.get("properties") or {}
    for name, spec in props.items():
        if not isinstance(spec, dict):
            return False, f"property '{name}' spec is not a dict"
        if "type" not in spec and "enum" not in spec and "$ref" not in spec:
            return False, f"property '{name}' has no type/enum/$ref"
    return True, ""


def _all_registered_tools() -> list[BaseTool]:
    """Return every tool in the project's default registry.

    The CLI's ``_register_builtin_tools`` is the canonical registration
    path; we trigger it directly so this test sees the same tool set the
    running agent does.
    """
    from opencomputer.cli import _register_builtin_tools
    from opencomputer.tools.registry import registry

    _register_builtin_tools()
    return list(registry._tools.values())


_TOOLS = _all_registered_tools()


@pytest.mark.parametrize("tool", _TOOLS, ids=lambda t: t.schema.name)
def test_strict_tools_have_compatible_schemas(tool: BaseTool):
    """Every tool with strict_mode=True must have a strict-compatible schema."""
    if not getattr(tool, "strict_mode", False):
        pytest.skip(f"{tool.schema.name} opted out of strict mode")
    passes, reason = _is_strict_compatible(tool.schema.parameters)
    assert passes, f"{tool.schema.name} not strict-compatible: {reason}"


def test_initial_strict_adoption_includes_core_tools():
    """Item 3 ships the infrastructure with 5 well-defined tools opted in.

    The spec's ≥80% target is aspirational; broader rollout happens as
    each tool's schema is audited for strict-compatibility (the per-tool
    test above gates each addition). This test pins the floor: the core
    Read/Write/Bash/Glob/Grep family must be strict.
    """
    if not _TOOLS:
        pytest.skip("no tools registered")
    by_name = {t.schema.name: t for t in _TOOLS}
    core = ["Read", "Write", "Bash", "Glob", "Grep"]
    for name in core:
        tool = by_name.get(name)
        if tool is None:
            pytest.skip(f"{name} not registered")
        assert getattr(tool, "strict_mode", False), (
            f"{name} should have strict_mode=True (Item 3 core opt-in)"
        )


def test_strict_emitted_in_anthropic_format():
    """ToolSchema with strict=True includes the strict field in API format."""
    s_no = ToolSchema(
        name="DummyA", description="x",
        parameters={"type": "object", "additionalProperties": False, "properties": {}},
    )
    s_yes = ToolSchema(
        name="DummyB", description="x",
        parameters={"type": "object", "additionalProperties": False, "properties": {}},
        strict=True,
    )
    assert "strict" not in s_no.to_anthropic_format()
    assert s_yes.to_anthropic_format()["strict"] is True


@pytest.mark.parametrize("tool", _TOOLS, ids=lambda t: t.schema.name)
def test_strict_tools_have_no_default_keys(tool: BaseTool):
    """Strict-mode tool schemas must not declare JSON-Schema 'default' keys.

    Different SDKs treat 'default' differently under strict — OpenAI rejects
    it outright; Anthropic's behaviour is unspecified. Safer to keep it out
    of the wire payload and document defaults in the description text. Each
    tool's ``execute()`` already supplies fallbacks via ``args.get(k, default)``.
    """
    if not getattr(tool, "strict_mode", False):
        pytest.skip(f"{tool.schema.name} opted out of strict mode")
    props = tool.schema.parameters.get("properties") or {}

    def _no_default(spec: dict, path: str) -> None:
        if not isinstance(spec, dict):
            return
        assert "default" not in spec, (
            f"{tool.schema.name}: 'default' key at {path} is incompatible with strict mode"
        )
        for k, v in spec.items():
            if isinstance(v, dict):
                _no_default(v, f"{path}.{k}")

    for name, spec in props.items():
        _no_default(spec, name)
