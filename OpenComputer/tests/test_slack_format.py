"""Tests for Slack mrkdwn formatting in send/edit (PR 3b.2).

The Slack adapter applies ``plugin_sdk.format_converters.slack_mrkdwn``
to every outgoing payload. These tests verify the on-the-wire ``text``
field is the converted form, not the raw markdown.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "slack_adapter_pr3b2_format",
        Path(__file__).resolve().parent.parent / "extensions" / "slack" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SlackAdapter, mod


@pytest.fixture
def adapter_with_mock():
    SlackAdapter, _ = _load()
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.url.path.endswith("/chat.postMessage") or req.url.path.endswith(
            "/chat.update"
        ):
            return httpx.Response(200, json={"ok": True, "ts": "1.2"})
        return httpx.Response(404, json={"ok": False})

    a = SlackAdapter(config={"bot_token": "xoxb-test"})
    a._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer xoxb-test",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    return a, requests


def _last_text(requests: list[httpx.Request]) -> str:
    body = json.loads(requests[-1].read())
    return body["text"]


# ---------------------------------------------------------------------------
# format_message — standalone converter check
# ---------------------------------------------------------------------------


class TestFormatMessage:
    def test_bold_double_to_single(self) -> None:
        SlackAdapter, _ = _load()
        a = SlackAdapter(config={"bot_token": "x"})
        assert a.format_message("**hi**") == "*hi*"

    def test_link_to_pipe_form(self) -> None:
        SlackAdapter, _ = _load()
        a = SlackAdapter(config={"bot_token": "x"})
        assert a.format_message("[label](https://example.com)") == (
            "<https://example.com|label>"
        )

    def test_code_fence_preserved(self) -> None:
        SlackAdapter, _ = _load()
        a = SlackAdapter(config={"bot_token": "x"})
        out = a.format_message("```python\nprint(1)\n```")
        # Fence is stashed verbatim — should round-trip unchanged.
        assert "```" in out
        assert "print(1)" in out

    def test_inline_code_preserved(self) -> None:
        SlackAdapter, _ = _load()
        a = SlackAdapter(config={"bot_token": "x"})
        # The slack converter retains inline backticks.
        assert "`x`" in a.format_message("`x`")

    def test_empty_input_safe(self) -> None:
        SlackAdapter, _ = _load()
        a = SlackAdapter(config={"bot_token": "x"})
        assert a.format_message("") == ""
        assert a.format_message(None) == ""  # type: ignore[arg-type]

    def test_heading_to_bold(self) -> None:
        SlackAdapter, _ = _load()
        a = SlackAdapter(config={"bot_token": "x"})
        assert a.format_message("# Title") == "*Title*"


# ---------------------------------------------------------------------------
# Wire-format checks: send + edit
# ---------------------------------------------------------------------------


class TestSendUsesConverter:
    @pytest.mark.asyncio
    async def test_bold_converted_on_wire(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("C1", "Hello **world**")
        assert _last_text(requests) == "Hello *world*"

    @pytest.mark.asyncio
    async def test_link_converted_on_wire(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("C1", "see [docs](https://x.com)")
        assert _last_text(requests) == "see <https://x.com|docs>"

    @pytest.mark.asyncio
    async def test_code_fence_passes_through(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("C1", "```\ncode\n```")
        text = _last_text(requests)
        assert "```" in text
        assert "code" in text

    @pytest.mark.asyncio
    async def test_plain_text_unchanged(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("C1", "just plain")
        assert _last_text(requests) == "just plain"


class TestEditUsesConverter:
    @pytest.mark.asyncio
    async def test_edit_applies_format(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.edit_message("C1", "1.2", "**bold** edit")
        assert _last_text(requests) == "*bold* edit"


# ---------------------------------------------------------------------------
# Retry wrapper — transient network blip retried, fatal not retried
# ---------------------------------------------------------------------------


class TestRetryWrap:
    @pytest.mark.asyncio
    async def test_transient_connecterror_retried(self) -> None:
        SlackAdapter, _ = _load()
        attempts = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("network down")
            return httpx.Response(200, json={"ok": True, "ts": "1.2"})

        a = SlackAdapter(config={"bot_token": "x"})
        a._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={
                "Authorization": "Bearer x",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        result = await a.send("C1", "hi")
        assert result.success
        assert attempts["n"] == 2  # first failed, second succeeded
