"""CC §4 — ToolSearch tool (partial: user-visible primitive).

Spec: docs/OC-FROM-CLAUDE-CODE.md §4.

This implements the *agent-facing* half of CC §4: a tool the agent
can call to discover tools and fetch schemas. The architectural
deferred-injection optimization (inject only NAMES at startup, fetch
schemas lazily) is documented as next-pass work — it requires careful
agent loop + provider edits.
"""

from __future__ import annotations

import json

import pytest

from opencomputer.tools.registry import ToolRegistry
from opencomputer.tools.tool_search import ToolSearch
from plugin_sdk.core import ToolCall
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class _StubTool(BaseTool):
    """Tool with configurable name + description for the search tests."""

    def __init__(self, name: str, description: str) -> None:
        self._name = name
        self._description = description

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self._name,
            description=self._description,
            parameters={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": [],
            },
        )

    async def execute(self, call):
        return None  # never invoked


@pytest.fixture
def isolated_registry(monkeypatch):
    """Swap the live tool registry for a fresh one with deterministic
    contents per test."""
    fresh = ToolRegistry()
    fresh.register(_StubTool("Edit", "Modify a file in place"))
    fresh.register(_StubTool("Write", "Create a new file or overwrite"))
    fresh.register(_StubTool("Read", "Read a file's contents into context"))
    fresh.register(
        _StubTool("Bash", "Run a shell command; respects approvals")
    )
    fresh.register(_StubTool("Grep", "Search for a pattern across files"))
    import opencomputer.tools.tool_search as ts_mod

    monkeypatch.setattr(ts_mod, "registry", fresh, raising=True)
    return fresh


def _result_to_dict(content: str) -> dict:
    return json.loads(content)


# ─── Exact name mode ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exact_name_returns_single_schema(isolated_registry):
    t = ToolSearch()
    result = await t.execute(ToolCall(id="c1", name="ToolSearch", arguments={"name": "Edit"}))
    payload = _result_to_dict(result.content)
    assert len(payload["matches"]) == 1
    assert payload["matches"][0]["name"] == "Edit"
    assert "Modify" in payload["matches"][0]["description"]


@pytest.mark.asyncio
async def test_exact_name_unknown_returns_empty_match_with_hint(isolated_registry):
    t = ToolSearch()
    result = await t.execute(
        ToolCall(id="c1", name="ToolSearch", arguments={"name": "NopeNope"})
    )
    payload = _result_to_dict(result.content)
    assert payload["matches"] == []
    assert "no registered tool" in payload["error"].lower()
    # NOT marked is_error (the call succeeded, just no result).
    assert result.is_error is False


@pytest.mark.asyncio
async def test_exact_name_includes_parameters(isolated_registry):
    t = ToolSearch()
    result = await t.execute(
        ToolCall(id="c1", name="ToolSearch", arguments={"name": "Edit"})
    )
    payload = _result_to_dict(result.content)
    params = payload["matches"][0]["parameters"]
    assert params["type"] == "object"
    assert "x" in params["properties"]


@pytest.mark.asyncio
async def test_exact_name_ignores_query_when_both_set(isolated_registry):
    t = ToolSearch()
    result = await t.execute(
        ToolCall(
            id="c1",
            name="ToolSearch",
            arguments={"name": "Edit", "query": "totally-different"},
        )
    )
    payload = _result_to_dict(result.content)
    # `name` wins.
    assert len(payload["matches"]) == 1
    assert payload["matches"][0]["name"] == "Edit"


# ─── Fuzzy query mode ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_matches_in_name(isolated_registry):
    t = ToolSearch()
    result = await t.execute(
        ToolCall(id="c1", name="ToolSearch", arguments={"query": "edit"})
    )
    payload = _result_to_dict(result.content)
    names = {m["name"] for m in payload["matches"]}
    assert "Edit" in names


@pytest.mark.asyncio
async def test_query_matches_in_description(isolated_registry):
    t = ToolSearch()
    result = await t.execute(
        ToolCall(id="c1", name="ToolSearch", arguments={"query": "shell"})
    )
    payload = _result_to_dict(result.content)
    names = {m["name"] for m in payload["matches"]}
    # Bash's description mentions "shell command".
    assert "Bash" in names


@pytest.mark.asyncio
async def test_query_case_insensitive(isolated_registry):
    t = ToolSearch()
    result_upper = await t.execute(
        ToolCall(id="c1", name="ToolSearch", arguments={"query": "READ"})
    )
    result_lower = await t.execute(
        ToolCall(id="c2", name="ToolSearch", arguments={"query": "read"})
    )
    p_upper = _result_to_dict(result_upper.content)
    p_lower = _result_to_dict(result_lower.content)
    assert {m["name"] for m in p_upper["matches"]} == {m["name"] for m in p_lower["matches"]}


@pytest.mark.asyncio
async def test_query_no_match(isolated_registry):
    t = ToolSearch()
    result = await t.execute(
        ToolCall(id="c1", name="ToolSearch", arguments={"query": "zzzzz"})
    )
    payload = _result_to_dict(result.content)
    assert payload["matches"] == []
    assert payload["total_registered"] == 5
    assert payload["returned"] == 0


@pytest.mark.asyncio
async def test_no_args_returns_all_capped(isolated_registry):
    """Empty / missing args → return everything (up to cap)."""
    t = ToolSearch()
    result = await t.execute(
        ToolCall(id="c1", name="ToolSearch", arguments={})
    )
    payload = _result_to_dict(result.content)
    assert payload["total_registered"] == 5
    # 5 < cap of 10, so all returned.
    assert payload["returned"] == 5


@pytest.mark.asyncio
async def test_query_caps_at_max_matches():
    """Register more than MAX_MATCHES tools; query should cap."""
    import opencomputer.tools.tool_search as ts_mod
    big = ToolRegistry()
    for i in range(20):
        big.register(_StubTool(f"Tool{i}", "shared description text"))
    import pytest
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(ts_mod, "registry", big, raising=True)
        t = ToolSearch()
        result = await t.execute(
            ToolCall(id="c1", name="ToolSearch", arguments={"query": "shared"})
        )
        payload = _result_to_dict(result.content)
        assert payload["returned"] == 10
        assert payload["capped_at"] == 10
    finally:
        monkeypatch.undo()


# ─── Schema + integration ────────────────────────────────────────────


def test_tool_schema_well_formed():
    """ToolSearch.schema is well-formed JSON Schema."""
    t = ToolSearch()
    s = t.schema
    assert s.name == "ToolSearch"
    assert "discover" in s.description.lower() or "schema" in s.description.lower()
    assert s.parameters["type"] == "object"
    assert "name" in s.parameters["properties"]
    assert "query" in s.parameters["properties"]


@pytest.mark.asyncio
async def test_no_match_is_not_error(isolated_registry):
    """An unmatched query is a 2xx with empty matches — not a tool error.
    The agent decides whether to try a different query."""
    t = ToolSearch()
    result = await t.execute(
        ToolCall(id="c1", name="ToolSearch", arguments={"query": "nothing-matches"})
    )
    assert result.is_error is False


@pytest.mark.asyncio
async def test_returned_payload_is_valid_json(isolated_registry):
    t = ToolSearch()
    result = await t.execute(
        ToolCall(id="c1", name="ToolSearch", arguments={"name": "Edit"})
    )
    # Round-trip
    parsed = json.loads(result.content)
    assert isinstance(parsed, dict)


@pytest.mark.asyncio
async def test_handles_non_serializable_parameters_gracefully():
    """A plugin tool with weird parameter values doesn't crash the
    search; the schema is reported with a fallback marker."""
    import opencomputer.tools.tool_search as ts_mod

    class _WeirdTool(BaseTool):
        @property
        def schema(self) -> ToolSchema:
            return ToolSchema(
                name="Weird",
                description="x",
                # set() is not JSON-serialisable
                parameters={"type": "object", "extra": set([1, 2, 3])},
            )

        async def execute(self, call):
            return None

    fresh = ToolRegistry()
    fresh.register(_WeirdTool())
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(ts_mod, "registry", fresh, raising=True)
        t = ToolSearch()
        result = await t.execute(
            ToolCall(id="c1", name="ToolSearch", arguments={"name": "Weird"})
        )
        payload = json.loads(result.content)
        # Fallback marker present.
        assert "error" in payload["matches"][0]["parameters"]
    finally:
        monkeypatch.undo()
