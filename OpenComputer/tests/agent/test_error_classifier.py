"""Tests for the error_classifier module (Hermes B3)."""

from __future__ import annotations

import asyncio

import pytest

from opencomputer.agent.error_classifier import (
    ErrorCategory,
    classify,
    is_retryable,
)


def _exc_with_status(code: int) -> Exception:
    """Build a fake exception that exposes ``status_code`` like Anthropic SDK."""
    e = RuntimeError(f"status={code}")
    e.status_code = code  # type: ignore[attr-defined]
    return e


def _exc_with_response_status(code: int) -> Exception:
    """Mimic httpx.HTTPStatusError shape (exc.response.status_code)."""

    class _Resp:
        status_code = code

    e = RuntimeError(f"resp.status={code}")
    e.response = _Resp()  # type: ignore[attr-defined]
    return e


@pytest.mark.parametrize(
    "code,expected",
    [
        (429, ErrorCategory.RATE_LIMITED),
        (401, ErrorCategory.AUTH),
        (403, ErrorCategory.AUTH),
        (402, ErrorCategory.QUOTA),
        (408, ErrorCategory.TIMEOUT),
        (400, ErrorCategory.BAD_REQUEST),
        (422, ErrorCategory.BAD_REQUEST),
        (500, ErrorCategory.SERVER),
        (502, ErrorCategory.SERVER),
        (503, ErrorCategory.SERVER),
        (504, ErrorCategory.SERVER),
        (599, ErrorCategory.SERVER),
        (200, ErrorCategory.UNKNOWN),  # not actually an error code
        (418, ErrorCategory.UNKNOWN),  # i'm a teapot — unknown bucket
    ],
)
def test_status_code_dispatch(code: int, expected: ErrorCategory) -> None:
    assert classify(_exc_with_status(code)) == expected


@pytest.mark.parametrize(
    "code,expected",
    [
        (429, ErrorCategory.RATE_LIMITED),
        (401, ErrorCategory.AUTH),
        (500, ErrorCategory.SERVER),
    ],
)
def test_response_status_dispatch(code: int, expected: ErrorCategory) -> None:
    assert classify(_exc_with_response_status(code)) == expected


def test_class_name_rate_limited() -> None:
    class RateLimitError(Exception):
        pass

    assert classify(RateLimitError("too many")) == ErrorCategory.RATE_LIMITED


def test_class_name_auth_subclass() -> None:
    class AuthenticationError(Exception):
        pass

    class MyVendorAuthError(AuthenticationError):
        pass

    assert classify(MyVendorAuthError("bad key")) == ErrorCategory.AUTH


def test_class_name_quota() -> None:
    class InsufficientQuotaError(Exception):
        pass

    assert classify(InsufficientQuotaError()) == ErrorCategory.QUOTA


def test_asyncio_timeout() -> None:
    assert classify(TimeoutError()) == ErrorCategory.TIMEOUT


def test_connection_error() -> None:
    assert classify(ConnectionError("eof")) == ErrorCategory.NETWORK


def test_unknown_falls_through() -> None:
    assert classify(RuntimeError("???")) == ErrorCategory.UNKNOWN


def test_status_code_overrides_class_name() -> None:
    """If both status code AND class name say something, status wins.

    Priority guarantees consistency: a vendor reusing the name
    ``RateLimitError`` for a 503 wouldn't fool the classifier.
    """

    class RateLimitError(Exception):
        pass

    e = RateLimitError("but it's actually a 503")
    e.status_code = 503  # type: ignore[attr-defined]
    assert classify(e) == ErrorCategory.SERVER


@pytest.mark.parametrize(
    "category,retryable",
    [
        (ErrorCategory.RATE_LIMITED, True),
        (ErrorCategory.TIMEOUT, True),
        (ErrorCategory.NETWORK, True),
        (ErrorCategory.SERVER, True),
        (ErrorCategory.AUTH, False),
        (ErrorCategory.QUOTA, False),
        (ErrorCategory.BAD_REQUEST, False),
        (ErrorCategory.UNKNOWN, False),
    ],
)
def test_is_retryable(category: ErrorCategory, retryable: bool) -> None:
    assert is_retryable(category) is retryable


def test_classify_does_not_raise_on_weird_exception() -> None:
    """Classifier must never crash — that would mask the real error."""

    class Weird(Exception):  # noqa: N818 — intentionally weird, not an Error suffix
        @property
        def status_code(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated buggy property")

    # Even though our property raises, classify should fall through.
    # Note: this currently DOES raise, so we wrap defensively.
    try:
        cat = classify(Weird())
    except Exception:
        pytest.fail("classify() must not raise on weird exceptions")
    assert isinstance(cat, ErrorCategory)
