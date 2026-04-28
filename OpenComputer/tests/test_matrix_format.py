"""Tests for Matrix HTML formatting in send/edit (PR 3b.3).

When the outbound text contains markdown, the adapter emits both
``body`` (plain) and ``formatted_body`` + ``format=org.matrix.custom.html``
so HTML-capable clients render rich text. Plain-text passthrough is
preserved (no formatted_body when text has no markdown markers).

URL allowlist rules (javascript: rejected, data: rejected) are
inherited from plugin_sdk.format_converters.matrix_html._sanitize_url.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "matrix_adapter_pr3b3_format",
        Path(__file__).resolve().parent.parent / "extensions" / "matrix" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MatrixAdapter, mod


@pytest.fixture
def adapter_with_mock():
    MatrixAdapter, _ = _load()
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if "/send/" in req.url.path:
            return httpx.Response(200, json={"event_id": "$ev:test.local"})
        if "/redact/" in req.url.path:
            return httpx.Response(200, json={"event_id": "$rd:test.local"})
        return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})

    a = MatrixAdapter(
        config={
            "homeserver": "https://matrix.test.local",
            "access_token": "syt_xxx",
        }
    )
    a._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer syt_xxx",
            "Content-Type": "application/json",
        },
    )
    a._user_id = "@bot:test.local"
    return a, requests


def _send_body(requests: list[httpx.Request]) -> dict:
    return json.loads(
        next(
            r for r in requests if "/send/m.room.message/" in r.url.path
        ).read()
    )


# ---------------------------------------------------------------------------
# Markdown → HTML on the wire
# ---------------------------------------------------------------------------


class TestMarkdownToHtml:
    @pytest.mark.asyncio
    async def test_bold_becomes_strong(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("!r:t.l", "this is **bold** text")
        body = _send_body(requests)
        assert body["body"] == "this is **bold** text"  # plain preserved
        assert body["format"] == "org.matrix.custom.html"
        assert "<strong>bold</strong>" in body["formatted_body"]

    @pytest.mark.asyncio
    async def test_italic_becomes_em(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("!r:t.l", "see [docs](https://example.com)")
        body = _send_body(requests)
        # Link present, sanitized URL preserved
        assert '<a href="https://example.com">docs</a>' in body["formatted_body"]

    @pytest.mark.asyncio
    async def test_heading_becomes_h(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("!r:t.l", "# Title")
        body = _send_body(requests)
        assert "<h1>Title</h1>" in body["formatted_body"]

    @pytest.mark.asyncio
    async def test_strikethrough(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("!r:t.l", "~~old~~ new")
        body = _send_body(requests)
        assert "<del>old</del>" in body["formatted_body"]

    @pytest.mark.asyncio
    async def test_code_fence_to_pre_code(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("!r:t.l", "```\nhello\n```")
        body = _send_body(requests)
        assert "<pre><code>" in body["formatted_body"]
        assert "hello" in body["formatted_body"]


# ---------------------------------------------------------------------------
# URL allowlist (javascript: / data: rejected)
# ---------------------------------------------------------------------------


class TestUrlAllowlist:
    @pytest.mark.asyncio
    async def test_javascript_scheme_rejected(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("!r:t.l", "click [me](javascript:alert(1))")
        body = _send_body(requests)
        # Anchor dropped, label survives
        assert "<a href=" not in body["formatted_body"]
        assert "javascript:" not in body["formatted_body"]
        assert "me" in body["formatted_body"]

    @pytest.mark.asyncio
    async def test_data_scheme_rejected(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send(
            "!r:t.l", "click [x](data:text/html,<script>1</script>)"
        )
        body = _send_body(requests)
        assert "<a href=" not in body["formatted_body"]
        assert "data:" not in body["formatted_body"]

    @pytest.mark.asyncio
    async def test_http_scheme_allowed(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("!r:t.l", "[label](http://example.com)")
        body = _send_body(requests)
        assert '<a href="http://example.com">label</a>' in body["formatted_body"]

    @pytest.mark.asyncio
    async def test_mailto_scheme_allowed(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("!r:t.l", "[contact](mailto:hi@example.com)")
        body = _send_body(requests)
        assert (
            '<a href="mailto:hi@example.com">contact</a>'
            in body["formatted_body"]
        )


# ---------------------------------------------------------------------------
# Plain-text passthrough — no formatted_body when no markdown
# ---------------------------------------------------------------------------


class TestPlainPassthrough:
    @pytest.mark.asyncio
    async def test_plain_text_omits_formatted_body(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("!r:t.l", "just a plain sentence")
        body = _send_body(requests)
        assert "formatted_body" not in body
        assert "format" not in body
        assert body["body"] == "just a plain sentence"


# ---------------------------------------------------------------------------
# Edits also format
# ---------------------------------------------------------------------------


class TestEditFormatting:
    @pytest.mark.asyncio
    async def test_edit_emits_html_in_new_content(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.edit_message("!r:t.l", "$orig:t.l", "**updated** text")
        body = _send_body(requests)
        # Fallback body uses "* " prefix
        assert body["body"].startswith("* ")
        # Both fallback formatted_body and m.new_content carry HTML
        assert body["format"] == "org.matrix.custom.html"
        assert body["formatted_body"].startswith("* ")
        assert "<strong>updated</strong>" in body["formatted_body"]
        assert body["m.new_content"]["format"] == "org.matrix.custom.html"
        assert (
            "<strong>updated</strong>"
            in body["m.new_content"]["formatted_body"]
        )

    @pytest.mark.asyncio
    async def test_edit_plain_text_no_formatted(self, adapter_with_mock) -> None:
        adapter, requests = adapter_with_mock
        await adapter.edit_message("!r:t.l", "$orig:t.l", "just plain")
        body = _send_body(requests)
        assert "format" not in body
        assert "formatted_body" not in body
        assert "format" not in body["m.new_content"]


# ---------------------------------------------------------------------------
# Retry wrap on transient ConnectError
# ---------------------------------------------------------------------------


class TestRetry:
    @pytest.mark.asyncio
    async def test_transient_connecterror_retried(self) -> None:
        MatrixAdapter, _ = _load()
        attempts = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("net flap")
            return httpx.Response(200, json={"event_id": "$ev:t.l"})

        a = MatrixAdapter(
            config={
                "homeserver": "https://matrix.test.local",
                "access_token": "x",
            }
        )
        a._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={
                "Authorization": "Bearer x",
                "Content-Type": "application/json",
            },
        )
        a._user_id = "@bot:t.l"
        result = await a.send("!r:t.l", "hi")
        assert result.success
        assert attempts["n"] == 2
