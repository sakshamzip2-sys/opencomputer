"""M3 #10 fix — compaction preserves the session's opening anchor.

A long-lived gateway session is compacted many times; each pass
re-summarises the previous summary, so the conversation's *origin* (the
first user message — "what this is about") degrades turn after turn.
``preserve_anchor`` holds that first user message out of every
compaction verbatim, so the early context is never lost.
"""
from __future__ import annotations

import pytest

from opencomputer.agent.compaction import CompactionConfig, CompactionEngine
from plugin_sdk.core import Message

_ANCHOR = "ANCHOR: help me build the Foobar trading dashboard in Rust"


class _StubProvider:
    """Returns a fixed summary so the aux-LLM success path is exercised."""

    name = "stub"
    default_model = "stub-1"

    async def complete(self, **kwargs):
        from plugin_sdk.provider_contract import ProviderResponse, Usage

        return ProviderResponse(
            message=Message(role="assistant", content="SUMMARY-OF-OLD-BLOCK"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=0, output_tokens=0),
        )


def _session(n_pairs: int) -> list[Message]:
    """Anchor user message + ``n_pairs`` plain user/assistant exchanges."""
    msgs = [Message(role="user", content=_ANCHOR)]
    for i in range(n_pairs):
        msgs.append(Message(role="user", content=f"q{i}"))
        msgs.append(Message(role="assistant", content=f"a{i}"))
    return msgs


def _engine(**cfg) -> CompactionEngine:
    return CompactionEngine(
        provider=_StubProvider(),
        model="claude-opus-4-7",
        config=CompactionConfig(preserve_recent=5, **cfg),
    )


@pytest.mark.asyncio
async def test_compaction_keeps_the_anchor_verbatim() -> None:
    eng = _engine()
    result = await eng.maybe_run(_session(30), last_input_tokens=10**9, force=True)

    assert result.did_compact is True
    # The first message is the untouched anchor — not a summary.
    assert result.messages[0].role == "user"
    assert result.messages[0].content == _ANCHOR
    # The summary follows the anchor.
    assert "[compacted-summary]" in result.messages[1].content


@pytest.mark.asyncio
async def test_anchor_survives_repeated_compaction() -> None:
    """Two compactions in a row — the anchor stays verbatim, no drift."""
    eng = _engine()
    once = await eng.maybe_run(_session(30), last_input_tokens=10**9, force=True)
    # Grow the once-compacted history and compact again.
    grown = once.messages + [
        Message(role="user", content=f"more{i}") for i in range(40)
    ]
    twice = await eng.maybe_run(grown, last_input_tokens=10**9, force=True)

    assert twice.did_compact is True
    assert twice.messages[0].content == _ANCHOR  # still byte-identical


@pytest.mark.asyncio
async def test_preserve_anchor_false_restores_legacy_behaviour() -> None:
    eng = _engine(preserve_anchor=False)
    result = await eng.maybe_run(_session(30), last_input_tokens=10**9, force=True)

    assert result.did_compact is True
    # Legacy: the synthetic summary is first; the anchor was summarised.
    assert "[compacted-summary]" in result.messages[0].content


@pytest.mark.asyncio
async def test_no_anchor_when_first_message_is_not_user() -> None:
    """If the history does not start with a user message (e.g. seeded
    via delegate initial_messages), anchoring is skipped — no crash."""
    eng = _engine()
    msgs = [Message(role="assistant", content="seeded")] + _session(30)[1:]
    result = await eng.maybe_run(msgs, last_input_tokens=10**9, force=True)
    assert result.did_compact is True
    # First message is the synthetic summary — nothing to anchor.
    assert "[compacted-summary]" in result.messages[0].content


@pytest.mark.asyncio
async def test_truncate_fallback_keeps_the_anchor() -> None:
    """When the aux LLM fails, the degraded truncate path still keeps
    the anchor rather than dropping it with the oldest messages."""
    eng = _engine(fallback_drop_count=10)

    async def _boom(_old):
        raise RuntimeError("aux LLM down")

    eng._summarize = _boom  # type: ignore[method-assign]
    result = await eng.maybe_run(_session(30), last_input_tokens=10**9, force=True)

    assert result.did_compact is True
    assert result.degraded is True
    assert result.messages[0].content == _ANCHOR
