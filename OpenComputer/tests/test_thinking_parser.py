"""ThinkingTagsParser: async stream wrapper that extracts <think>...</think>
content out of text_delta events and emits thinking_delta events for the
contents."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from opencomputer.agent.thinking_parser import ThinkingTagsParser
from plugin_sdk.provider_contract import StreamEvent


async def _to_list(it: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [e async for e in it]


async def _from_chunks(*chunks: str) -> AsyncIterator[StreamEvent]:
    for c in chunks:
        yield StreamEvent(kind="text_delta", text=c)
    yield StreamEvent(kind="done")


def _kinds(events) -> list[str]:
    return [e.kind for e in events]


def _texts(events, kind) -> str:
    return "".join((e.text or "") for e in events if e.kind == kind)


def test_passthrough_when_no_think_tags():
    """Pure text — every chunk passes through unchanged."""
    parser = ThinkingTagsParser()
    src = _from_chunks("hello ", "world")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert "done" in _kinds(out)
    assert _texts(out, "text_delta") == "hello world"


def test_extracts_single_think_block():
    parser = ThinkingTagsParser()
    src = _from_chunks("answer is <think>let me reason</think> 42")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "thinking_delta") == "let me reason"
    assert _texts(out, "text_delta") == "answer is  42"


def test_handles_open_tag_split_across_chunks():
    """The <think> tag is split mid-tag at the chunk boundary —
    parser must stitch it together via the partial buffer."""
    parser = ThinkingTagsParser()
    src = _from_chunks("hi <th", "ink>secret</think> bye")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "thinking_delta") == "secret"
    assert _texts(out, "text_delta") == "hi  bye"


def test_handles_close_tag_split_across_chunks():
    parser = ThinkingTagsParser()
    src = _from_chunks("hi <think>secret</thi", "nk> bye")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "thinking_delta") == "secret"
    assert _texts(out, "text_delta") == "hi  bye"


def test_handles_chunk_starting_inside_thinking():
    parser = ThinkingTagsParser()
    src = _from_chunks("<think>line1\n", "line2</think>done")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "thinking_delta") == "line1\nline2"
    assert _texts(out, "text_delta") == "done"


def test_multiple_think_blocks_in_one_response():
    parser = ThinkingTagsParser()
    src = _from_chunks("<think>a</think>x<think>b</think>y")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "thinking_delta") == "ab"
    assert _texts(out, "text_delta") == "xy"


def test_unclosed_think_tag_flushes_remaining_as_thinking_on_done():
    """If the model emits <think> but never </think>, on stream end we
    flush the remaining buffer as thinking. Defensive — better than
    losing the content silently."""
    parser = ThinkingTagsParser()
    src = _from_chunks("hi <think>never closes")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "text_delta") == "hi "
    assert _texts(out, "thinking_delta") == "never closes"
    # done event present at end.
    assert out[-1].kind == "done"


def test_passes_non_text_events_through_untouched():
    """tool_call, done, and other event kinds must NOT be inspected by
    the parser — they pass through verbatim."""
    parser = ThinkingTagsParser()

    async def src():
        yield StreamEvent(kind="text_delta", text="<think>x</think>y")
        yield StreamEvent(kind="done")

    out = asyncio.run(_to_list(parser.wrap(src())))
    assert _texts(out, "thinking_delta") == "x"
    assert _texts(out, "text_delta") == "y"


def test_native_thinking_delta_events_pass_through_unchanged():
    """If a native-thinking provider already emits thinking_delta
    events (and the loop wired the parser anyway by mistake), those
    events MUST pass through verbatim — parser only inspects
    text_delta."""
    parser = ThinkingTagsParser()

    async def src():
        yield StreamEvent(kind="text_delta", text="hi ")
        yield StreamEvent(kind="thinking_delta", text="native think")
        yield StreamEvent(kind="text_delta", text="bye")
        yield StreamEvent(kind="done")

    out = asyncio.run(_to_list(parser.wrap(src())))
    assert _texts(out, "thinking_delta") == "native think"
    assert _texts(out, "text_delta") == "hi bye"


def test_empty_think_block_is_dropped_cleanly():
    parser = ThinkingTagsParser()
    src = _from_chunks("a<think></think>b")
    out = asyncio.run(_to_list(parser.wrap(src)))
    # No thinking_delta event should be emitted for an empty block.
    thinking_events = [e for e in out if e.kind == "thinking_delta"]
    assert all(e.text == "" for e in thinking_events) or not thinking_events
    assert _texts(out, "text_delta") == "ab"


def test_buffer_flushes_pending_text_on_done():
    """If the stream ends with un-flushed buffer (e.g. ends mid '<th'
    that turned out NOT to be a tag), the remaining bytes flush as
    text on done."""
    parser = ThinkingTagsParser()
    src = _from_chunks("hello <th")
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "text_delta") == "hello <th"


def test_python_less_than_operator_does_not_trigger_partial_match():
    """Edge case: text like 'if x < think_max:' contains '<th' but is
    NOT a tag start. The parser conservatively holds back ONLY the
    actual tag-prefix suffix; on the next chunk if no completion comes,
    it emits the held bytes as text."""
    parser = ThinkingTagsParser()
    src = _from_chunks("if x < think_max:", " do_it()")
    out = asyncio.run(_to_list(parser.wrap(src)))
    # The whole thing is text — no thinking events.
    assert _texts(out, "thinking_delta") == ""
    assert _texts(out, "text_delta") == "if x < think_max: do_it()"


def test_think_tag_inside_fenced_code_block_is_treated_as_literal():
    """Critical: when the model writes a code example containing
    <think>, the parser must NOT consume it as thinking. Otherwise
    the model can't teach about XML, can't document this very feature,
    can't reply to 'what does the parser do?' without silent
    corruption."""
    parser = ThinkingTagsParser()
    src = _from_chunks(
        "Here is the syntax:\n```xml\n<think>example</think>\n```\nDone."
    )
    out = asyncio.run(_to_list(parser.wrap(src)))
    # All text, no thinking extraction inside the fence.
    assert _texts(out, "thinking_delta") == ""
    assert _texts(out, "text_delta") == (
        "Here is the syntax:\n```xml\n<think>example</think>\n```\nDone."
    )


def test_think_tag_outside_fence_extracted_normally_after_fence():
    """A <think> block AFTER a closed fence should still be extracted."""
    parser = ThinkingTagsParser()
    src = _from_chunks(
        "Code: ```py\n<think>literal</think>\n```\n"
        "<think>real reasoning</think>\nAnswer: 42"
    )
    out = asyncio.run(_to_list(parser.wrap(src)))
    # Inside-fence <think> is text; outside-fence <think> is thinking.
    assert _texts(out, "thinking_delta") == "real reasoning"
    text = _texts(out, "text_delta")
    assert "<think>literal</think>" in text  # inside fence preserved
    assert "<think>real reasoning</think>" not in text  # outside extracted
    assert text.endswith("Answer: 42")


def test_code_fence_split_across_chunks():
    """Triple-backtick split mid-fence — parser must hold back partial
    backticks, just like partial tags."""
    parser = ThinkingTagsParser()
    src = _from_chunks("hi `", "``code <think>x</think> code", "``` ok")
    out = asyncio.run(_to_list(parser.wrap(src)))
    # Inside the fence, <think> is literal.
    assert _texts(out, "thinking_delta") == ""
    full_text = _texts(out, "text_delta")
    assert "```code <think>x</think> code```" in full_text
    assert full_text.endswith(" ok")


def test_open_tag_before_fence_still_enters_thinking():
    """If <think> comes BEFORE a ``` block, thinking mode kicks in."""
    parser = ThinkingTagsParser()
    src = _from_chunks(
        "<think>reasoning</think>\nCode:\n```py\nx = 1\n```\nDone."
    )
    out = asyncio.run(_to_list(parser.wrap(src)))
    assert _texts(out, "thinking_delta") == "reasoning"
    text = _texts(out, "text_delta")
    assert "```py" in text and "x = 1" in text and "```" in text
    assert text.endswith("Done.")
