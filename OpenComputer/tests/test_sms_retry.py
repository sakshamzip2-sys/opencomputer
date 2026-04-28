"""PR #221 O2 — SMS adapter retry-on-transient-error coverage.

Verifies ``_send_with_retry`` is wired around the Twilio
``Messages.json`` POST. SMS uses aiohttp (not httpx), so the wrapping
needs an inner coroutine to pull the body out of the
async-context-manager response.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "_sms_adapter_test_o2",
        str(Path(__file__).parent.parent / "extensions" / "sms" / "adapter.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SmsAdapter


def _make_adapter():
    return _load()(
        config={
            "account_sid": "ACtest",
            "auth_token": "secret_token_test",
            "from_number": "+15551234567",
            "webhook_url": "https://example.com/webhooks/twilio",
        }
    )


@pytest.mark.asyncio
async def test_sms_send_retries_transient_connect_errors() -> None:
    a = _make_adapter()

    # Speed up retry backoff
    original = a._send_with_retry

    async def fast_retry(send_fn, *args, **kwargs):
        kwargs.setdefault("base_delay", 0.001)
        return await original(send_fn, *args, **kwargs)

    a._send_with_retry = fast_retry  # type: ignore[assignment]

    state = {"calls": 0}

    fake_resp = AsyncMock()
    fake_resp.__aenter__.return_value.status = 201
    fake_resp.__aenter__.return_value.json = AsyncMock(
        return_value={"sid": "SMfake999"}
    )

    def post_side_effect(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] <= 2:
            # Raise a retryable network error before we even produce a
            # context manager — _send_with_retry catches and retries.
            raise ConnectionError("simulated network blip")
        return fake_resp

    fake_session = MagicMock()
    fake_session.post = MagicMock(side_effect=post_side_effect)
    a._http_session = fake_session

    result = await a.send("+19998887777", "hello world")
    assert result.success is True
    assert result.message_id == "SMfake999"
    # 2 transient failures + 1 success = 3 calls
    assert state["calls"] == 3
