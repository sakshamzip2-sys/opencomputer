"""Tests for resolve_channel_prompt + resolve_channel_skills (Hermes PR 2 Task 2.5).

Per-channel ephemeral system prompt and skill auto-load (with parent
fallback for threaded chats). Foundation for the DM Topics work in PR 5.
"""

from __future__ import annotations

from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import Platform


class _A(BaseChannelAdapter):
    platform = Platform.CLI

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, *a, **kw):
        return None


# ─── resolve_channel_prompt ──────────────────────────────────────────


def test_resolve_channel_prompt_direct() -> None:
    a = _A({"channel_prompts": {"chan-1": "be helpful"}})
    assert a.resolve_channel_prompt("chan-1") == "be helpful"


def test_resolve_channel_prompt_parent_fallback() -> None:
    """Threaded chats fall back to the parent's prompt when no per-thread override."""
    a = _A({"channel_prompts": {"parent": "fallback prompt"}})
    assert a.resolve_channel_prompt(
        "thread-1", parent_id="parent"
    ) == "fallback prompt"


def test_resolve_channel_prompt_direct_takes_precedence_over_parent() -> None:
    a = _A(
        {
            "channel_prompts": {
                "thread-1": "specific",
                "parent": "fallback",
            }
        }
    )
    assert (
        a.resolve_channel_prompt("thread-1", parent_id="parent") == "specific"
    )


def test_resolve_channel_prompt_none_when_unset() -> None:
    a = _A({})
    assert a.resolve_channel_prompt("any") is None


def test_resolve_channel_prompt_none_when_no_match() -> None:
    a = _A({"channel_prompts": {"other": "x"}})
    assert a.resolve_channel_prompt("not-here") is None


def test_resolve_channel_prompt_handles_no_config() -> None:
    """Adapter constructed with config=None survives."""

    class _NoConfigA(_A):
        def __init__(self) -> None:
            self.config = None  # type: ignore[assignment]
            self._message_handler = None
            self._fatal_error_code = None
            self._fatal_error_message = None
            self._fatal_error_retryable = False

    a = _NoConfigA()
    assert a.resolve_channel_prompt("any") is None


# ─── resolve_channel_skills ──────────────────────────────────────────


def test_resolve_channel_skills_direct() -> None:
    a = _A(
        {"channel_skill_bindings": {"c1": ["stock-market-analysis"]}}
    )
    assert a.resolve_channel_skills("c1") == ["stock-market-analysis"]


def test_resolve_channel_skills_parent_fallback() -> None:
    a = _A({"channel_skill_bindings": {"parent": ["skill-a", "skill-b"]}})
    assert a.resolve_channel_skills(
        "thread-1", parent_id="parent"
    ) == ["skill-a", "skill-b"]


def test_resolve_channel_skills_empty_when_unset() -> None:
    assert _A({}).resolve_channel_skills("any") == []


def test_resolve_channel_skills_returns_copy_not_reference() -> None:
    """Caller mutating the returned list MUST NOT mutate the config."""
    bindings = ["a", "b"]
    a = _A({"channel_skill_bindings": {"c1": bindings}})
    result = a.resolve_channel_skills("c1")
    result.append("c")
    # Original config unchanged.
    assert a.config["channel_skill_bindings"]["c1"] == ["a", "b"]


def test_resolve_channel_skills_empty_when_no_match() -> None:
    a = _A({"channel_skill_bindings": {"other": ["x"]}})
    assert a.resolve_channel_skills("not-here") == []
