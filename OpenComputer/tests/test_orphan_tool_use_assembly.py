"""Regression: Anthropic message assembly must never produce an orphan
``tool_result`` block.

Anthropic's API rejects any request whose ``messages[i]`` carries a
``tool_result`` content block whose ``tool_use_id`` is not present in the
preceding assistant message's ``tool_use`` blocks:

    BadRequestError: 400 - messages.N.content.0: unexpected `tool_use_id`
    found in `tool_result` blocks: toolu_xxx. Each `tool_result` block
    must have a corresponding `tool_use` block in the previous message.

Real-world hit (2026-05-03): a session with 180 messages 400'd because the
assembly pipeline somehow shipped a small payload that contained a
tool_result for a tool_use whose owning assistant turn had been dropped or
stripped. The orphan turn shape was ``role=assistant, content="",
tool_calls=[ToolCall(...)]`` — i.e. empty narration text but valid tool
calls.

These tests pin down the contract at every step that handles message
sequences, so any future filter / truncation / converter regression
fails noisily here BEFORE it can hit the wire.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.compaction import (
    CompactionConfig,
    CompactionEngine,
    CompactionResult,
)
from opencomputer.agent.loop import merge_adjacent_user_messages
from plugin_sdk import Message, ToolCall

# ─── Anthropic provider import (matches test_anthropic_thinking_resend) ───


def _import_provider():
    mod_name = "_anth_provider_orphan"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_MODE", raising=False)
    return _import_provider().AnthropicProvider()


# ─── helpers ────────────────────────────────────────────────────────────


def _assert_no_orphan_tool_results(wire: list[dict[str, Any]]) -> None:
    """Walk the wire payload and verify every ``tool_result`` block has a
    matching ``tool_use`` block in the most-recent assistant turn.

    Mirrors Anthropic's server-side validator. The validator accepts
    multiple consecutive tool_result-only user messages that reference
    the same prior assistant tool_use turn (parallel-batch fan-out) —
    walk back through them when searching for the owning assistant.
    Failure here would be a 400 in production.
    """
    def _is_tool_result_only(msg: dict[str, Any]) -> bool:
        if msg.get("role") != "user":
            return False
        c = msg.get("content")
        if not isinstance(c, list) or not c:
            return False
        return all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in c
        )

    for i, m in enumerate(wire):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            assert tool_use_id, "tool_result missing tool_use_id"
            # Walk back through consecutive tool_result-only user messages
            # to find the assistant turn that owns this tool_use_id.
            j = i - 1
            while j >= 0 and _is_tool_result_only(wire[j]):
                j -= 1
            assert j >= 0, (
                f"orphan tool_result at messages[{i}]: no preceding "
                f"assistant message to host the matching tool_use"
            )
            prev = wire[j]
            assert prev.get("role") == "assistant", (
                f"orphan tool_result at messages[{i}]: walked back to "
                f"messages[{j}] role={prev.get('role')!r}, expected assistant"
            )
            prev_content = prev.get("content")
            assert isinstance(prev_content, list), (
                f"orphan tool_result at messages[{i}]: assistant at "
                f"messages[{j}] has non-list content (cannot host "
                f"tool_use blocks); prev={prev}"
            )
            ids_in_prev = {
                b.get("id")
                for b in prev_content
                if isinstance(b, dict) and b.get("type") == "tool_use"
            }
            assert tool_use_id in ids_in_prev, (
                f"orphan tool_result at messages[{i}]: tool_use_id "
                f"{tool_use_id!r} has no matching tool_use in messages[{j}]"
            )


# ─── Phase 1: converter directly on the offending shape ────────────────


def test_assistant_with_empty_content_and_tool_calls_emits_tool_use(provider):
    """An assistant Message with ``content=""`` and a non-empty
    ``tool_calls`` list must emit a content array with a ``tool_use`` block.

    This is the row-431 shape from the production session. If the converter
    drops the message or strips the tool_calls, the immediately-following
    tool_result becomes an orphan and Anthropic 400s.
    """
    msgs = [
        Message(role="user", content="hello"),
        Message(
            role="assistant",
            content="",  # ← no narration, only tools — common for chained calls
            tool_calls=[
                ToolCall(
                    id="toolu_01Fb7pHwvT94iS7fr7RLJ9Ax",
                    name="Browser",
                    arguments={"action": "snapshot"},
                ),
            ],
        ),
        Message(
            role="tool",
            content="...28k snapshot text...",
            tool_call_id="toolu_01Fb7pHwvT94iS7fr7RLJ9Ax",
        ),
        Message(role="user", content="what next"),
    ]
    wire = provider._to_anthropic_messages(msgs)
    # The assistant turn must survive AND carry a tool_use block.
    assert len(wire) == 4
    asst_blocks = wire[1]["content"]
    assert isinstance(asst_blocks, list)
    types = [b["type"] for b in asst_blocks]
    assert "tool_use" in types
    tool_use_ids = [b["id"] for b in asst_blocks if b["type"] == "tool_use"]
    assert "toolu_01Fb7pHwvT94iS7fr7RLJ9Ax" in tool_use_ids
    # No orphan tool_result anywhere in the wire payload.
    _assert_no_orphan_tool_results(wire)


def test_merge_adjacent_user_messages_preserves_tool_pair(provider):
    """``merge_adjacent_user_messages`` must not merge a tool_result message
    (which lives on role="tool" in our canonical shape) into anything, and
    must not drop the assistant tool_use turn that precedes it.
    """
    msgs = [
        Message(role="user", content="hi"),
        Message(role="user", content="hello again"),  # mergeable with above
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="tu_x", name="Read", arguments={"path": "/a"})],
        ),
        Message(role="tool", content="file content", tool_call_id="tu_x"),
    ]
    merged = merge_adjacent_user_messages(msgs)
    # Two user messages collapsed; assistant + tool preserved verbatim.
    assert len(merged) == 3
    assert merged[1].role == "assistant"
    assert merged[1].tool_calls is not None
    assert merged[1].tool_calls[0].id == "tu_x"
    assert merged[2].role == "tool"
    # Conversion must pass the orphan check.
    wire = provider._to_anthropic_messages(merged)
    _assert_no_orphan_tool_results(wire)


# ─── Phase 2: compaction's safe-split + truncate-fallback boundaries ───


def _build_session(n_pairs: int) -> list[Message]:
    """Build a realistic session of [user, asst-with-tool, tool, ...] pairs.

    Models the row-431/432 shape repeated. Each pair is:
        - user("ask N")
        - assistant(content="", tool_calls=[tu_N])     ← empty narration!
        - tool(tool_call_id=tu_N, content=...)
    """
    msgs: list[Message] = []
    for i in range(n_pairs):
        tu = f"toolu_{i:04d}"
        msgs.extend([
            Message(role="user", content=f"ask {i}"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id=tu, name="Browser", arguments={"i": i})],
            ),
            Message(role="tool", content=f"snapshot {i}", tool_call_id=tu),
        ])
    return msgs


class _StubProvider:
    """Minimal BaseProvider-shaped stand-in for CompactionEngine.

    We don't subclass :class:`BaseProvider` because none of its abstract
    methods are reached on the failing-aux-LLM path — :meth:`_summarize`
    is monkey-patched to raise before any provider call, and
    :class:`CompactionEngine` only stashes ``provider`` as an attribute
    (no isinstance check). Duck-typing keeps the test focused on the
    fallback shape rather than provider boilerplate.
    """

    name = "stub"
    default_model = "stub-1"

    async def complete(self, **kwargs):  # pragma: no cover — never reached
        from plugin_sdk.provider_contract import ProviderResponse, Usage
        return ProviderResponse(
            message=Message(role="assistant", content="summary"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=0, output_tokens=0),
        )


def test_safe_split_index_lands_on_clean_boundary():
    """``_safe_split_index`` must return a split point whose adjacent
    messages do NOT form a tool_use / tool_result pair across the split.
    """
    eng = CompactionEngine(
        provider=_StubProvider(),
        model="claude-opus-4-7",
        config=CompactionConfig(preserve_recent=5),
    )
    msgs = _build_session(20)  # 60 messages, 20 (user, asst-tool, tool) trios
    split = eng._safe_split_index(msgs, preserve_recent=5)
    # The recent block starts at split. It must NOT begin with a tool message
    # whose matching tool_use is on the other side of the cut.
    assert split > 0
    recent = msgs[split:]
    if recent and recent[0].role == "tool":
        # If split lands on a tool message, it can only be safe if the
        # preceding assistant (also moved into recent) carries the matching
        # tool_use — but split says "recent starts here", so the assistant
        # is in old_block. That's an orphan.
        prev_in_old = msgs[split - 1] if split > 0 else None
        assert not (
            prev_in_old is not None
            and prev_in_old.role == "assistant"
            and prev_in_old.tool_calls
            and any(tc.id == recent[0].tool_call_id for tc in prev_in_old.tool_calls)
        ), (
            f"_safe_split_index orphaned tool_use_id={recent[0].tool_call_id} "
            f"at split={split}"
        )


@pytest.mark.asyncio
async def test_truncate_fallback_does_not_orphan_tool_result(provider):
    """Aux-LLM compaction failure path: ``_truncate_fallback`` blindly
    drops N oldest messages. If messages[N] is a tool_result whose
    tool_use lives at messages[N-1], we orphan it.

    Regression: the post-fallback canonical Message list must NOT begin
    with an orphan ``tool`` message (ignoring the synthetic at index 0).
    Asserting on the canonical list — not the wire payload — catches
    the bug at the compaction layer rather than relying on the
    provider-side defensive backstop to mask it.
    """
    eng = CompactionEngine(
        provider=_StubProvider(),
        model="claude-opus-4-7",
        config=CompactionConfig(preserve_recent=5, fallback_drop_count=10),
    )
    msgs = _build_session(20)  # indices 0..59 — every 3rd is a tool message

    # Force the failing-aux-LLM path. ``_summarize`` raises; ``maybe_run``
    # routes to ``_truncate_fallback``.
    async def _boom(_block):
        raise RuntimeError("aux LLM unavailable")

    eng._summarize = _boom  # type: ignore[assignment]

    # Trigger compaction by reporting a token count above threshold.
    result: CompactionResult = await eng.maybe_run(
        msgs, last_input_tokens=10**9, force=True,
    )
    assert result.did_compact, "fallback must still mark did_compact=True"
    # Surviving slice (after the synthetic at index 0).
    surviving = result.messages[1:]
    if surviving and surviving[0].role == "tool":
        owning_id = surviving[0].tool_call_id
        # The matching tool_use must live somewhere in `surviving`. If
        # it's been dropped (i.e. only `tool_call_id` matches an asst
        # NOT in surviving), that's an orphan.
        owners_in_surviving = [
            m for m in surviving
            if m.role == "assistant"
            and m.tool_calls
            and any(tc.id == owning_id for tc in m.tool_calls)
        ]
        assert owners_in_surviving, (
            "truncate_fallback orphaned a tool_result: surviving slice "
            f"begins with role='tool' tool_call_id={owning_id!r} but its "
            "matching assistant tool_use is in the dropped prefix"
        )
    # End-to-end converter check.
    wire = provider._to_anthropic_messages(result.messages)
    _assert_no_orphan_tool_results(wire)


@pytest.mark.asyncio
async def test_truncate_fallback_drop_starting_on_tool_message(provider):
    """Worst-case shape: ``fallback_drop_count`` lands EXACTLY on a tool
    message whose owning assistant tool_use is at index ``drop-1``.

    With pattern ``[user(0), asst-with-tool(0), tool(0), user(1), ...]``,
    tool messages live at indices 2, 5, 8, ... and their owning
    assistants live at the index immediately before. Setting
    ``fallback_drop_count=2`` makes the naive ``messages[2:]`` slice
    drop ``messages[1]=asst-with-tool`` while keeping
    ``messages[2]=tool`` — a textbook orphan.

    Pre-fix: this 400's the next request. Post-fix:
    :meth:`CompactionEngine._safe_drop_index` walks the boundary
    forward to a clean turn boundary, so the wire payload converts
    cleanly.
    """
    msgs = _build_session(10)  # indices 0..29; pattern user/asst/tool
    eng = CompactionEngine(
        provider=_StubProvider(),
        model="claude-opus-4-7",
        config=CompactionConfig(preserve_recent=2, fallback_drop_count=2),
    )

    async def _boom(_block):
        raise RuntimeError("aux LLM unavailable")

    eng._summarize = _boom  # type: ignore[assignment]
    result = await eng.maybe_run(msgs, last_input_tokens=10**9, force=True)
    assert result.did_compact
    # Direct shape check: the surviving slice (after the synthetic) must
    # not begin with a tool whose tool_use was dropped.
    surviving = result.messages[1:]  # skip the synthetic
    if surviving and surviving[0].role == "tool":
        # The matching tool_use must be in the surviving slice (i.e.
        # somewhere later, not before the cut).
        pytest.fail(
            "truncate fallback orphaned a tool_result: surviving slice "
            f"begins with role='tool' tool_call_id={surviving[0].tool_call_id!r}"
        )
    # End-to-end converter check.
    wire = provider._to_anthropic_messages(result.messages)
    _assert_no_orphan_tool_results(wire)


# ─── Phase 3: defensive backstop in the converter ──────────────────────


def test_orphan_tool_result_block_stripped_from_wire(provider):
    """Defense-in-depth: if upstream history is corrupt and a
    ``tool_result`` arrives with no matching ``tool_use`` in the
    immediately-preceding assistant message, the converter MUST NOT
    ship it to the wire (Anthropic 400s on orphan tool_use_ids).

    Construct an explicitly broken sequence — assistant tool_use turn
    is missing, but its ``tool_result`` survives — and assert the
    converter strips the orphan.
    """
    msgs = [
        Message(role="user", content="hi"),
        # NOTE: the matching ``assistant tool_use`` for ``orphan_id`` is
        # NOT in the list — simulates the production bug where the
        # asst-with-empty-content turn was dropped by a buggy filter.
        Message(role="tool", content="result", tool_call_id="orphan_id"),
        Message(role="user", content="continue"),
    ]
    wire = provider._to_anthropic_messages(msgs)
    # The orphan tool_result must not appear anywhere in the wire.
    for m in wire:
        content = m.get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    assert blk.get("tool_use_id") != "orphan_id", (
                        "orphan tool_result leaked through to the wire payload"
                    )
    # And the resulting wire must be valid (no orphans).
    _assert_no_orphan_tool_results(wire)


def test_orphan_only_tool_result_message_is_dropped(provider):
    """When stripping an orphan tool_result empties the message's
    content list (which is the normal case — role='tool' source rows
    convert to a single-block list), the whole message must be dropped
    so we don't ship a content-less ``user`` message.
    """
    msgs = [
        Message(role="user", content="hi"),
        Message(role="tool", content="result", tool_call_id="orphan_id"),
    ]
    wire = provider._to_anthropic_messages(msgs)
    # Only the user message should survive; the orphan tool_result
    # converted to a single-block 'user' message that's been dropped.
    assert len(wire) == 1
    assert wire[0]["role"] == "user"
    assert wire[0]["content"] == "hi"


# ─── Phase 4: end-to-end converter on a 180-message session shape ──────


def test_realistic_180_message_session_converts_cleanly(provider):
    """The exact production shape: many pairs of (asst empty-content tool_use,
    tool result). The full-history conversion must round-trip without orphans.
    """
    msgs = _build_session(60)  # 180 messages
    wire = provider._to_anthropic_messages(msgs)
    _assert_no_orphan_tool_results(wire)
    # Spot-check the row-431-equivalent shape: asst with empty content but
    # tool_calls becomes a content list (not a plain string), so the
    # tool_use block is preserved.
    asst_indices = [
        i for i, m in enumerate(wire)
        if m["role"] == "assistant" and isinstance(m["content"], list)
    ]
    assert len(asst_indices) == 60
    for i in asst_indices:
        types = [b["type"] for b in wire[i]["content"]]
        assert "tool_use" in types
