"""
III.2 — Pattern-syntax tool allowlist entries.

Extends III.1's ``allowed_tools`` (bare names, exact match) with the two
pattern syntaxes Claude Code uses in command frontmatter
(sources/claude-code/plugins/code-review/commands/code-review.md)::

    allowed-tools: Bash(gh issue view:*), Bash(gh search:*), mcp__*

The two patterns:

* ``ToolName(arg_pattern)`` — allow ``ToolName`` only when its first
  meaningful string arg matches ``arg_pattern`` (fnmatch glob). Example:
  ``Bash(gh issue view:*)`` → Bash allowed only when ``command`` starts
  with ``gh issue view``.
* ``prefix*`` — plain prefix + glob wildcard. Example: ``mcp__*`` → any
  tool whose name starts with ``mcp__``. ``DevTools*`` → any tool whose
  name starts with ``DevTools``.

Bare names (e.g. ``"Read"``) remain supported for backwards compat with
III.1 — the caller still drops them into the names set for O(1) lookup;
the ``ToolPattern`` path exists for anything that needs parsing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from opencomputer.agent.config import Config, LoopConfig
from opencomputer.agent.loop import AgentLoop
from opencomputer.tools.delegate import DelegateTool
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import Message, ToolCall, ToolResult
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage
from plugin_sdk.tool_contract import BaseTool, ToolSchema
from plugin_sdk.tool_matcher import ToolPattern, matches, parse


async def _noop_fire_blocking(*_a, **_kw):
    """Test helper: swallow any PreToolUse hook decisions so the allowlist
    path — not a plugin hook — drives the assertion under test.

    Coding-harness registers a workspace-scope PreToolUse guard that can
    preempt Write/Bash calls pointing outside the project root. The
    integration tests below exercise the allowlist for those same tool
    names but don't want the hook to win the race, so we replace
    ``hook_engine.fire_blocking`` with this no-op for the duration of
    each such test.
    """
    return None


# ─── 1. parse() unit tests ──────────────────────────────────────────


def test_parse_bare_name_returns_pattern_with_no_arg_or_prefix_match() -> None:
    """'Read' → plain name, no arg pattern, no prefix wildcard."""
    p = parse("Read")
    assert isinstance(p, ToolPattern)
    assert p.raw == "Read"
    assert p.tool_name == "Read"
    assert p.arg_pattern is None
    assert p.is_prefix is False


def test_parse_prefix_wildcard_sets_is_prefix_true_and_strips_trailing_star() -> None:
    """'mcp__*' → prefix match; tool_name stores the prefix without the star."""
    p = parse("mcp__*")
    assert p.tool_name == "mcp__"
    assert p.arg_pattern is None
    assert p.is_prefix is True


def test_parse_tool_with_arg_pattern() -> None:
    """'Bash(gh issue view:*)' → tool_name='Bash', arg_pattern='gh issue view:*'."""
    p = parse("Bash(gh issue view:*)")
    assert p.tool_name == "Bash"
    assert p.arg_pattern == "gh issue view:*"
    assert p.is_prefix is False


def test_parse_tool_with_arg_pattern_preserves_spaces_and_colons() -> None:
    p = parse("Read(/Users/*)")
    assert p.tool_name == "Read"
    assert p.arg_pattern == "/Users/*"


def test_parse_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        parse("")


def test_parse_rejects_whitespace_only() -> None:
    with pytest.raises(ValueError):
        parse("   ")


def test_parse_rejects_mismatched_parens() -> None:
    with pytest.raises(ValueError):
        parse("Bash(foo")


def test_parse_rejects_star_in_middle_of_name() -> None:
    """Only trailing '*' is supported (prefix glob). 'mc*p__' is not valid."""
    with pytest.raises(ValueError):
        parse("mc*p__")


def test_parse_strips_surrounding_whitespace() -> None:
    """Frontmatter often ends up with stray whitespace from list parsing."""
    p = parse("  Bash(gh:*)  ")
    assert p.tool_name == "Bash"
    assert p.arg_pattern == "gh:*"


# ─── 2. matches() unit tests ────────────────────────────────────────


def test_matches_bare_name_positive() -> None:
    p = parse("Read")
    assert matches(p, "Read", {"file_path": "/etc/hosts"}) is True


def test_matches_bare_name_negative() -> None:
    p = parse("Read")
    assert matches(p, "Write", {"file_path": "/etc/hosts"}) is False


def test_matches_prefix_wildcard_positive_mcp() -> None:
    p = parse("mcp__*")
    assert matches(p, "mcp__github__create_issue", {}) is True
    assert matches(p, "mcp__linear__create_task", {}) is True


def test_matches_prefix_wildcard_positive_devtools() -> None:
    p = parse("DevTools*")
    assert matches(p, "DevToolsClick", {}) is True
    assert matches(p, "DevToolsTakeScreenshot", {}) is True


def test_matches_prefix_wildcard_negative() -> None:
    p = parse("mcp__*")
    assert matches(p, "Read", {"file_path": "/tmp/x"}) is False
    assert matches(p, "Bash", {"command": "ls"}) is False


def test_matches_prefix_matches_exactly_the_prefix() -> None:
    """'mcp__*' must match 'mcp__' itself (zero extra chars) — fnmatch glob
    semantics for a trailing '*' include empty suffix."""
    p = parse("mcp__*")
    assert matches(p, "mcp__", {}) is True


def test_matches_arg_pattern_bash_positive() -> None:
    """Bash(gh issue view:*) → command starting with 'gh issue view' matches."""
    p = parse("Bash(gh issue view:*)")
    assert matches(p, "Bash", {"command": "gh issue view 123"}) is True


def test_matches_arg_pattern_bash_negative_different_command() -> None:
    p = parse("Bash(gh issue view:*)")
    assert matches(p, "Bash", {"command": "rm -rf /"}) is False


def test_matches_arg_pattern_bash_negative_wrong_tool_name() -> None:
    """Even if the args would match, a different tool name must not match."""
    p = parse("Bash(gh:*)")
    assert matches(p, "Read", {"command": "gh issue view"}) is False


def test_matches_arg_pattern_git_prefix() -> None:
    p = parse("Bash(git:*)")
    assert matches(p, "Bash", {"command": "git status"}) is True
    assert matches(p, "Bash", {"command": "git log --oneline"}) is True
    assert matches(p, "Bash", {"command": "rm -rf /"}) is False


def test_matches_arg_pattern_read_file_path() -> None:
    """Read(/Users/*) → file_path starting with /Users/ matches."""
    p = parse("Read(/Users/*)")
    assert matches(p, "Read", {"file_path": "/Users/saksham/code.py"}) is True
    assert matches(p, "Read", {"file_path": "/etc/hosts"}) is False


def test_matches_arg_pattern_grep_uses_pattern_field() -> None:
    """Grep's first meaningful arg is 'pattern', not 'command'."""
    p = parse("Grep(def *)")
    assert matches(p, "Grep", {"pattern": "def foo"}) is True
    assert matches(p, "Grep", {"pattern": "class X"}) is False


def test_matches_arg_pattern_unknown_tool_fails_closed() -> None:
    """Tools not in the hardcoded per-tool first-arg map fail closed when an
    arg pattern is set — we'd rather refuse than guess."""
    p = parse("WeirdNewTool(*)")
    assert matches(p, "WeirdNewTool", {"anything": "value"}) is False


def test_matches_arg_pattern_bash_missing_command_is_no_match() -> None:
    """If a pattern expects a field but args don't have it, don't match."""
    p = parse("Bash(ls:*)")
    assert matches(p, "Bash", {}) is False


def test_matches_arg_pattern_bash_non_string_command_is_no_match() -> None:
    """Args field must be a string — non-string values fail closed."""
    p = parse("Bash(ls:*)")
    assert matches(p, "Bash", {"command": 123}) is False


# ─── 3. Integration — AgentLoop with mixed pattern allowlist ────────


class _CountingTool(BaseTool):
    """Test probe — counts invocations; returns a synthesized ok message."""

    parallel_safe = False

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
    def __init__(self) -> None:
        self.captured_tools: list[ToolSchema] | None = None

    async def complete(
        self, *, model, messages, system, tools, max_tokens, temperature
    ) -> ProviderResponse:
        self.captured_tools = list(tools)
        return ProviderResponse(
            message=Message(role="assistant", content="done"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    async def stream_complete(
        self, *, model, messages, system, tools, max_tokens, temperature
    ):  # pragma: no cover
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


def _make_loop(tmp_path, registry_tools: list[BaseTool]):
    """Install a test registry + return an AgentLoop + provider probe."""
    import opencomputer.agent.loop as loop_mod

    test_reg = ToolRegistry()
    for t in registry_tools:
        test_reg.register(t)
    loop_mod.registry = test_reg

    from opencomputer.agent.state import SessionDB

    cfg = Config(
        loop=LoopConfig(max_iterations=1),
        session=type(Config().session)(db_path=tmp_path / "s.db"),  # type: ignore[call-arg]
    )
    provider = _EndTurnProvider()
    loop = AgentLoop(
        provider=provider,
        config=cfg,
        db=SessionDB(tmp_path / "s.db"),
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )
    return loop, provider


def test_agentloop_pattern_allowlist_schemas_include_matching_names(tmp_path) -> None:
    """allowed_tools=['Bash(git:*)', 'Read'] → provider sees both Bash and Read
    schemas (patterns with arg filters still surface the tool name to the model
    so it can discover + invoke it with an allowed arg shape)."""
    import opencomputer.agent.loop as loop_mod

    real = loop_mod.registry
    try:
        loop, provider = _make_loop(
            tmp_path,
            [_CountingTool("Bash"), _CountingTool("Read"), _CountingTool("Write")],
        )
        loop.allowed_tools = frozenset({"Bash(git:*)", "Read"})
        asyncio.run(loop.run_conversation("hi"))
        names = {s.name for s in (provider.captured_tools or [])}
        assert "Bash" in names
        assert "Read" in names
        # Write was not in the allowlist at all — must not leak.
        assert "Write" not in names
    finally:
        loop_mod.registry = real


def test_agentloop_pattern_allowlist_prefix_wildcard_surfaces_matching_tools(
    tmp_path,
) -> None:
    """allowed_tools=['mcp__*'] → every tool whose name starts with 'mcp__'
    reaches the provider; non-matching tools do not."""
    import opencomputer.agent.loop as loop_mod

    real = loop_mod.registry
    try:
        loop, provider = _make_loop(
            tmp_path,
            [
                _CountingTool("mcp__github__create_issue"),
                _CountingTool("mcp__linear__create_task"),
                _CountingTool("Bash"),
                _CountingTool("Read"),
            ],
        )
        loop.allowed_tools = frozenset({"mcp__*"})
        asyncio.run(loop.run_conversation("hi"))
        names = {s.name for s in (provider.captured_tools or [])}
        assert "mcp__github__create_issue" in names
        assert "mcp__linear__create_task" in names
        assert "Bash" not in names
        assert "Read" not in names
    finally:
        loop_mod.registry = real


def test_agentloop_pattern_allowlist_dispatch_blocks_disallowed_bash_args(
    tmp_path, monkeypatch,
) -> None:
    """allowed_tools=['Bash(git:*)']: invoking Bash with 'git status' runs;
    invoking Bash with 'rm -rf /' is refused by dispatch even though the
    schema is visible."""
    import opencomputer.agent.loop as loop_mod

    # Neutralize any plugin-registered PreToolUse hooks for this test
    # (e.g. coding-harness workspace-scope check) so we're asserting
    # purely on the allowlist path, not hook interception.
    from opencomputer.hooks.engine import engine as hook_engine

    monkeypatch.setattr(hook_engine, "fire_blocking", _noop_fire_blocking)

    real = loop_mod.registry
    try:
        bash = _CountingTool("Bash")
        loop, _provider = _make_loop(tmp_path, [bash])
        loop.allowed_tools = frozenset({"Bash(git:*)"})

        # Allowed: git subcommand.
        results = asyncio.run(
            loop._dispatch_tool_calls(
                [
                    ToolCall(
                        id="c1",
                        name="Bash",
                        arguments={"command": "git status"},
                    )
                ],
                session_id="s",
                turn_index=0,
            )
        )
        assert bash.calls == 1
        assert "Bash ran" in (results[0].content or "")

        # Refused: not a git subcommand.
        results2 = asyncio.run(
            loop._dispatch_tool_calls(
                [
                    ToolCall(
                        id="c2",
                        name="Bash",
                        arguments={"command": "rm -rf /"},
                    )
                ],
                session_id="s",
                turn_index=0,
            )
        )
        assert bash.calls == 1  # unchanged — not allowed
        assert "not allowed" in (results2[0].content or "").lower()
    finally:
        loop_mod.registry = real


def test_agentloop_pattern_allowlist_dispatch_blocks_unlisted_tools(
    tmp_path, monkeypatch,
) -> None:
    """allowed_tools=['Bash(git:*)', 'Read']: a call to a tool not in any
    pattern/name is still refused. Same shape as III.1 behavior."""
    import opencomputer.agent.loop as loop_mod
    from opencomputer.hooks.engine import engine as hook_engine

    monkeypatch.setattr(hook_engine, "fire_blocking", _noop_fire_blocking)

    real = loop_mod.registry
    try:
        write = _CountingTool("Write")
        loop, _provider = _make_loop(
            tmp_path,
            [_CountingTool("Bash"), _CountingTool("Read"), write],
        )
        loop.allowed_tools = frozenset({"Bash(git:*)", "Read"})
        results = asyncio.run(
            loop._dispatch_tool_calls(
                [
                    ToolCall(
                        id="c1",
                        name="Write",
                        arguments={"file_path": "/tmp/x", "content": "y"},
                    )
                ],
                session_id="s",
                turn_index=0,
            )
        )
        assert write.calls == 0
        assert "not allowed" in (results[0].content or "").lower()
    finally:
        loop_mod.registry = real


def test_agentloop_pattern_allowlist_dispatch_allows_bare_read(
    tmp_path, monkeypatch,
) -> None:
    """Bare names coexist with patterns — Read (bare) is still allowed when
    mixed with arg-patterned Bash."""
    import opencomputer.agent.loop as loop_mod
    from opencomputer.hooks.engine import engine as hook_engine

    monkeypatch.setattr(hook_engine, "fire_blocking", _noop_fire_blocking)

    real = loop_mod.registry
    try:
        read = _CountingTool("Read")
        loop, _provider = _make_loop(
            tmp_path, [_CountingTool("Bash"), read, _CountingTool("Write")]
        )
        loop.allowed_tools = frozenset({"Bash(git:*)", "Read"})
        results = asyncio.run(
            loop._dispatch_tool_calls(
                [
                    ToolCall(
                        id="c1",
                        name="Read",
                        arguments={"file_path": "/etc/hosts"},
                    )
                ],
                session_id="s",
                turn_index=0,
            )
        )
        assert read.calls == 1
        assert "not allowed" not in (results[0].content or "").lower()
    finally:
        loop_mod.registry = real


def test_agentloop_pattern_allowlist_mcp_wildcard_dispatch_allows_any_mcp_tool(
    tmp_path,
) -> None:
    """allowed_tools=['mcp__*']: any tool with that prefix runs; Bash refused."""
    import opencomputer.agent.loop as loop_mod

    real = loop_mod.registry
    try:
        mcp = _CountingTool("mcp__github__create_issue")
        bash = _CountingTool("Bash")
        loop, _provider = _make_loop(tmp_path, [mcp, bash])
        loop.allowed_tools = frozenset({"mcp__*"})

        # MCP tool: allowed.
        asyncio.run(
            loop._dispatch_tool_calls(
                [
                    ToolCall(
                        id="c1",
                        name="mcp__github__create_issue",
                        arguments={},
                    )
                ],
                session_id="s",
                turn_index=0,
            )
        )
        assert mcp.calls == 1

        # Bash: refused.
        results = asyncio.run(
            loop._dispatch_tool_calls(
                [
                    ToolCall(
                        id="c2",
                        name="Bash",
                        arguments={"command": "ls"},
                    )
                ],
                session_id="s",
                turn_index=0,
            )
        )
        assert bash.calls == 0
        assert "not allowed" in (results[0].content or "").lower()
    finally:
        loop_mod.registry = real


# ─── 4. DelegateTool end-to-end propagation of pattern entries ──────


def test_delegate_tool_passes_pattern_entries_through_unchanged() -> None:
    """DelegateTool accepts pattern entries in the allowed_tools list and
    forwards them verbatim — parsing happens in the child loop."""
    captured: dict[str, Any] = {}

    class _FakeLoop:
        def __init__(self) -> None:
            self.config = Config(loop=LoopConfig(max_iterations=5, delegation_max_iterations=3))
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
                arguments={
                    "task": "review a PR",
                    "allowed_tools": [
                        "Bash(gh issue view:*)",
                        "Bash(gh pr diff:*)",
                        "mcp__github_inline_comment__create_inline_comment",
                    ],
                },
            )
        )
    )
    assert not result.is_error
    assert captured["allowed_tools"] == frozenset(
        {
            "Bash(gh issue view:*)",
            "Bash(gh pr diff:*)",
            "mcp__github_inline_comment__create_inline_comment",
        }
    )
