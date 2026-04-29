"""Tests for /<skill-name> slash dispatch fallback."""
from types import SimpleNamespace

import pytest

from opencomputer.agent.slash_dispatcher import dispatch
from opencomputer.agent.slash_skill_fallback import make_skill_fallback
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext


class _FakeMemoryManager:
    def __init__(self, skills_data: list[dict] | None = None) -> None:
        self.skills_data = skills_data or []
        self.bodies: dict[str, str] = {}

    def list_skills(self):
        return [
            SimpleNamespace(id=s["id"], name=s.get("name", s["id"]))
            for s in self.skills_data
        ]

    def load_skill_body(self, skill_id: str) -> str:
        return self.bodies.get(skill_id, "")


# ---------- fallback unit tests ----------


def test_fallback_returns_none_for_unknown_skill():
    mm = _FakeMemoryManager(skills_data=[{"id": "foo"}])
    fb = make_skill_fallback(mm)
    result = fb("nonexistent", "", DEFAULT_RUNTIME_CONTEXT)
    assert result is None


def test_fallback_resolves_known_skill():
    mm = _FakeMemoryManager(skills_data=[{"id": "pead-screener", "name": "pead-screener"}])
    mm.bodies["pead-screener"] = "## How to screen PEAD setups\n\nStep 1: ..."
    fb = make_skill_fallback(mm)
    result = fb("pead-screener", "", DEFAULT_RUNTIME_CONTEXT)
    assert result is not None
    assert "Step 1" in result.output
    assert result.handled is True


def test_fallback_matches_by_name_too():
    """Name and id can differ — match either."""
    mm = _FakeMemoryManager(skills_data=[
        {"id": "weird-id-12345", "name": "human-friendly-name"}
    ])
    mm.bodies["weird-id-12345"] = "body content here"
    fb = make_skill_fallback(mm)
    result = fb("human-friendly-name", "", DEFAULT_RUNTIME_CONTEXT)
    assert result is not None
    assert "body content here" in result.output


def test_fallback_renders_title_header():
    mm = _FakeMemoryManager(skills_data=[{"id": "foo", "name": "Foo Skill"}])
    mm.bodies["foo"] = "the body"
    fb = make_skill_fallback(mm)
    result = fb("foo", "", DEFAULT_RUNTIME_CONTEXT)
    assert result.output.startswith("## Foo Skill")


def test_fallback_truncates_huge_body():
    huge = "x" * 50_000
    mm = _FakeMemoryManager(skills_data=[{"id": "huge", "name": "huge"}])
    mm.bodies["huge"] = huge
    fb = make_skill_fallback(mm)
    result = fb("huge", "", DEFAULT_RUNTIME_CONTEXT)
    assert "truncated" in result.output.lower()
    assert len(result.output) < len(huge)


def test_fallback_handles_empty_body():
    mm = _FakeMemoryManager(skills_data=[{"id": "empty", "name": "empty"}])
    mm.bodies["empty"] = ""
    fb = make_skill_fallback(mm)
    result = fb("empty", "", DEFAULT_RUNTIME_CONTEXT)
    assert "empty body" in result.output.lower()


def test_fallback_swallows_list_skills_error():
    """A failing list_skills() shouldn't crash the dispatcher path."""
    class _Boom(_FakeMemoryManager):
        def list_skills(self):
            raise RuntimeError("DB down")

    fb = make_skill_fallback(_Boom())
    result = fb("anything", "", DEFAULT_RUNTIME_CONTEXT)
    assert result is None  # graceful — let dispatcher fall through


def test_fallback_surfaces_load_body_error():
    """If list_skills works but load_skill_body errors, surface it as a result."""
    class _Boom(_FakeMemoryManager):
        def load_skill_body(self, skill_id: str) -> str:
            raise RuntimeError("file not readable")

    mm = _Boom(skills_data=[{"id": "foo", "name": "foo"}])
    fb = make_skill_fallback(mm)
    result = fb("foo", "", DEFAULT_RUNTIME_CONTEXT)
    assert result is not None
    assert "failed to load skill" in result.output.lower()


# ---------- integration tests via dispatcher ----------


@pytest.mark.asyncio
async def test_dispatcher_no_fallback_returns_none_for_unknown():
    """Without a fallback, unknown slash falls through to None."""
    result = await dispatch("/nonexistent", {}, DEFAULT_RUNTIME_CONTEXT)
    assert result is None


@pytest.mark.asyncio
async def test_dispatcher_with_fallback_resolves_skill():
    mm = _FakeMemoryManager(skills_data=[{"id": "foo", "name": "foo"}])
    mm.bodies["foo"] = "the foo skill body"
    fb = make_skill_fallback(mm)
    result = await dispatch("/foo", {}, DEFAULT_RUNTIME_CONTEXT, fallback=fb)
    assert result is not None
    assert "the foo skill body" in result.output


@pytest.mark.asyncio
async def test_dispatcher_with_fallback_unknown_skill_still_returns_none():
    mm = _FakeMemoryManager(skills_data=[{"id": "foo", "name": "foo"}])
    fb = make_skill_fallback(mm)
    result = await dispatch("/never-heard-of-it", {}, DEFAULT_RUNTIME_CONTEXT, fallback=fb)
    assert result is None


@pytest.mark.asyncio
async def test_dispatcher_primary_command_wins_over_fallback():
    """If the primary registry has /foo, the fallback is NOT consulted."""
    primary_called = {"yes": False}

    class _PrimaryFoo:
        async def execute(self, args, runtime):
            primary_called["yes"] = True
            from plugin_sdk.slash_command import SlashCommandResult
            return SlashCommandResult(output="from primary", handled=True)

    fallback_called = {"yes": False}

    def _fallback(name, args, runtime):
        fallback_called["yes"] = True
        return None

    result = await dispatch(
        "/foo", {"foo": _PrimaryFoo()}, DEFAULT_RUNTIME_CONTEXT, fallback=_fallback
    )
    assert primary_called["yes"] is True
    assert fallback_called["yes"] is False
    assert "from primary" in result.output


@pytest.mark.asyncio
async def test_dispatcher_fallback_exception_caught():
    def _bad_fallback(name, args, runtime):
        raise RuntimeError("boom")

    result = await dispatch(
        "/anything", {}, DEFAULT_RUNTIME_CONTEXT, fallback=_bad_fallback
    )
    assert result is not None
    assert "boom" in result.output.lower() or "RuntimeError" in result.output


@pytest.mark.asyncio
async def test_dispatcher_fallback_returning_string_is_wrapped():
    def _str_fallback(name, args, runtime):
        return f"plain string for {name}"

    result = await dispatch(
        "/anything", {}, DEFAULT_RUNTIME_CONTEXT, fallback=_str_fallback
    )
    assert result is not None
    assert "plain string for anything" in result.output


@pytest.mark.asyncio
async def test_dispatcher_async_fallback_supported():
    async def _async_fallback(name, args, runtime):
        from plugin_sdk.slash_command import SlashCommandResult
        return SlashCommandResult(output="async-resolved", handled=True)

    result = await dispatch(
        "/anything", {}, DEFAULT_RUNTIME_CONTEXT, fallback=_async_fallback
    )
    assert result is not None
    assert "async-resolved" in result.output


# ─── Task 10: SlashCommandResult.source field ─────────────────────


def test_slash_command_result_source_defaults_to_command() -> None:
    """Backwards-compat: existing call sites that don't pass source
    get source='command'."""
    from plugin_sdk.slash_command import SlashCommandResult

    r = SlashCommandResult(output="hi")
    assert r.source == "command"


def test_slash_command_result_source_can_be_skill() -> None:
    """source='skill' is the marker for the Hybrid dispatch path."""
    from plugin_sdk.slash_command import SlashCommandResult

    r = SlashCommandResult(output="x", source="skill")
    assert r.source == "skill"


def test_slash_command_result_source_type_is_literal() -> None:
    """Source field is restricted to 'command' or 'skill'."""
    from typing import get_args, get_type_hints

    from plugin_sdk.slash_command import SlashCommandResult

    hints = get_type_hints(SlashCommandResult)
    source_type = hints["source"]
    # Literal["command", "skill"]
    assert "command" in get_args(source_type)
    assert "skill" in get_args(source_type)


# ─── Task 11: fallback marks results source="skill" ───────────────


def test_skill_fallback_result_has_source_skill():
    """The fallback closure must mark its result with source='skill'
    so the agent loop's Hybrid wrap fires."""
    mm = _FakeMemoryManager(skills_data=[{"id": "hello"}])
    mm.bodies["hello"] = "body"
    fallback = make_skill_fallback(mm)
    result = fallback("hello", "", DEFAULT_RUNTIME_CONTEXT)
    assert result is not None
    assert result.source == "skill"


def test_skill_fallback_empty_body_marked_skill():
    """Empty body returns an error-shaped result — must still be
    source='skill' so the agent loop knows it came from the fallback."""
    mm = _FakeMemoryManager(skills_data=[{"id": "empty"}])
    mm.bodies["empty"] = ""  # empty body → fallback returns error result
    fallback = make_skill_fallback(mm)
    result = fallback("empty", "", DEFAULT_RUNTIME_CONTEXT)
    assert result is not None
    assert result.source == "skill"


def test_skill_fallback_load_failure_marked_skill():
    """If load_skill_body raises, fallback returns an error-marked
    SlashCommandResult — must still be source='skill'."""

    class _BoomMemory:
        def list_skills(self):
            from types import SimpleNamespace

            return [SimpleNamespace(id="exploding", name="exploding")]

        def load_skill_body(self, sid):
            raise RuntimeError("boom")

    fallback = make_skill_fallback(_BoomMemory())
    result = fallback("exploding", "", DEFAULT_RUNTIME_CONTEXT)
    assert result is not None
    assert result.source == "skill"
    assert "failed to load skill" in result.output
