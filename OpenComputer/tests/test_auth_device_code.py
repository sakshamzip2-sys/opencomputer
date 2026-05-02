"""Tests for opencomputer/auth/device_code.py — generic OAuth device-code flow."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest


def test_device_code_response_dataclass():
    from opencomputer.auth.device_code import DeviceCodeResponse

    r = DeviceCodeResponse(
        device_code="dc-xyz",
        user_code="ABCD-EFGH",
        verification_uri="https://provider.example/activate",
        verification_uri_complete="https://provider.example/activate?code=ABCD-EFGH",
        expires_in=900,
        interval=5,
    )
    assert r.device_code == "dc-xyz"
    assert r.user_code == "ABCD-EFGH"


def test_request_device_code_returns_parsed_response():
    from opencomputer.auth.device_code import request_device_code

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "device_code": "dc-test",
        "user_code": "TEST-CODE",
        "verification_uri": "https://example.com/activate",
        "verification_uri_complete": "https://example.com/activate?code=TEST-CODE",
        "expires_in": 600,
        "interval": 5,
    }

    with patch("httpx.post", return_value=mock_response):
        result = request_device_code(
            device_code_url="https://example.com/oauth/device",
            client_id="test-client",
            scope="read write",
        )

    assert result.device_code == "dc-test"
    assert result.user_code == "TEST-CODE"
    assert result.interval == 5


def test_request_device_code_raises_on_http_error():
    from opencomputer.auth.device_code import (
        DeviceCodeError,
        request_device_code,
    )

    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = '{"error": "invalid_client"}'
    mock_response.json.return_value = {"error": "invalid_client"}

    with patch("httpx.post", return_value=mock_response):
        with pytest.raises(DeviceCodeError, match="invalid_client"):
            request_device_code(
                device_code_url="https://example.com/oauth/device",
                client_id="bad-client",
            )


def test_poll_for_token_success_on_first_try():
    from opencomputer.auth.device_code import poll_for_token

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "at-success",
        "refresh_token": "rt-xyz",
        "token_type": "Bearer",
        "expires_in": 3600,
    }

    with patch("httpx.post", return_value=mock_response):
        with patch("time.sleep"):  # don't actually sleep in tests
            result = poll_for_token(
                token_url="https://example.com/oauth/token",
                client_id="test-client",
                device_code="dc-test",
                interval=1,
                max_wait_seconds=30,
            )

    assert result["access_token"] == "at-success"
    assert result["refresh_token"] == "rt-xyz"


def test_poll_for_token_retries_on_authorization_pending():
    from opencomputer.auth.device_code import poll_for_token

    pending_resp = MagicMock()
    pending_resp.status_code = 400
    pending_resp.json.return_value = {"error": "authorization_pending"}

    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {
        "access_token": "at-eventually",
        "expires_in": 3600,
    }

    # First two responses pending, third success
    responses = [pending_resp, pending_resp, success_resp]

    with patch("httpx.post", side_effect=responses):
        with patch("time.sleep"):
            result = poll_for_token(
                token_url="https://example.com/oauth/token",
                client_id="test-client",
                device_code="dc-test",
                interval=1,
                max_wait_seconds=30,
            )

    assert result["access_token"] == "at-eventually"


def test_poll_for_token_raises_on_expired_token():
    from opencomputer.auth.device_code import (
        DeviceCodeError,
        poll_for_token,
    )

    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.json.return_value = {"error": "expired_token"}

    with patch("httpx.post", return_value=mock_response):
        with patch("time.sleep"):
            with pytest.raises(DeviceCodeError, match="expired"):
                poll_for_token(
                    token_url="https://example.com/oauth/token",
                    client_id="test-client",
                    device_code="dc-expired",
                    interval=1,
                    max_wait_seconds=30,
                )


def test_poll_for_token_honors_slow_down_response():
    """slow_down → bump interval by 5s per RFC 8628."""
    from opencomputer.auth.device_code import poll_for_token

    slow_resp = MagicMock()
    slow_resp.status_code = 400
    slow_resp.json.return_value = {"error": "slow_down"}

    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {
        "access_token": "at-slow",
        "expires_in": 3600,
    }

    sleep_calls: list[float] = []
    with patch("httpx.post", side_effect=[slow_resp, success_resp]):
        with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            poll_for_token(
                token_url="https://example.com/oauth/token",
                client_id="test-client",
                device_code="dc-slow",
                interval=2,
                max_wait_seconds=30,
            )

    # First sleep before retry should be initial interval (2s)
    # Second sleep (after slow_down) should be bumped to 2+5 = 7s
    assert sleep_calls[0] == 2  # initial interval
    assert sleep_calls[1] == 7  # bumped after slow_down


def test_poll_for_token_times_out_after_max_wait():
    """When polling exceeds max_wait, raise."""
    from opencomputer.auth.device_code import (
        DeviceCodeError,
        poll_for_token,
    )

    pending_resp = MagicMock()
    pending_resp.status_code = 400
    pending_resp.json.return_value = {"error": "authorization_pending"}

    elapsed = [0.0]

    def fake_sleep(s):
        elapsed[0] += s

    with patch("httpx.post", return_value=pending_resp):
        with patch("time.sleep", side_effect=fake_sleep):
            with patch("time.monotonic", side_effect=lambda: elapsed[0]):
                with pytest.raises(DeviceCodeError, match="timed out"):
                    poll_for_token(
                        token_url="https://example.com/oauth/token",
                        client_id="test-client",
                        device_code="dc-test",
                        interval=5,
                        max_wait_seconds=10,  # short timeout
                    )


def test_to_oauth_token_converts_response_with_expiry():
    from opencomputer.auth.device_code import to_oauth_token
    from opencomputer.auth.token_store import OAuthToken

    token_response = {
        "access_token": "at-x",
        "refresh_token": "rt-x",
        "expires_in": 3600,
        "scope": "read write",
        "token_type": "Bearer",
    }

    before = int(time.time())
    result = to_oauth_token("test-provider", token_response)
    after = int(time.time())

    assert isinstance(result, OAuthToken)
    assert result.provider == "test-provider"
    assert result.access_token == "at-x"
    assert result.refresh_token == "rt-x"
    # expires_at should be ~now + 3600
    assert before + 3600 <= result.expires_at <= after + 3600
    assert result.scope == "read write"
