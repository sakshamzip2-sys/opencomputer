"""Typed error taxonomy for provider/network failures.

Hermes parity port (B3) — see
``OpenComputer/docs/refs/hermes-agent/2026-05-06-deep-comparison.md``.

Today, OpenComputer scatters error inspection through ``gateway/dispatch.py``
(string-compares ``RateLimitError`` / ``AuthenticationError``),
``credential_pool.py`` (auth-failure callbacks), and individual provider
plugins (each their own try/except). This module is the single classifier:
all callers convert raw exceptions to :class:`ErrorCategory` and decide
retry vs fatal from one place.

Provider-agnostic by design — the dispatch logic looks at structural
attributes (``status_code`` / ``response.status_code`` / ``code``) and
exception class names. It does NOT import any SDK so we don't pay the
import cost on the hot path and don't bind to specific SDK versions.
"""

from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    """Coarse-grained taxonomy used for retry / rotation decisions.

    The values are short snake_case strings so they can be logged or
    persisted (e.g. into ``llm_calls.error_category``) without an
    intermediate translation layer.
    """

    RATE_LIMITED = "rate_limited"
    AUTH = "auth"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    NETWORK = "network"
    BAD_REQUEST = "bad_request"
    SERVER = "server"
    UNKNOWN = "unknown"


_RETRYABLE: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.TIMEOUT,
        ErrorCategory.NETWORK,
        ErrorCategory.SERVER,
    }
)


def is_retryable(category: ErrorCategory) -> bool:
    """Return True if a category is worth retrying with backoff.

    AUTH is *not* retryable from this layer's perspective: the right
    response is to rotate credentials (``CredentialPool.with_retry``)
    or fail. QUOTA and BAD_REQUEST are fatal — retrying makes them
    worse. UNKNOWN is conservatively *not* retryable to avoid burning
    quota on opaque errors.
    """
    return category in _RETRYABLE


# ---------------------------------------------------------------------------
# Class-name → category dispatch.
#
# Most provider SDKs expose error subclasses with stable names. We match by
# class name (case-sensitive) so we don't need to import the SDK. The match
# is checked across the exception's MRO so subclasses of these names also
# classify correctly.
# ---------------------------------------------------------------------------

_CLASS_NAME_MAP: dict[str, ErrorCategory] = {
    # 429
    "RateLimitError": ErrorCategory.RATE_LIMITED,
    "TooManyRequests": ErrorCategory.RATE_LIMITED,
    "TooManyRequestsError": ErrorCategory.RATE_LIMITED,
    "ThrottlingException": ErrorCategory.RATE_LIMITED,  # AWS Bedrock
    # 401 / 403
    "AuthenticationError": ErrorCategory.AUTH,
    "PermissionDeniedError": ErrorCategory.AUTH,
    "PermissionError": ErrorCategory.AUTH,
    "Unauthorized": ErrorCategory.AUTH,
    "UnauthorizedError": ErrorCategory.AUTH,
    "InvalidAPIKeyError": ErrorCategory.AUTH,
    # plan / billing
    "InsufficientQuotaError": ErrorCategory.QUOTA,
    "QuotaExceededException": ErrorCategory.QUOTA,
    "BillingError": ErrorCategory.QUOTA,
    # timeouts
    "TimeoutError": ErrorCategory.TIMEOUT,
    "ReadTimeout": ErrorCategory.TIMEOUT,
    "ConnectTimeout": ErrorCategory.TIMEOUT,
    "WriteTimeout": ErrorCategory.TIMEOUT,
    "PoolTimeout": ErrorCategory.TIMEOUT,
    "APITimeoutError": ErrorCategory.TIMEOUT,
    # network
    "ConnectionError": ErrorCategory.NETWORK,
    "ConnectError": ErrorCategory.NETWORK,
    "RemoteProtocolError": ErrorCategory.NETWORK,
    "NetworkError": ErrorCategory.NETWORK,
    "APIConnectionError": ErrorCategory.NETWORK,
    "ProtocolError": ErrorCategory.NETWORK,
    # caller bug
    "BadRequestError": ErrorCategory.BAD_REQUEST,
    "InvalidRequestError": ErrorCategory.BAD_REQUEST,
    "ValidationError": ErrorCategory.BAD_REQUEST,
    "UnprocessableEntityError": ErrorCategory.BAD_REQUEST,
    # server
    "InternalServerError": ErrorCategory.SERVER,
    "ServiceUnavailableError": ErrorCategory.SERVER,
    "BadGateway": ErrorCategory.SERVER,
    "GatewayTimeout": ErrorCategory.SERVER,
}


def _category_from_class_name(exc: BaseException) -> ErrorCategory | None:
    for cls in type(exc).__mro__:
        cat = _CLASS_NAME_MAP.get(cls.__name__)
        if cat is not None:
            return cat
    return None


def _safe_getattr(obj: object, name: str) -> object | None:
    """``getattr`` that swallows exceptions from misbehaving properties.

    Some SDK errors expose ``status_code`` as a property that raises
    when the response was never received. Catching here keeps
    ``classify`` total — the worst case is "fall through to UNKNOWN".
    """
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001
        return None


def _status_code(exc: BaseException) -> int | None:
    """Extract an HTTP status code from an exception, if present.

    Tries a handful of conventions:
    - ``exc.status_code``  (anthropic, openai, httpx.HTTPStatusError)
    - ``exc.response.status_code``  (httpx errors)
    - ``exc.code``  (some legacy SDKs)
    """
    for path in ("status_code", "code"):
        v = _safe_getattr(exc, path)
        if isinstance(v, int):
            return v
    response = _safe_getattr(exc, "response")
    if response is not None:
        v = _safe_getattr(response, "status_code")
        if isinstance(v, int):
            return v
    return None


def _category_from_status_code(code: int) -> ErrorCategory | None:
    if code == 429:
        return ErrorCategory.RATE_LIMITED
    if code in (401, 403):
        return ErrorCategory.AUTH
    if code == 402:
        return ErrorCategory.QUOTA
    if code in (408,):
        return ErrorCategory.TIMEOUT
    if code in (400, 422):
        return ErrorCategory.BAD_REQUEST
    if 500 <= code <= 599:
        return ErrorCategory.SERVER
    return None


def classify(exc: BaseException) -> ErrorCategory:
    """Return the :class:`ErrorCategory` for an exception.

    Lookup priority:
    1. HTTP status code (most authoritative — survives SDK refactors).
    2. Exception class name (stable across SDK versions).
    3. Default :data:`ErrorCategory.UNKNOWN`.

    Never raises — even on weird exception subclasses.
    """
    code = _status_code(exc)
    if code is not None:
        cat = _category_from_status_code(code)
        if cat is not None:
            return cat
    name_cat = _category_from_class_name(exc)
    if name_cat is not None:
        return name_cat
    return ErrorCategory.UNKNOWN


__all__ = ["ErrorCategory", "classify", "is_retryable"]
