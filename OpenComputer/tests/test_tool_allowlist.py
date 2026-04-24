"""
III.1 — Tool allowlist on DelegateTool subagent spawn.

Claude Code's ``allowed-tools:`` frontmatter on commands
(sources/claude-code/plugins/code-review/commands/code-review.md)
declares which tools a command may invoke::

    allowed-tools: Bash(gh issue view:*), Bash(gh search:*), mcp__github_...

In OpenComputer, slash commands compose directly (they don't dispatch
tools), so the frontmatter-on-slash-command concept is a wrong-concept
for this codebase. The ACTUAL tool-dispatching surface that benefits
from an allowlist is ``DelegateTool`` — subagent spawning. When a
subagent is delegated, the parent can now declare an ``allowed_tools``
list that restricts the child loop's tool registry.

These tests pin:
* ``AgentLoop.allowed_tools`` defaulting to ``None`` = full registry.
* ``AgentLoop.allowed_tools`` as a concrete tuple/frozenset filters
  schemas emitted to the provider AND gates ``registry.dispatch``.
* Empty allowlist = no tools available.
* End-to-end: ``DelegateTool.execute(allowed_tools=[...])`` flows the
  list onto the child loop before it runs.
"""

from __future__ import annotations

import asyncio
from typing import Any

from opencomputer.agent.config import Config, LoopConfig
from opencomputer.agent.loop import AgentLoop
from opencomputer.tools.delegate import DelegateTool
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import Message, ToolCall, ToolResult
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    Usage,
)
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# ─── Shared helpers ────────────────────────────────────────────────


class _CountingTool(BaseTool):
    """Tool that records how many times it was invoked — test probe."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.calls = 0

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self._name,
            description=f"Counting tool {self._name}",
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls += 1
        return ToolResult(tool_call_id=call.id, content=f"{self._name} ran")


class _EndTurnProvider(BaseProvider):
    """Provider that never calls any tool — just returns end_turn immediately.

    The loop still goes through ``_run_one_step`` once so the schemas path
    (registry.schemas() → provider.complete tools=[...]) is exercised, and
    we can capture what was handed to the provider.
    """

    def __init__(self) -> None:
        self.captured_tools: list[ToolSchema] | None = None

    async def complete(
        self, *, model, messages, system, tools, max_tokens, temperature
    ) -> ProviderResponse:
        # Snapshot the tools list the loop handed us.
        self.captured_tools = list(tools)
        return ProviderResponse(
            message=Message(role="assistant", content="done"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    async def stream_complete(
        self, *, model, messages, system, tools, max_tokens, temperature
    ):  # pragma: no cover - not used
        resp = await self.complete(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        class _Done:
            kind = "done"

            def __init__(self, r):
                self.response = r

        yield _Done(resp)


def _tool_names(schemas: list[ToolSchema] | None) -> set[str]:
    if not schemas:
        return set()
    return {s.name for s in schemas}


# ─── 1. None = full registry (existing behavior unchanged) ──────────


def test_agentloop_allowed_tools_default_is_none_and_exposes_full_registry(
    tmp_path,
) -> None:
    """AgentLoop.allowed_tools defaults to None → all registered tools reach
    the provider (existing behavior is not regressed)."""
    import opencomputer.agent.loop as loop_mod

    real_registry = loop_mod.registry
    test_reg = ToolRegistry()
    test_reg.register(_CountingTool("Alpha"))
    test_reg.register(_CountingTool("Beta"))
    test_reg.register(_CountingTool("Gamma"))
    loop_mod.registry = test_reg
    try:
        provider = _EndTurnProvider()
        cfg = Config(
            loop=LoopConfig(max_iterations=1),
        )
        # Force SessionDB into tmp so we don't clobber the user's db.
        from opencomputer.agent.state import SessionDB

        cfg = Config(
            loop=LoopConfig(max_iterations=1),
            session=type(cfg.session)(db_path=tmp_path / "s.db"),  # type: ignore[call-arg]
        )
        loop = AgentLoop(
            provider=provider,
            config=cfg,
            db=SessionDB(tmp_path / "s.db"),
            compaction_disabled=True,
            episodic_disabled=True,
            reviewer_disabled=True,
        )
        assert loop.allowed_tools is None

        asyncio.run(loop.run_conversation("hi"))

        # All tools the test-registry had should be surfaced to the provider.
        names = _tool_names(provider.captured_tools)
        assert {"Alpha", "Beta", "Gamma"}.issubset(names)
    finally:
        loop_mod.registry = real_registry


# ─── 2. Concrete allowlist filters the schemas handed to the provider ──


def test_agentloop_allowed_tools_filters_schemas_passed_to_provider(tmp_path) -> None:
    """AgentLoop.allowed_tools = ("Alpha", "Gamma") → the provider only sees
    ``Alpha`` and ``Gamma``; ``Beta`` is invisible even though it's registered.
    """
    import opencomputer.agent.loop as loop_mod

    real_registry = loop_mod.registry
    test_reg = ToolRegistry()
    test_reg.register(_CountingTool("Alpha"))
    test_reg.register(_CountingTool("Beta"))
    test_reg.register(_CountingTool("Gamma"))
    loop_mod.registry = test_reg
    try:
        provider = _EndTurnProvider()
        from opencomputer.agent.state import SessionDB

        cfg = Config(
            loop=LoopConfig(max_iterations=1),
            session=type(Config().session)(db_path=tmp_path / "s.db"),  # type: ignore[call-arg]
        )
        loop = AgentLoop(
            provider=provider,
            config=cfg,
            db=SessionDB(tmp_path / "s.db"),
            compaction_disabled=True,
            episodic_disabled=True,
            reviewer_disabled=True,
        )
        loop.allowed_tools = frozenset({"Alpha", "Gamma"})

        asyncio.run(loop.run_conversation("hi"))
        names = _tool_names(provider.captured_tools)
        assert "Alpha" in names
        assert "Gamma" in names
        assert "Beta" not in names
    finally:
        loop_mod.registry = real_registry


# ─── 3. Empty allowlist = no tools at all ───────────────────────────


def test_agentloop_empty_allowed_tools_hides_all_tools(tmp_path) -> None:
    """Empty allowlist = child loop has NO tools (documented behavior).

    Useful when a caller wants to delegate a pure-text reasoning task
    with zero tool side effects.
    """
    import opencomputer.agent.loop as loop_mod

    real_registry = loop_mod.registry
    test_reg = ToolRegistry()
    test_reg.register(_CountingTool("Alpha"))
    test_reg.register(_CountingTool("Beta"))
    loop_mod.registry = test_reg
    try:
        provider = _EndTurnProvider()
        from opencomputer.agent.state import SessionDB

        cfg = Config(
            loop=LoopConfig(max_iterations=1),
            session=type(Config().session)(db_path=tmp_path / "s.db"),  # type: ignore[call-arg]
        )
        loop = AgentLoop(
            provider=provider,
            config=cfg,
            db=SessionDB(tmp_path / "s.db"),
            compaction_disabled=True,
            episodic_disabled=True,
            reviewer_disabled=True,
        )
        loop.allowed_tools = frozenset()  # empty allowlist

        asyncio.run(loop.run_conversation("hi"))
        names = _tool_names(provider.captured_tools)
        # Not a single registered tool should leak through.
        assert "Alpha" not in names
        assert "Beta" not in names
    finally:
        loop_mod.registry = real_registry


# ─── 4. Dispatch respects the allowlist too (not just schemas) ──────


def test_agentloop_dispatch_blocks_tools_not_in_allowlist(tmp_path) -> None:
    """Even if the model somehow calls a tool that isn't in the allowlist,
    dispatch returns an error (not a silent execution)."""
    import opencomputer.agent.loop as loop_mod

    real_registry = loop_mod.registry
    test_reg = ToolRegistry()
    beta = _CountingTool("Beta")
    test_reg.register(_CountingTool("Alpha"))
    test_reg.register(beta)
    loop_mod.registry = test_reg
    try:
        provider = _EndTurnProvider()
        from opencomputer.agent.state import SessionDB

        cfg = Config(
            loop=LoopConfig(max_iterations=1),
            session=type(Config().session)(db_path=tmp_path / "s.db"),  # type: ignore[call-arg]
        )
        loop = AgentLoop(
            provider=provider,
            config=cfg,
            db=SessionDB(tmp_path / "s.db"),
            compaction_disabled=True,
            episodic_disabled=True,
            reviewer_disabled=True,
        )
        loop.allowed_tools = frozenset({"Alpha"})
        # Directly exercise the dispatch helper — simulates the model
        # trying to invoke a disallowed tool mid-loop.
        results = asyncio.run(
            loop._dispatch_tool_calls(
                [ToolCall(id="c1", name="Beta", arguments={})],
                session_id="s",
                turn_index=0,
            )
        )
        assert len(results) == 1
        assert results[0].content is not None
        # Dispatch must have refused Beta without running it.
        assert beta.calls == 0
        assert "not allowed" in (results[0].content or "").lower() or (
            "not in allowed" in (results[0].content or "").lower()
        )
    finally:
        loop_mod.registry = real_registry


# ─── 5. DelegateTool integration: allowed_tools flows to the child ──


def test_delegate_tool_passes_allowed_tools_to_child_loop() -> None:
    """End-to-end: ``DelegateTool.execute(allowed_tools=[...])`` sets
    ``child.allowed_tools`` before the child runs."""
    captured: dict[str, Any] = {}

    class _FakeLoop:
        def __init__(self) -> None:
            self.config = Config(
                loop=LoopConfig(max_iterations=5, delegation_max_iterations=3)
            )
            self.allowed_tools = None  # parent has no filter by default

        async def run_conversation(self, user_message, runtime=None, **kw):
            # Snapshot the allowlist at the moment the subagent runs.
            captured["allowed_tools"] = self.allowed_tools

            class _R:
                class final_message:
                    content = "ok"

                session_id = "sub"

            return _R()

    DelegateTool.set_factory(lambda: _FakeLoop())
    tool = DelegateTool()
    result = asyncio.run(
        tool.execute(
            ToolCall(
                id="1",
                name="delegate",
                arguments={
                    "task": "read one file",
                    "allowed_tools": ["Read", "Grep"],
                },
            )
        )
    )
    assert not result.is_error
    # Child must have been given the requested allowlist.
    assert captured["allowed_tools"] == frozenset({"Read", "Grep"})


def test_delegate_tool_none_allowed_tools_leaves_child_unrestricted() -> None:
    """When ``allowed_tools`` is omitted, child.allowed_tools stays None —
    subagent gets the parent's full tool set (existing behavior)."""
    captured: dict[str, Any] = {}

    class _FakeLoop:
        def __init__(self) -> None:
            self.config = Config()
            self.allowed_tools = None

        async def run_conversation(self, user_message, runtime=None, **kw):
            captured["allowed_tools"] = self.allowed_tools

            class _R:
                class final_message:
                    content = "ok"

                session_id = "sub"

            return _R()

    DelegateTool.set_factory(lambda: _FakeLoop())
    tool = DelegateTool()
    result = asyncio.run(
        tool.execute(ToolCall(id="1", name="delegate", arguments={"task": "go"}))
    )
    assert not result.is_error
    assert captured["allowed_tools"] is None


def test_delegate_tool_empty_allowed_tools_is_propagated_as_empty() -> None:
    """Passing an explicit empty list ``[]`` means "no tools at all" and
    must propagate through as an empty frozenset, NOT be collapsed to None."""
    captured: dict[str, Any] = {}

    class _FakeLoop:
        def __init__(self) -> None:
            self.config = Config()
            self.allowed_tools = None

        async def run_conversation(self, user_message, runtime=None, **kw):
            captured["allowed_tools"] = self.allowed_tools

            class _R:
                class final_message:
                    content = "ok"

                session_id = "sub"

            return _R()

    DelegateTool.set_factory(lambda: _FakeLoop())
    tool = DelegateTool()
    result = asyncio.run(
        tool.execute(
            ToolCall(
                id="1",
                name="delegate",
                arguments={"task": "pure reasoning only", "allowed_tools": []},
            )
        )
    )
    assert not result.is_error
    # Explicit empty list = frozenset() — not None.
    assert captured["allowed_tools"] == frozenset()
    assert captured["allowed_tools"] is not None


def test_delegate_tool_schema_advertises_allowed_tools_parameter() -> None:
    """The DelegateTool schema must declare ``allowed_tools`` as an optional
    input so the model can discover it exists (same as Claude Code's
    frontmatter making the allowlist a first-class declarable)."""
    tool = DelegateTool()
    params = tool.schema.parameters
    props = params.get("properties", {})
    assert "allowed_tools" in props
    # It must be optional, not required.
    assert "allowed_tools" not in params.get("required", [])
    # Document-as-array-of-strings so the JSON schema is honest.
    assert props["allowed_tools"].get("type") == "array"
