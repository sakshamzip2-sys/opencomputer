"""Tests for Telegram MarkdownV2 outbound formatting + parse-error fallback (PR 3a.2).

Covers:
- ``send`` produces ``parse_mode=MarkdownV2`` and converts the text body.
- ``send`` falls back to plain text on a 400 "can't parse entities" response.
- ``send_photo`` / ``send_document`` / ``send_voice`` captions all get conversion.
- ``edit_message`` converts + falls back.
- ``send_approval_request`` converts the prompt + falls back.

httpx I/O is mocked at the ``httpx.AsyncClient`` boundary.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from extensions.telegram.adapter import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    a = TelegramAdapter({"bot_token": "test"})
    a._client = AsyncMock()
    a._bot_id = 42
    a._bot_username = "hermes_bot"
    return a


def _ok_response(message_id: int = 99) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.text = ""
    r.json.return_value = {"ok": True, "result": {"message_id": message_id}}
    return r


def _parse_error_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 400
    r.text = "Bad Request: can't parse entities: character '_' is reserved"
    r.json.return_value = {
        "ok": False,
        "description": "Bad Request: can't parse entities",
    }
    return r


# ─── send ──────────────────────────────────────────────────────────


class TestSendFormat:
    @pytest.mark.asyncio
    async def test_send_emits_markdownv2_payload(self) -> None:
        a = _make_adapter()
        a._client.post = AsyncMock(return_value=_ok_response())

        result = await a.send("123", "hello *world*")
        assert result.success is True

        a._client.post.assert_awaited_once()
        kwargs = a._client.post.call_args.kwargs
        payload = kwargs["json"]
        assert payload["parse_mode"] == "MarkdownV2"
        # Body is converted (asterisks preserved as bold, but specials escaped)
        assert "world" in payload["text"]
        # MarkdownV2 conversion preserves bold *...* but escapes the dot in
        # plain text. The original user-typed `*world*` becomes formatted.
        assert payload["chat_id"] == "123"

    @pytest.mark.asyncio
    async def test_send_escapes_specials(self) -> None:
        """A dot in plain text must be escaped to ``\\.`` for MarkdownV2."""
        a = _make_adapter()
        a._client.post = AsyncMock(return_value=_ok_response())

        await a.send("c", "version 1.2.3 here")
        payload = a._client.post.call_args.kwargs["json"]
        assert payload["parse_mode"] == "MarkdownV2"
        assert r"1\.2\.3" in payload["text"]

    @pytest.mark.asyncio
    async def test_send_falls_back_to_plain_on_parse_error(self) -> None:
        """On 400 'can't parse entities', retry with original text + no parse_mode."""
        a = _make_adapter()
        a._client.post = AsyncMock(
            side_effect=[_parse_error_response(), _ok_response()],
        )

        result = await a.send("c", "raw_text.with.dots")
        assert result.success is True

        # Two calls: first w/ MarkdownV2, second plain.
        assert a._client.post.await_count == 2
        first_payload = a._client.post.await_args_list[0].kwargs["json"]
        second_payload = a._client.post.await_args_list[1].kwargs["json"]
        assert first_payload["parse_mode"] == "MarkdownV2"
        assert "parse_mode" not in second_payload
        # Fallback uses ORIGINAL text (un-escaped).
        assert second_payload["text"] == "raw_text.with.dots"

    @pytest.mark.asyncio
    async def test_send_non_parse_error_propagates(self) -> None:
        """Other 400s (e.g. chat-not-found) must NOT trigger fallback."""
        a = _make_adapter()
        bad = MagicMock()
        bad.status_code = 400
        bad.text = "Bad Request: chat not found"
        bad.json.return_value = {"ok": False}
        a._client.post = AsyncMock(return_value=bad)

        result = await a.send("c", "hi")
        assert result.success is False
        # Single attempt — no fallback fired.
        assert a._client.post.await_count == 1


# ─── captions on send_photo / send_document / send_voice ──────────


class TestCaptionFormat:
    @pytest.mark.asyncio
    async def test_send_photo_caption_converted(self, tmp_path: Path) -> None:
        a = _make_adapter()
        a._client.post = AsyncMock(return_value=_ok_response())

        photo = tmp_path / "p.jpg"
        photo.write_bytes(b"\x89PNG\r\n\x1a\n")
        await a.send_photo("c", photo, caption="hello.world")
        kwargs = a._client.post.call_args.kwargs
        form = kwargs["data"]
        assert form["parse_mode"] == "MarkdownV2"
        assert r"hello\.world" in form["caption"]

    @pytest.mark.asyncio
    async def test_send_document_caption_converted(self, tmp_path: Path) -> None:
        a = _make_adapter()
        a._client.post = AsyncMock(return_value=_ok_response())

        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF-1.4\n")
        await a.send_document("c", doc, caption="a.b")
        kwargs = a._client.post.call_args.kwargs
        form = kwargs["data"]
        assert form["parse_mode"] == "MarkdownV2"
        assert r"a\.b" in form["caption"]

    @pytest.mark.asyncio
    async def test_send_voice_caption_converted(self, tmp_path: Path) -> None:
        a = _make_adapter()
        a._client.post = AsyncMock(return_value=_ok_response())

        v = tmp_path / "v.ogg"
        v.write_bytes(b"OggS")
        await a.send_voice("c", v, caption="ping_me")
        kwargs = a._client.post.call_args.kwargs
        form = kwargs["data"]
        assert form["parse_mode"] == "MarkdownV2"
        assert r"ping\_me" in form["caption"]

    @pytest.mark.asyncio
    async def test_caption_falls_back_on_parse_error(self, tmp_path: Path) -> None:
        a = _make_adapter()
        a._client.post = AsyncMock(
            side_effect=[_parse_error_response(), _ok_response()],
        )
        photo = tmp_path / "p.jpg"
        photo.write_bytes(b"\x89PNG\r\n\x1a\n")
        result = await a.send_photo("c", photo, caption="weird_caption.txt")
        assert result.success is True
        assert a._client.post.await_count == 2
        # Second call: no parse_mode, original caption.
        second_form = a._client.post.await_args_list[1].kwargs["data"]
        assert "parse_mode" not in second_form
        assert second_form["caption"] == "weird_caption.txt"

    @pytest.mark.asyncio
    async def test_empty_caption_no_parse_mode(self, tmp_path: Path) -> None:
        """When caption is empty, we don't add parse_mode (keeps payload clean)."""
        a = _make_adapter()
        a._client.post = AsyncMock(return_value=_ok_response())
        photo = tmp_path / "p.jpg"
        photo.write_bytes(b"\x89PNG\r\n\x1a\n")
        await a.send_photo("c", photo, caption="")
        form = a._client.post.call_args.kwargs["data"]
        assert "parse_mode" not in form
        assert "caption" not in form


# ─── edit_message ──────────────────────────────────────────────────


class TestEditMessageFormat:
    @pytest.mark.asyncio
    async def test_edit_uses_markdownv2(self) -> None:
        a = _make_adapter()
        a._client.post = AsyncMock(return_value=_ok_response())
        await a.edit_message("c", "55", "updated.text")
        kwargs = a._client.post.call_args.kwargs
        payload = kwargs["json"]
        assert payload["parse_mode"] == "MarkdownV2"
        assert r"updated\.text" in payload["text"]

    @pytest.mark.asyncio
    async def test_edit_falls_back_on_parse_error(self) -> None:
        a = _make_adapter()
        a._client.post = AsyncMock(
            side_effect=[_parse_error_response(), _ok_response()],
        )
        result = await a.edit_message("c", "55", "raw.text")
        assert result.success is True
        assert a._client.post.await_count == 2
        second = a._client.post.await_args_list[1].kwargs["json"]
        assert "parse_mode" not in second
        assert second["text"] == "raw.text"


# ─── send_approval_request ─────────────────────────────────────────


class TestApprovalPromptFormat:
    @pytest.mark.asyncio
    async def test_approval_prompt_uses_markdownv2(self) -> None:
        a = _make_adapter()
        a._client.post = AsyncMock(return_value=_ok_response(message_id=42))
        result = await a.send_approval_request(
            chat_id="c", prompt_text="Allow x.y?", request_token="tk1",
        )
        assert result.success is True
        kwargs = a._client.post.call_args.kwargs
        payload = kwargs["json"]
        assert payload["parse_mode"] == "MarkdownV2"
        assert "Allow" in payload["text"]
        assert r"x\.y" in payload["text"]

    @pytest.mark.asyncio
    async def test_approval_prompt_falls_back(self) -> None:
        a = _make_adapter()
        a._client.post = AsyncMock(
            side_effect=[_parse_error_response(), _ok_response(message_id=42)],
        )
        result = await a.send_approval_request(
            chat_id="c", prompt_text="Allow weird.thing?", request_token="tk2",
        )
        assert result.success is True
        assert a._client.post.await_count == 2
        second = a._client.post.await_args_list[1].kwargs["json"]
        assert "parse_mode" not in second
        assert second["text"] == "Allow weird.thing?"
        # Token still tracked.
        assert "tk2" in a._approval_tokens


# ─── PR 3a.3 — _send_with_retry wiring ────────────────────────────


class TestSendWithRetry:
    @pytest.mark.asyncio
    async def test_send_retries_transient_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConnectError on attempts 1+2, success on 3 — total 3 calls."""
        import httpx as _httpx

        # Avoid real backoff sleeps in tests.
        async def _no_sleep(_d: float) -> None:
            return None

        monkeypatch.setattr("plugin_sdk.channel_contract.asyncio.sleep", _no_sleep)

        a = _make_adapter()
        a._client.post = AsyncMock(
            side_effect=[
                _httpx.ConnectError("boom 1"),
                _httpx.ConnectError("boom 2"),
                _ok_response(),
            ],
        )
        result = await a.send("c", "hi")
        assert result.success is True
        assert a._client.post.await_count == 3

    @pytest.mark.asyncio
    async def test_send_retry_exhaustion_returns_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All 3 attempts ConnectError — returns SendResult(success=False)."""
        import httpx as _httpx

        async def _no_sleep(_d: float) -> None:
            return None

        monkeypatch.setattr("plugin_sdk.channel_contract.asyncio.sleep", _no_sleep)

        a = _make_adapter()
        a._client.post = AsyncMock(
            side_effect=[
                _httpx.ConnectError("boom 1"),
                _httpx.ConnectError("boom 2"),
                _httpx.ConnectError("boom 3"),
            ],
        )
        result = await a.send("c", "hi")
        assert result.success is False
        assert "ConnectError" in (result.error or "")
        assert a._client.post.await_count == 3

    @pytest.mark.asyncio
    async def test_send_non_retryable_propagates(self) -> None:
        """A 400-class HTTP response is NOT a transient error — no retries."""
        a = _make_adapter()
        bad = MagicMock()
        bad.status_code = 400
        bad.text = "Bad Request: chat not found"
        bad.json.return_value = {"ok": False}
        a._client.post = AsyncMock(return_value=bad)

        result = await a.send("c", "hi")
        assert result.success is False
        # Single call, no retry on 400.
        assert a._client.post.await_count == 1

    @pytest.mark.asyncio
    async def test_send_reaction_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reaction send path also benefits from the retry wrapper."""
        import httpx as _httpx

        async def _no_sleep(_d: float) -> None:
            return None

        monkeypatch.setattr("plugin_sdk.channel_contract.asyncio.sleep", _no_sleep)

        a = _make_adapter()
        a._client.post = AsyncMock(
            side_effect=[
                _httpx.ConnectError("flap"),
                _ok_response(),
            ],
        )
        result = await a.send_reaction("c", "1", "👀")
        assert result.success is True
        assert a._client.post.await_count == 2

    @pytest.mark.asyncio
    async def test_edit_message_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx as _httpx

        async def _no_sleep(_d: float) -> None:
            return None

        monkeypatch.setattr("plugin_sdk.channel_contract.asyncio.sleep", _no_sleep)

        a = _make_adapter()
        a._client.post = AsyncMock(
            side_effect=[
                _httpx.ConnectError("flap"),
                _ok_response(),
            ],
        )
        result = await a.edit_message("c", "1", "updated")
        assert result.success is True
        assert a._client.post.await_count == 2
