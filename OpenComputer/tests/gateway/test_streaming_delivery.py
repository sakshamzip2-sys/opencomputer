"""A1 — live-streaming agent replies into an editable chat message."""
from __future__ import annotations

import pytest

from opencomputer.gateway.streaming_delivery import StreamingDelivery
from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, SendResult


class _FakeEditAdapter(BaseChannelAdapter):
    """Edit-capable adapter stub recording every send / edit."""

    platform = Platform.TELEGRAM
    capabilities = ChannelCapabilities.EDIT_MESSAGE
    max_message_length = 4096

    def __init__(self, *, fail_edit: bool = False, fail_send: bool = False):
        self.sends: list[str] = []
        self.edits: list[str] = []
        self._fail_edit = fail_edit
        self._fail_send = fail_send
        self._next_id = 0

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None: ...

    async def send(self, chat_id: str, text: str, **kw) -> SendResult:
        if self._fail_send:
            raise RuntimeError("send boom")
        self.sends.append(text)
        self._next_id += 1
        return SendResult(success=True, message_id=f"m{self._next_id}")

    async def send_typing(self, chat_id: str) -> None: ...

    async def edit_message(
        self, chat_id: str, message_id: str, text: str, **kw
    ) -> SendResult:
        if self._fail_edit:
            raise RuntimeError("edit boom")
        self.edits.append(text)
        return SendResult(success=True, message_id=message_id)


@pytest.mark.asyncio
async def test_start_sends_placeholder_and_activates():
    adapter = _FakeEditAdapter()
    sd = StreamingDelivery(adapter, "chat1")
    assert await sd.start() is True
    assert sd.active is True
    assert adapter.sends == ["…"]  # the placeholder


@pytest.mark.asyncio
async def test_start_returns_false_when_send_fails():
    sd = StreamingDelivery(_FakeEditAdapter(fail_send=True), "chat1")
    assert await sd.start() is False
    assert sd.active is False


@pytest.mark.asyncio
async def test_finalize_edits_message_to_final_text():
    adapter = _FakeEditAdapter()
    sd = StreamingDelivery(adapter, "chat1")
    await sd.start()
    sd.feed("hello ")
    sd.feed("world")
    delivered = await sd.finalize("hello world")
    assert delivered is True
    # The live message ends on the fully-formatted final text.
    assert adapter.edits[-1] == "hello world"
    # No duplicate send — only the placeholder went out.
    assert adapter.sends == ["…"]


@pytest.mark.asyncio
async def test_mid_stream_block_is_edited_live():
    adapter = _FakeEditAdapter()
    sd = StreamingDelivery(adapter, "chat1")
    await sd.start()
    # A paragraph boundary makes the chunker emit mid-stream.
    sd.feed("first paragraph\n\n")
    sd.feed("second paragraph")
    await sd.finalize("first paragraph\n\nsecond paragraph")
    # At least one edit landed before finalize (the live block), and the
    # last edit is the final text.
    assert len(adapter.edits) >= 2
    assert adapter.edits[-1] == "first paragraph\n\nsecond paragraph"


@pytest.mark.asyncio
async def test_over_cap_final_splits_into_followup_sends():
    adapter = _FakeEditAdapter()
    adapter.max_message_length = 100
    sd = StreamingDelivery(adapter, "chat1")
    await sd.start()
    long_text = "x" * 250
    delivered = await sd.finalize(long_text)
    assert delivered is True
    # First part edits the placeholder; the remainder is sent as
    # follow-up messages (chunk_text adds (n/m) markers, so compare the
    # content payload, not the framed strings).
    assert len(adapter.edits) >= 1
    assert len(adapter.sends) >= 2  # placeholder + at least one follow-up
    total_x = adapter.edits[-1].count("x") + sum(
        s.count("x") for s in adapter.sends[1:]
    )
    assert total_x == 250


@pytest.mark.asyncio
async def test_mid_stream_edit_failure_degrades_but_finalizes():
    """An edit failure mid-stream (rate limit) stops live edits; the
    finalize still has to deliver the whole reply."""
    adapter = _FakeEditAdapter(fail_edit=True)
    sd = StreamingDelivery(adapter, "chat1")
    await sd.start()
    sd.feed("para one\n\n")
    sd.feed("para two")
    delivered = await sd.finalize("para one\n\npara two")
    # Every edit failed → finalize could not deliver → caller falls back.
    assert delivered is False


@pytest.mark.asyncio
async def test_finalize_without_start_returns_false():
    sd = StreamingDelivery(_FakeEditAdapter(), "chat1")
    assert await sd.finalize("anything") is False
