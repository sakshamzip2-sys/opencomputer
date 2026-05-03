"""Async stream wrapper that extracts ``<think>...</think>`` blocks out
of text-delta events.

Used by the agent loop when the active provider does NOT have native
extended-thinking support for the current model (e.g. gpt-4o,
OpenRouter routes to non-thinking models, local Llama, legacy Claude
3.x). A complementary :class:`ThinkingInjector` adds a system-prompt
instruction telling the model to use these tags; this parser then
transparently routes the contents to the existing
``thinking_callback`` chain so the StreamingRenderer + ReasoningStore
pipeline pick them up unchanged.

State machine (per stream):
    - ``_in_thinking: bool`` — whether the next text bytes belong
      inside a thinking block.
    - ``_partial: str`` — bytes held back from emission because they
      MIGHT be the start of a tag whose closure hasn't arrived yet.

Tag-boundary safety: tags can split arbitrarily across chunk
boundaries (``<th`` then ``ink>``). We hold back at most
``len("</think>")`` chars between iterations, then on the next chunk
concatenate and re-scan.

False-positive safety: text like ``if x < think_max:`` contains ``<th``
but is NOT a tag start. The hold-back logic is conservative — it only
holds bytes that EXACTLY match a non-empty prefix of the tag. ``<th_``
is not a prefix of ``<think>``, so the parser emits it immediately.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from plugin_sdk.provider_contract import StreamEvent


_OPEN_TAG = "<think>"
_CLOSE_TAG = "</think>"


class ThinkingTagsParser:
    """Wraps an ``AsyncIterator[StreamEvent]`` and extracts thinking
    tags from ``text_delta`` events. Other event kinds pass through
    unchanged."""

    def __init__(self) -> None:
        self._in_thinking = False
        self._partial = ""

    async def wrap(
        self, source: AsyncIterator[StreamEvent]
    ) -> AsyncIterator[StreamEvent]:
        async for event in source:
            if event.kind != "text_delta":
                # Non-text events pass through unchanged. If the stream
                # ends, flush any held-back partial bytes first.
                if event.kind == "done":
                    async for flush in self._flush():
                        yield flush
                yield event
                continue

            text = self._partial + (event.text or "")
            self._partial = ""

            # Walk through the text emitting events in order. Loop
            # because one chunk can contain multiple tag transitions.
            while text:
                if self._in_thinking:
                    close_at = text.find(_CLOSE_TAG)
                    if close_at == -1:
                        # No close tag in this chunk. Emit everything
                        # except the trailing partial-tag suffix.
                        emit, hold = _split_with_tag_buffer(text, _CLOSE_TAG)
                        if emit:
                            yield StreamEvent(
                                kind="thinking_delta", text=emit
                            )
                        self._partial = hold
                        text = ""
                    else:
                        # Found close tag.
                        if close_at > 0:
                            yield StreamEvent(
                                kind="thinking_delta",
                                text=text[:close_at],
                            )
                        text = text[close_at + len(_CLOSE_TAG):]
                        self._in_thinking = False
                else:
                    open_at = text.find(_OPEN_TAG)
                    if open_at == -1:
                        # No open tag in this chunk. Emit everything
                        # except the trailing partial-tag suffix.
                        emit, hold = _split_with_tag_buffer(text, _OPEN_TAG)
                        if emit:
                            yield StreamEvent(
                                kind="text_delta", text=emit
                            )
                        self._partial = hold
                        text = ""
                    else:
                        # Found open tag.
                        if open_at > 0:
                            yield StreamEvent(
                                kind="text_delta", text=text[:open_at],
                            )
                        text = text[open_at + len(_OPEN_TAG):]
                        self._in_thinking = True

    async def _flush(self) -> AsyncIterator[StreamEvent]:
        """Emit any held-back partial buffer at stream end."""
        if not self._partial:
            return
        if self._in_thinking:
            yield StreamEvent(kind="thinking_delta", text=self._partial)
        else:
            yield StreamEvent(kind="text_delta", text=self._partial)
        self._partial = ""


def _split_with_tag_buffer(text: str, tag: str) -> tuple[str, str]:
    """Split ``text`` so the trailing portion that COULD be the start
    of ``tag`` is held back for the next chunk.

    Example: tag=``<think>``, text=``hello <th`` → emit=``hello ``,
    hold=``<th``. Next chunk ``ink>`` will be concatenated and the full
    tag detected.

    Conservative: only holds back if the tail genuinely matches a
    non-empty prefix of the tag. Avoids stalling on text like
    ``hello !`` where no part of ``!`` could ever be a tag start, and
    on text like ``< think_max`` where the ``<`` is followed by a space
    so it can't be a tag start.
    """
    n = len(tag)
    # Find the longest tag-prefix that is a suffix of text.
    for k in range(min(n - 1, len(text)), 0, -1):
        if text.endswith(tag[:k]):
            return text[:-k], text[-k:]
    return text, ""


__all__ = ["ThinkingTagsParser"]
