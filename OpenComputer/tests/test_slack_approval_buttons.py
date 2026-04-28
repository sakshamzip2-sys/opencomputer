"""Slack ConsentGate inline-button approval — PR #221 follow-up Item 3.

Mirrors the Telegram approval surface (``test_telegram_approval_buttons.py``)
but uses Slack's Block Kit + interactivity webhook flow:

* :meth:`SlackAdapter.set_approval_callback` registers the click handler.
* :meth:`SlackAdapter.send_approval_request` posts a Block Kit message
  with three primary/danger buttons whose ``value`` carries the
  ``"oc:approve:<verb>:<token>"`` triple end-to-end.
* :meth:`SlackAdapter._handle_interactivity` is the aiohttp endpoint
  that Slack POSTs button clicks to. Verifies HMAC-SHA256 signature
  using the configured signing secret, parses the form-encoded
  ``payload=<json>`` body, dispatches to the registered callback, and
  responds with a Block Kit-friendly confirmation.

Slack signing-secret algorithm:
  https://api.slack.com/authentication/verifying-requests-from-slack
  basestring = ``v0:<timestamp>:<raw-body>``
  signature  = ``v0=<hex(hmac-sha256(secret, basestring))>``
  replay window = 5 minutes
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import SendResult


def _load():
    spec = importlib.util.spec_from_file_location(
        "slack_adapter_approval_test_g17",
        Path(__file__).resolve().parent.parent
        / "extensions" / "slack" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SlackAdapter, mod


def _make_adapter(
    signing_secret: str = "test-signing-secret",
    interactivity_port: int = 0,
):
    SlackAdapter, _ = _load()
    a = SlackAdapter(
        config={
            "bot_token": "xoxb-test",
            "signing_secret": signing_secret,
            "interactivity_port": interactivity_port,
        }
    )
    # Stub the outbound Web API client.
    a._client = AsyncMock()
    return a


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    """Produce a valid X-Slack-Signature for the given body."""
    basestring = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


# ---------------------------------------------------------------------------
# set_approval_callback — straightforward register/replace
# ---------------------------------------------------------------------------


def test_set_approval_callback_registers_handler() -> None:
    a = _make_adapter()
    assert a._approval_callback is None

    async def cb(verb: str, token: str) -> None:
        return None

    a.set_approval_callback(cb)
    assert a._approval_callback is cb

    async def cb2(verb: str, token: str) -> None:
        return None

    a.set_approval_callback(cb2)
    assert a._approval_callback is cb2


# ---------------------------------------------------------------------------
# send_approval_request — wire format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_approval_request_emits_block_kit_with_three_buttons() -> None:
    a = _make_adapter()
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"ok": True, "ts": "1234567890.123456"}
    a._client.post = AsyncMock(return_value=fake_resp)

    result = await a.send_approval_request(
        chat_id="C999",
        prompt_text="Allow read_files.metadata on /tmp/x?",
        request_token="abc123",
    )
    assert isinstance(result, SendResult)
    assert result.success is True

    # Two awaits expected: pause_typing + chat.postMessage. Find the
    # postMessage call.
    posts = a._client.post.call_args_list
    post_calls = [c for c in posts if c.args and "chat.postMessage" in c.args[0]]
    assert len(post_calls) == 1, f"expected 1 chat.postMessage, got {posts}"
    payload = post_calls[0].kwargs["json"]
    assert payload["channel"] == "C999"
    assert "blocks" in payload
    blocks = payload["blocks"]

    # First block is a markdown section with the prompt text.
    assert blocks[0]["type"] == "section"
    assert "Allow read_files.metadata" in blocks[0]["text"]["text"]

    # Second block is an actions row with three buttons.
    actions = blocks[1]
    assert actions["type"] == "actions"
    elements = actions["elements"]
    assert len(elements) == 3

    expected_values = [
        "oc:approve:once:abc123",
        "oc:approve:always:abc123",
        "oc:approve:deny:abc123",
    ]
    expected_action_ids = [
        "oc_approve_once_abc123",
        "oc_approve_always_abc123",
        "oc_approve_deny_abc123",
    ]
    expected_styles = ["primary", "primary", "danger"]
    for i, el in enumerate(elements):
        assert el["type"] == "button"
        assert el["value"] == expected_values[i]
        assert el["action_id"] == expected_action_ids[i]
        assert el["style"] == expected_styles[i]

    # Token registered for inbound resolution lookup.
    assert "abc123" in a._approval_tokens
    assert a._approval_tokens["abc123"]["chat_id"] == "C999"
    assert a._approval_tokens["abc123"]["ts"] == "1234567890.123456"


@pytest.mark.asyncio
async def test_send_approval_request_propagates_slack_error() -> None:
    a = _make_adapter()
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"ok": False, "error": "channel_not_found"}
    a._client.post = AsyncMock(return_value=fake_resp)

    result = await a.send_approval_request(
        chat_id="CXYZ",
        prompt_text="?",
        request_token="t",
    )
    assert result.success is False
    assert "channel_not_found" in (result.error or "")
    # Token NOT registered on failure — a stale click would otherwise
    # match against a future request reusing the same token.
    assert "t" not in a._approval_tokens


@pytest.mark.asyncio
async def test_send_approval_request_pauses_typing_status() -> None:
    """Approval prompt clears the ``Thinking…`` indicator while we wait."""
    a = _make_adapter()
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"ok": True, "ts": "x"}
    a._client.post = AsyncMock(return_value=fake_resp)
    await a.send_approval_request(
        chat_id="C1", prompt_text="?", request_token="t1",
    )
    # First call should be setStatus(""), then chat.postMessage.
    urls = [c.args[0] for c in a._client.post.call_args_list if c.args]
    assert any("assistant.threads.setStatus" in u for u in urls)
    # Verify the setStatus payload is the empty-string "clear" form.
    set_status_calls = [
        c for c in a._client.post.call_args_list
        if c.args and "assistant.threads.setStatus" in c.args[0]
    ]
    assert set_status_calls[0].kwargs["json"]["status"] == ""


# ---------------------------------------------------------------------------
# Signature verification — _verify_signature
# ---------------------------------------------------------------------------


def test_verify_signature_accepts_valid() -> None:
    a = _make_adapter(signing_secret="s3cret")
    body = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
    ts = str(int(time.time()))
    sig = _sign("s3cret", ts, body)
    assert a._verify_signature(timestamp=ts, body=body, signature=sig) is True


def test_verify_signature_rejects_wrong_secret() -> None:
    a = _make_adapter(signing_secret="s3cret")
    body = b"payload=%7B%7D"
    ts = str(int(time.time()))
    sig = _sign("WRONG", ts, body)
    assert a._verify_signature(timestamp=ts, body=body, signature=sig) is False


def test_verify_signature_rejects_replay_outside_window() -> None:
    a = _make_adapter(signing_secret="s3cret")
    body = b"payload=%7B%7D"
    # Six minutes in the past — outside the 5-minute window.
    ts = str(int(time.time()) - 6 * 60)
    sig = _sign("s3cret", ts, body)
    assert a._verify_signature(timestamp=ts, body=body, signature=sig) is False


def test_verify_signature_rejects_missing_secret() -> None:
    """No signing_secret configured → all signatures fail (fail-closed)."""
    a = _make_adapter(signing_secret="")
    body = b"payload=%7B%7D"
    ts = str(int(time.time()))
    sig = _sign("anything", ts, body)
    assert a._verify_signature(timestamp=ts, body=body, signature=sig) is False


# ---------------------------------------------------------------------------
# _handle_interactivity — full inbound flow
# ---------------------------------------------------------------------------


def _make_request(adapter, *, body: bytes, signature: str, timestamp: str):
    """Build a stub request object that ``_handle_interactivity`` can read."""
    from urllib.parse import parse_qs

    parsed = parse_qs(body.decode("utf-8", errors="replace"))
    form = {k: v[0] for k, v in parsed.items()}

    async def _read() -> bytes:
        return body

    async def _post() -> dict[str, str]:
        return form

    return SimpleNamespace(
        headers={
            "X-Slack-Signature": signature,
            "X-Slack-Request-Timestamp": timestamp,
        },
        read=_read,
        post=_post,
    )


def _build_action_payload(
    *, value: str, action_id: str
) -> bytes:
    """Form-encoded ``payload=<json>`` body Slack sends for button clicks."""
    from urllib.parse import quote

    payload = {
        "type": "block_actions",
        "actions": [
            {
                "type": "button",
                "value": value,
                "action_id": action_id,
            },
        ],
    }
    encoded = quote(json.dumps(payload))
    return f"payload={encoded}".encode()


@pytest.mark.asyncio
async def test_handle_interactivity_routes_valid_click_to_callback() -> None:
    a = _make_adapter(signing_secret="s3cret")
    a._approval_tokens["tok-abc"] = {"chat_id": "C9", "ts": "ts1"}

    received: list[tuple[str, str]] = []

    async def cb(verb: str, token: str) -> None:
        received.append((verb, token))

    a.set_approval_callback(cb)

    body = _build_action_payload(
        value="oc:approve:always:tok-abc",
        action_id="oc_approve_always_tok-abc",
    )
    ts = str(int(time.time()))
    sig = _sign("s3cret", ts, body)
    request = _make_request(a, body=body, signature=sig, timestamp=ts)

    response = await a._handle_interactivity(request)

    assert received == [("always", "tok-abc")]
    # Response is HTTP 200 with a Slack-friendly confirmation body.
    assert response.status == 200
    text = response.text or response.body
    if isinstance(text, bytes):
        text = text.decode()
    parsed = json.loads(text)
    assert "Decision recorded" in parsed["text"]
    assert parsed["replace_original"] is True
    # Token consumed.
    assert "tok-abc" not in a._approval_tokens


@pytest.mark.asyncio
async def test_handle_interactivity_rejects_invalid_signature() -> None:
    a = _make_adapter(signing_secret="s3cret")
    received: list = []
    a.set_approval_callback(lambda v, t: received.append((v, t)))  # type: ignore[arg-type]

    body = _build_action_payload(
        value="oc:approve:once:tok",
        action_id="oc_approve_once_tok",
    )
    ts = str(int(time.time()))
    sig = _sign("WRONG-SECRET", ts, body)  # bad signature
    request = _make_request(a, body=body, signature=sig, timestamp=ts)

    response = await a._handle_interactivity(request)
    assert response.status == 401
    assert received == []  # callback never fired


@pytest.mark.asyncio
async def test_handle_interactivity_dedupes_action_id() -> None:
    """Slack retries unacked deliveries — same action_id must dispatch once."""
    a = _make_adapter(signing_secret="s3cret")
    a._approval_tokens["tok-dup"] = {"chat_id": "C", "ts": "ts"}

    received: list[tuple[str, str]] = []

    async def cb(verb: str, token: str) -> None:
        received.append((verb, token))

    a.set_approval_callback(cb)

    body = _build_action_payload(
        value="oc:approve:once:tok-dup",
        action_id="oc_approve_once_tok-dup",
    )
    ts = str(int(time.time()))
    sig = _sign("s3cret", ts, body)
    req1 = _make_request(a, body=body, signature=sig, timestamp=ts)
    req2 = _make_request(a, body=body, signature=sig, timestamp=ts)

    await a._handle_interactivity(req1)
    await a._handle_interactivity(req2)
    assert received == [("once", "tok-dup")]


@pytest.mark.asyncio
async def test_handle_interactivity_unknown_token_quiet_ignore() -> None:
    """A click for a token we never sent is dropped quietly (no callback)."""
    a = _make_adapter(signing_secret="s3cret")
    received: list = []

    async def cb(verb: str, token: str) -> None:
        received.append((verb, token))

    a.set_approval_callback(cb)

    body = _build_action_payload(
        value="oc:approve:once:does-not-exist",
        action_id="oc_approve_once_orphan",
    )
    ts = str(int(time.time()))
    sig = _sign("s3cret", ts, body)
    request = _make_request(a, body=body, signature=sig, timestamp=ts)

    response = await a._handle_interactivity(request)
    assert response.status == 200
    assert received == []


@pytest.mark.asyncio
async def test_handle_interactivity_non_approval_value_ignored() -> None:
    """Buttons not minted by the consent flow don't reach the callback."""
    a = _make_adapter(signing_secret="s3cret")
    received: list = []

    async def cb(verb: str, token: str) -> None:
        received.append((verb, token))

    a.set_approval_callback(cb)

    body = _build_action_payload(
        value="some-other-plugin:menu:42",
        action_id="some_other",
    )
    ts = str(int(time.time()))
    sig = _sign("s3cret", ts, body)
    request = _make_request(a, body=body, signature=sig, timestamp=ts)
    response = await a._handle_interactivity(request)
    assert response.status == 200
    assert received == []


@pytest.mark.asyncio
async def test_handle_interactivity_resumes_typing_after_resolution() -> None:
    """After a successful click, the typing indicator is restored."""
    a = _make_adapter(signing_secret="s3cret")
    a._approval_tokens["tok-resume"] = {"chat_id": "C-resume", "ts": "ts"}

    async def cb(verb: str, token: str) -> None:
        return None

    a.set_approval_callback(cb)

    # Stub the outbound client so resume_typing_status's
    # assistant.threads.setStatus call is observable.
    set_status_calls: list[dict[str, Any]] = []
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"ok": True}

    async def _post(url: str, **kwargs: Any) -> Any:
        if "assistant.threads.setStatus" in url:
            set_status_calls.append(kwargs.get("json") or {})
        return fake_resp

    a._client.post = AsyncMock(side_effect=_post)

    body = _build_action_payload(
        value="oc:approve:once:tok-resume",
        action_id="oc_approve_once_tok-resume",
    )
    ts = str(int(time.time()))
    sig = _sign("s3cret", ts, body)
    request = _make_request(a, body=body, signature=sig, timestamp=ts)
    await a._handle_interactivity(request)

    # At least one setStatus call with the default "Thinking…" status.
    assert any(
        c.get("status") == "Thinking…" for c in set_status_calls
    ), f"expected resume_typing_status call, got {set_status_calls}"


# ---------------------------------------------------------------------------
# Defensive guard — connect() does not start the server without a secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_skips_interactivity_server_without_secret() -> None:
    """``interactivity_port > 0`` but no signing_secret → server NOT started."""
    SlackAdapter, _ = _load()
    a = SlackAdapter(
        config={
            "bot_token": "xoxb-test",
            "signing_secret": "",  # explicitly empty
            "interactivity_port": 8645,
        },
    )

    # Stub the auth.test response so connect succeeds otherwise.
    import httpx
    def _handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth.test"):
            return httpx.Response(200, json={"ok": True, "user": "u", "team": "t"})
        return httpx.Response(404, json={"ok": False})

    # Connect manually (bypass the inner client construction).
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    # _client is ALREADY set so connect's reassignment overwrites with a
    # fresh real client — patch httpx.AsyncClient to short-circuit.
    from unittest.mock import patch as _patch
    with _patch("httpx.AsyncClient", return_value=a._client):
        result = await a.connect()
    assert result is True
    # No interactivity runner — secret was missing.
    assert a._interactivity_runner is None
