"""Gateway dispatch — user-facing error messages.

When ``run_conversation`` raises (LLM upstream 504, rate-limit, auth
failure, network error), the gateway catches the exception and returns
a string the channel adapter sends back to the user. The default
``f"[error: {type(e).__name__}: {e}]"`` shape leaks raw class names and
SDK internals at the user — this suite locks in friendlier text
keyed off the exception's ``status_code`` (Anthropic / OpenAI / httpx
all expose it) or class name (network-layer errors that never got an
HTTP response).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _make_status_err(name: str, code: int) -> Exception:
    """Build an exception that mimics the SDK status-error shape.

    Anthropic's ``APIStatusError`` subclasses carry a ``status_code``
    attribute; the dispatch helper duck-types on that, so this stand-in
    works without importing the real (heavy) anthropic exception tree.
    """
    cls = type(name, (Exception,), {})
    inst = cls(f"upstream returned HTTP {code}")
    inst.status_code = code  # type: ignore[attr-defined]
    return inst


def _make_named_exc(name: str) -> Exception:
    """Build an exception whose class name matches a network-layer error."""
    cls = type(name, (Exception,), {})
    return cls("network test")


def _dispatch_with_loop_raising(exc: Exception):
    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.core import MessageEvent, Platform

    mock_loop = MagicMock()
    mock_loop.run_conversation = AsyncMock(side_effect=exc)
    d = Dispatch(mock_loop)
    e = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="1",
        user_id="u",
        text="hello",
        timestamp=0.0,
    )
    return asyncio.run(d.handle_message(e))


def test_504_returns_friendly_transient_message() -> None:
    """Real-world bug repro: claude-router returned 504 → user saw garbage."""
    out = _dispatch_with_loop_raising(_make_status_err("InternalServerError", 504))
    assert out is not None
    # No raw class-name leak.
    assert "InternalServerError" not in out
    assert "[error:" not in out
    # Mentions the status + a retry hint.
    assert "504" in out
    assert "try again" in out.lower() or "transient" in out.lower()


def test_502_503_also_treated_as_transient() -> None:
    """All 5xx upstream errors share the friendly transient template."""
    for code in (500, 502, 503):
        out = _dispatch_with_loop_raising(
            _make_status_err("APIStatusError", code)
        )
        assert out is not None
        assert str(code) in out
        assert "try again" in out.lower() or "transient" in out.lower()


def test_429_returns_rate_limit_message() -> None:
    out = _dispatch_with_loop_raising(_make_status_err("RateLimitError", 429))
    assert out is not None
    assert "rate" in out.lower() and "limit" in out.lower()
    assert "RateLimitError" not in out


def test_401_returns_auth_message() -> None:
    out = _dispatch_with_loop_raising(_make_status_err("AuthenticationError", 401))
    assert out is not None
    assert "auth" in out.lower() or "api key" in out.lower()
    assert "AuthenticationError" not in out


def test_403_treated_as_auth_error() -> None:
    out = _dispatch_with_loop_raising(_make_status_err("PermissionDeniedError", 403))
    assert out is not None
    assert "auth" in out.lower() or "api key" in out.lower()


def test_network_error_returns_network_message() -> None:
    """No HTTP response yet — connection refused, DNS, timeout."""
    out = _dispatch_with_loop_raising(_make_named_exc("APIConnectionError"))
    assert out is not None
    assert (
        "network" in out.lower()
        or "reach" in out.lower()
        or "connect" in out.lower()
    )


def test_connect_timeout_treated_as_network() -> None:
    out = _dispatch_with_loop_raising(_make_named_exc("ConnectTimeout"))
    assert out is not None
    assert (
        "network" in out.lower()
        or "reach" in out.lower()
        or "connect" in out.lower()
    )


def test_unknown_exception_falls_back_to_generic_message() -> None:
    """Anything we don't have a friendly mapping for — still no raw repr leak."""
    out = _dispatch_with_loop_raising(ValueError("internal: kwargs collision"))
    assert out is not None
    assert "kwargs collision" not in out, "raw exception detail must not leak"
    # Class name is OK in the fallback (helps diagnose), but raw repr is not.
    assert "ValueError" in out
    assert "logs" in out.lower() or "details" in out.lower()


def test_format_helper_is_pure_function() -> None:
    """Importable + callable without a Dispatch instance — used by tests + tooling."""
    from opencomputer.gateway.dispatch import _format_user_facing_error

    assert callable(_format_user_facing_error)
    out = _format_user_facing_error(_make_status_err("InternalServerError", 504))
    assert "504" in out
