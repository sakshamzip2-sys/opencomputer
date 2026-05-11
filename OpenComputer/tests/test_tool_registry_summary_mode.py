"""CC §4 — deferred-injection helpers on ToolRegistry.

Spec: docs/OC-FROM-CLAUDE-CODE.md §4.

Claude Code injects only tool NAMES + short descriptions at startup
and forces the agent to call ``ToolSearch`` for the full schema. This
saves significant tokens (~10x for 50+ tools). The ``ToolSearch``
primitive already exists (test_tool_search.py). This commit adds the
registry-side helpers providers consume when lazy mode is on:

  - ``tool_summaries(max_description_len)`` — returns ``list[dict]`` of
    ``{"name": str, "description": str}`` with description truncated.
    No parameters block at all.
  - ``summary_schemas(max_description_len)`` — returns ``list[ToolSchema]``
    with the same trimmed-description shape but ``parameters={}``
    (still a valid ToolSchema — providers serialise consistently).

Wiring into Anthropic / OpenAI providers is opt-in via the existing
``Config.tools`` machinery — see follow-up doc.
"""

from __future__ import annotations

import pytest

from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import ToolCall
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class _StubTool(BaseTool):
    """Tool with configurable name + description + parameters."""

    def __init__(
        self, name: str, description: str, parameters: dict | None = None
    ) -> None:
        self._name = name
        self._description = description
        self._parameters = parameters or {
            "type": "object",
            "properties": {
                "x": {"type": "string"},
                "y": {"type": "integer"},
                "z": {"type": "boolean"},
            },
            "required": ["x"],
        }

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self._name,
            description=self._description,
            parameters=self._parameters,
        )

    async def execute(self, call: ToolCall):
        return None  # never invoked


@pytest.fixture
def registry_with_tools() -> ToolRegistry:
    r = ToolRegistry()
    r.register(_StubTool("Edit", "Modify a file in place. Accepts old/new strings and validates uniqueness."))
    r.register(_StubTool("Read", "Read a file's contents into the conversation context."))
    r.register(_StubTool("Bash", "Run a shell command with timeout. Respects approval rules and per-pattern denylist for destructive commands like rm -rf."))
    r.register(_StubTool("Grep", "Search files for a regex pattern."))
    return r


# ─── tool_summaries ────────────────────────────────────────────────────


def test_summaries_returns_one_dict_per_tool(registry_with_tools):
    summaries = registry_with_tools.tool_summaries()
    assert isinstance(summaries, list)
    assert len(summaries) == 4
    assert all(isinstance(s, dict) for s in summaries)


def test_summaries_have_name_and_description_only(registry_with_tools):
    """Each entry has exactly ``name`` and ``description`` — no
    parameters block to keep the token cost minimal."""
    summaries = registry_with_tools.tool_summaries()
    for s in summaries:
        assert set(s.keys()) == {"name", "description"}
        assert isinstance(s["name"], str)
        assert isinstance(s["description"], str)


def test_summaries_truncate_long_descriptions_default(registry_with_tools):
    """Default max_description_len is 80; longer descriptions truncated."""
    summaries = registry_with_tools.tool_summaries()
    bash_summary = next(s for s in summaries if s["name"] == "Bash")
    # The seeded Bash description is >80 chars.
    assert len(bash_summary["description"]) <= 80 + 1  # +1 for ellipsis byte
    assert bash_summary["description"].endswith("…") or bash_summary["description"].endswith("...")


def test_summaries_short_descriptions_pass_through(registry_with_tools):
    summaries = registry_with_tools.tool_summaries()
    grep_summary = next(s for s in summaries if s["name"] == "Grep")
    # Short description preserved exactly.
    assert grep_summary["description"] == "Search files for a regex pattern."


def test_summaries_custom_max_length(registry_with_tools):
    """Caller can override the truncation cap (e.g. 200 for less aggressive)."""
    summaries = registry_with_tools.tool_summaries(max_description_len=200)
    bash_summary = next(s for s in summaries if s["name"] == "Bash")
    # Full description fits under 200.
    assert "rm -rf" in bash_summary["description"]


def test_summaries_zero_max_means_name_only(registry_with_tools):
    """``max_description_len=0`` strips descriptions entirely — name-only."""
    summaries = registry_with_tools.tool_summaries(max_description_len=0)
    for s in summaries:
        assert s["description"] == ""


def test_summaries_negative_max_clamps_to_zero(registry_with_tools):
    """Adversarial: negative cap clamps to 0, not a crash."""
    summaries = registry_with_tools.tool_summaries(max_description_len=-50)
    for s in summaries:
        assert s["description"] == ""


def test_summaries_empty_registry_returns_empty_list():
    assert ToolRegistry().tool_summaries() == []


def test_summaries_round_trip_with_tool_search():
    """The summary-mode shape is what a provider would inject; an agent
    that needs the full schema calls ToolSearch. This pins the contract:
    the names from summaries are exactly the names ToolSearch resolves."""
    r = ToolRegistry()
    r.register(_StubTool("Foo", "x"))
    r.register(_StubTool("Bar", "y"))
    summary_names = {s["name"] for s in r.tool_summaries()}
    schema_names = {s.name for s in r.schemas()}
    assert summary_names == schema_names


# ─── summary_schemas (ToolSchema-typed variant) ─────────────────────────


def test_summary_schemas_return_toolschemas(registry_with_tools):
    out = registry_with_tools.summary_schemas()
    assert all(isinstance(s, ToolSchema) for s in out)
    assert len(out) == 4


def test_summary_schemas_have_empty_parameters_block(registry_with_tools):
    """Critical: even the parameter SHAPE is dropped. The agent has
    to call ToolSearch to discover argument names. Without this, the
    token-saving promise is hollow."""
    out = registry_with_tools.summary_schemas()
    for s in out:
        # Empty-object parameters: still valid JSON-Schema, but no
        # properties block. Some providers require at least ``type``;
        # we honour that minimum.
        assert s.parameters in (
            {},
            {"type": "object"},
            {"type": "object", "properties": {}},
        )


def test_summary_schemas_preserve_description_truncation(registry_with_tools):
    out = registry_with_tools.summary_schemas(max_description_len=20)
    edit = next(s for s in out if s.name == "Edit")
    assert len(edit.description) <= 20 + 1  # +1 for ellipsis


def test_summary_schemas_empty_registry_returns_empty():
    assert ToolRegistry().summary_schemas() == []


# ─── token-size sanity ────────────────────────────────────────────────


def test_summary_mode_is_smaller_than_full_schemas(registry_with_tools):
    """The whole point: summary mode produces a smaller serialisation."""
    import json
    full_size = len(json.dumps([
        {"name": s.name, "description": s.description, "parameters": s.parameters}
        for s in registry_with_tools.schemas()
    ]))
    summary_size = len(json.dumps(registry_with_tools.tool_summaries()))
    assert summary_size < full_size
    # The reduction should be material — at least 25% smaller for a
    # 4-tool registry with realistic parameter blocks.
    assert summary_size < full_size * 0.75, (
        f"summary mode not materially smaller: {summary_size} vs {full_size}"
    )
