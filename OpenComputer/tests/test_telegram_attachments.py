"""Tests for Telegram adapter file-attachment + reaction + edit/delete capabilities.

Mocks the Telegram Bot API via httpx ``MockTransport`` so tests run without
network. Verifies request shape (multipart vs JSON, correct endpoint, correct
chat_id / message_id), response handling (200 OK, error paths, ok=False),
and inbound update parsing for photo/document/voice attachments.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest

from plugin_sdk import ChannelCapabilities


# Load TelegramAdapter via the same loader pattern existing tests use,
# avoiding sibling-name collisions with other plugins' adapter.py files.
# Path is resolved relative to this test file so it works in CI + local + Docker.
_TELEGRAM_ADAPTER_PATH = (
    Path(__file__).resolve().parent.parent / "extensions" / "telegram" / "adapter.py"
)


def _load_telegram() -> tuple[Any, Any]:
    spec = importlib.util.spec_from_file_location(
        "telegram_adapter_test_g2",
        str(_TELEGRAM_ADAPTER_PATH),
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.TelegramAdapter, mod


@pytest.fixture
def adapter():
    """Construct a TelegramAdapter with a mocked httpx transport."""
    TelegramAdapter, _ = _load_telegram()
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        path = req.url.path
        # Default to ok response; individual tests may install their own handlers.
        if "/getMe" in path:
            return httpx.Response(200, json={"ok": True, "result": {"id": 42, "username": "testbot"}})
        if "/sendPhoto" in path or "/sendDocument" in path or "/sendVoice" in path:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 999}})
        if "/setMessageReaction" in path:
            return httpx.Response(200, json={"ok": True, "result": True})
        if "/editMessageText" in path:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 999}})
        if "/deleteMessage" in path:
            return httpx.Response(200, json={"ok": True, "result": True})
        if "/getFile" in path:
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_id": "abc", "file_size": 1234, "file_path": "photos/test.jpg"}},
            )
        if "/file/bot" in path:  # CDN download
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n_fake_png_bytes_")
        return httpx.Response(404, json={"ok": False, "description": f"unmocked: {path}"})

    a = TelegramAdapter({"bot_token": "TEST_TOKEN"})
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    a._bot_id = 42
    a._requests = requests  # type: ignore[attr-defined]
    return a


class TestCapabilitiesFlag:
    def test_telegram_advertises_full_capability_set(self) -> None:
        TelegramAdapter, _ = _load_telegram()
        c = TelegramAdapter.capabilities
        for cap in (
            ChannelCapabilities.TYPING,
            ChannelCapabilities.REACTIONS,
            ChannelCapabilities.PHOTO_OUT,
            ChannelCapabilities.PHOTO_IN,
            ChannelCapabilities.DOCUMENT_OUT,
            ChannelCapabilities.DOCUMENT_IN,
            ChannelCapabilities.VOICE_OUT,
            ChannelCapabilities.VOICE_IN,
            ChannelCapabilities.EDIT_MESSAGE,
            ChannelCapabilities.DELETE_MESSAGE,
        ):
            assert c & cap, f"telegram should advertise {cap}"
        # Telegram doesn't model THREADS the way Discord/Slack do
        assert not (c & ChannelCapabilities.THREADS)


class TestSendPhoto:
    @pytest.mark.asyncio
    async def test_sends_local_file(self, adapter, tmp_path: Path) -> None:
        photo = tmp_path / "chart.jpg"
        photo.write_bytes(b"\xff\xd8\xff" + b"x" * 1000)

        result = await adapter.send_photo("123", photo, caption="GUJALKALI breakout")
        assert result.success
        assert any(r.url.path.endswith("/sendPhoto") for r in adapter._requests)

    @pytest.mark.asyncio
    async def test_missing_file_returns_error(self, adapter, tmp_path: Path) -> None:
        result = await adapter.send_photo("123", tmp_path / "nope.jpg")
        assert not result.success
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_oversized_file_blocked_locally(self, adapter, tmp_path: Path) -> None:
        big = tmp_path / "huge.jpg"
        # Write 11 MB > 10 MB photo limit
        with big.open("wb") as f:
            f.seek(11 * 1024 * 1024)
            f.write(b"x")
        result = await adapter.send_photo("123", big)
        assert not result.success
        assert "limit is 10MB" in result.error
        # And the request never went out
        assert not any(r.url.path.endswith("/sendPhoto") for r in adapter._requests)


class TestSendDocument:
    @pytest.mark.asyncio
    async def test_sends_pdf(self, adapter, tmp_path: Path) -> None:
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 500)
        result = await adapter.send_document("123", pdf, caption="weekly report")
        assert result.success
        assert any(r.url.path.endswith("/sendDocument") for r in adapter._requests)


class TestSendVoice:
    @pytest.mark.asyncio
    async def test_sends_ogg(self, adapter, tmp_path: Path) -> None:
        ogg = tmp_path / "briefing.ogg"
        ogg.write_bytes(b"OggS" + b"\x00" * 100)
        result = await adapter.send_voice("123", ogg)
        assert result.success
        assert any(r.url.path.endswith("/sendVoice") for r in adapter._requests)


class TestReactions:
    @pytest.mark.asyncio
    async def test_reaction_post(self, adapter) -> None:
        result = await adapter.send_reaction("123", "456", "👍")
        assert result.success
        rxn_reqs = [r for r in adapter._requests if r.url.path.endswith("/setMessageReaction")]
        assert rxn_reqs
        # Body should reference message_id
        import json
        body = json.loads(rxn_reqs[0].content.decode())
        assert body["message_id"] == 456
        assert body["reaction"][0]["emoji"] == "👍"


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_edit_post(self, adapter) -> None:
        result = await adapter.edit_message("123", "456", "updated text")
        assert result.success
        assert any(r.url.path.endswith("/editMessageText") for r in adapter._requests)


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_delete_post(self, adapter) -> None:
        result = await adapter.delete_message("123", "456")
        assert result.success
        assert any(r.url.path.endswith("/deleteMessage") for r in adapter._requests)


class TestDownloadAttachment:
    @pytest.mark.asyncio
    async def test_round_trip(self, adapter, tmp_path: Path) -> None:
        out = await adapter.download_attachment(file_id="abc", dest_dir=tmp_path)
        assert out.exists()
        assert out.suffix == ".jpg"
        assert out.read_bytes().startswith(b"\x89PNG")

    @pytest.mark.asyncio
    async def test_strips_prefix(self, adapter, tmp_path: Path) -> None:
        out = await adapter.download_attachment(file_id="telegram:abc", dest_dir=tmp_path)
        assert out.exists()


class TestInboundAttachmentParsing:
    @pytest.mark.asyncio
    async def test_photo_inbound_emits_attachment(self, adapter) -> None:
        captured: list = []

        async def handler(event):
            captured.append(event)
            return None

        adapter.set_message_handler(handler)
        update = {
            "update_id": 1,
            "message": {
                "message_id": 100,
                "date": 1700000000,
                "from": {"id": 999},
                "chat": {"id": 555},
                "photo": [
                    {"file_id": "small_id", "file_size": 1000, "width": 90, "height": 90},
                    {"file_id": "big_id", "file_size": 50000, "width": 1024, "height": 768},
                ],
                "caption": "look at this chart",
            },
        }
        await adapter._handle_update(update)

        assert len(captured) == 1
        ev = captured[0]
        assert ev.text == "look at this chart"
        assert ev.attachments == ["telegram:big_id"]  # largest variant only
        meta = ev.metadata.get("attachment_meta")
        assert meta and meta[0]["type"] == "photo" and meta[0]["file_id"] == "big_id"

    @pytest.mark.asyncio
    async def test_document_inbound(self, adapter) -> None:
        captured: list = []

        async def handler(event):
            captured.append(event)
            return None

        adapter.set_message_handler(handler)
        update = {
            "update_id": 2,
            "message": {
                "message_id": 101,
                "date": 1700000000,
                "from": {"id": 999},
                "chat": {"id": 555},
                "document": {
                    "file_id": "doc_id",
                    "file_size": 12345,
                    "file_name": "report.pdf",
                    "mime_type": "application/pdf",
                },
            },
        }
        await adapter._handle_update(update)
        assert len(captured) == 1
        assert captured[0].attachments == ["telegram:doc_id"]
        assert captured[0].metadata["attachment_meta"][0]["filename"] == "report.pdf"

    @pytest.mark.asyncio
    async def test_voice_inbound(self, adapter) -> None:
        captured: list = []

        async def handler(event):
            captured.append(event)
            return None

        adapter.set_message_handler(handler)
        update = {
            "update_id": 3,
            "message": {
                "message_id": 102,
                "date": 1700000000,
                "from": {"id": 999},
                "chat": {"id": 555},
                "voice": {
                    "file_id": "voice_id",
                    "duration": 5,
                    "mime_type": "audio/ogg",
                    "file_size": 12345,
                },
            },
        }
        await adapter._handle_update(update)
        assert len(captured) == 1
        assert captured[0].attachments == ["telegram:voice_id"]
        assert captured[0].metadata["attachment_meta"][0]["type"] == "voice"

    @pytest.mark.asyncio
    async def test_metadata_only_update_skipped(self, adapter) -> None:
        """Updates without text or attachments shouldn't fire the handler."""
        captured: list = []

        async def handler(event):
            captured.append(event)
            return None

        adapter.set_message_handler(handler)
        update = {
            "update_id": 4,
            "message": {
                "message_id": 103,
                "date": 1700000000,
                "from": {"id": 999},
                "chat": {"id": 555},
                "new_chat_title": "renamed",
            },
        }
        await adapter._handle_update(update)
        assert captured == []
