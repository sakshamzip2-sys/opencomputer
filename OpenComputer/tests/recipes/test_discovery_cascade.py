"""cascade() probes a URL with PUBLIC → COOKIE → HEADER strategies."""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from opencomputer.recipes.discovery import run_cascade


def _mock_response(status: int, content_type: str = "application/json", body=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.headers = {"content-type": content_type}
    if content_type == "application/json":
        resp.json.return_value = body if body is not None else {}
    else:
        resp.text = body if body is not None else ""
    return resp


def test_public_succeeds_returns_public_strategy(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_BROWSER_CDP_URL", raising=False)

    fake = _mock_response(200, body={"ok": True})
    with patch("httpx.get", return_value=fake):
        result = run_cascade("https://example.com/api")

    assert result.strategy == "public"
    assert result.status_code == 200
    assert result.body == {"ok": True}
    assert result.attempted == ["public"]


def test_public_fails_falls_back_to_header(monkeypatch):
    """Public 401 → fall through to header. No CDP env so cookie strategy is skipped."""
    monkeypatch.delenv("OPENCOMPUTER_BROWSER_CDP_URL", raising=False)

    responses = [
        _mock_response(401, body={"err": "unauthorized"}),  # public
        _mock_response(200, body={"ok": True}),              # header
    ]
    call_iter = iter(responses)

    def fake_get(*args, **kwargs):
        return next(call_iter)

    with patch("httpx.get", side_effect=fake_get):
        result = run_cascade("https://example.com/api")

    assert result.strategy == "header"
    assert result.attempted == ["public", "header"]


def test_all_fail_returns_none_strategy(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_BROWSER_CDP_URL", raising=False)

    fake = _mock_response(500, body={"err": "boom"})
    with patch("httpx.get", return_value=fake):
        result = run_cascade("https://example.com/api")

    assert result.strategy is None
    assert result.status_code == 500


def test_cdp_url_set_attempts_cookie_strategy(monkeypatch):
    """When CDP env set, 'cookie' is in attempted list (even if currently skipped)."""
    monkeypatch.setenv("OPENCOMPUTER_BROWSER_CDP_URL", "http://localhost:9222")

    responses = [
        _mock_response(401),  # public fails
        _mock_response(200, body={"ok": True}),  # header succeeds
    ]
    call_iter = iter(responses)

    def fake_get(*args, **kwargs):
        return next(call_iter)

    with patch("httpx.get", side_effect=fake_get):
        result = run_cascade("https://example.com/api")

    assert "cookie" in result.attempted
    assert result.strategy == "header"


def test_network_error_falls_through_cleanly(monkeypatch):
    """httpx raising should fall through to header, not propagate."""
    monkeypatch.delenv("OPENCOMPUTER_BROWSER_CDP_URL", raising=False)

    raise_once = [True]

    def maybe_raise(*args, **kwargs):
        if raise_once[0]:
            raise_once[0] = False
            raise httpx.RequestError("boom")
        return _mock_response(200, body={"ok": True})

    with patch("httpx.get", side_effect=maybe_raise):
        result = run_cascade("https://example.com/api")

    assert result.strategy == "header"
