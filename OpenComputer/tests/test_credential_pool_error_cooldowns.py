"""T7 — Error-code-specific cooldowns + OAuth refresh path.

Hermes-doc parity:
- 429 → 1h cooldown (existing).
- 402 → 24h cooldown + immediate rotate (NEW).
- 401 → try OAuth refresh first; quarantine only if refresh fails (NEW).

Backwards compat: callers that don't pass ``oauth_refresher`` or
``classify_failure`` get the existing behavior unchanged.
"""

from __future__ import annotations

import pytest
from httpx import HTTPStatusError, Request, Response

from opencomputer.agent.credential_pool import (
    EXHAUSTED_TTL_402_SECONDS,
    EXHAUSTED_TTL_429_SECONDS,
    ROTATE_COOLDOWN_SECONDS,
    CredentialPool,
)


def _http_status_error(code: int) -> HTTPStatusError:
    req = Request("POST", "https://example.com")
    return HTTPStatusError(f"http {code}", request=req, response=Response(code, request=req))


@pytest.mark.asyncio
async def test_402_constant_is_24h():
    assert EXHAUSTED_TTL_402_SECONDS == 86400.0


@pytest.mark.asyncio
async def test_report_auth_failure_default_uses_rotate_cooldown():
    pool = CredentialPool(keys=["a", "b"])
    await pool.report_auth_failure("a", reason="401")
    stats = pool.stats()
    a = stats["keys"][0]
    assert ROTATE_COOLDOWN_SECONDS - 5 <= a["quarantine_remaining_s"] <= ROTATE_COOLDOWN_SECONDS + 5


@pytest.mark.asyncio
async def test_report_auth_failure_with_402_ttl():
    pool = CredentialPool(keys=["a", "b"])
    await pool.report_auth_failure(
        "a", reason="402", ttl_seconds=EXHAUSTED_TTL_402_SECONDS
    )
    stats = pool.stats()
    a = stats["keys"][0]
    assert a["quarantine_remaining_s"] >= 86000


@pytest.mark.asyncio
async def test_report_auth_failure_with_429_ttl():
    pool = CredentialPool(keys=["a", "b"])
    await pool.report_auth_failure(
        "a", reason="429", ttl_seconds=EXHAUSTED_TTL_429_SECONDS
    )
    stats = pool.stats()
    a = stats["keys"][0]
    assert 3500 <= a["quarantine_remaining_s"] <= 3700


@pytest.mark.asyncio
async def test_with_retry_classify_failure_402_quarantines_24h():
    pool = CredentialPool(keys=["a", "b"])

    attempts = {"n": 0}

    async def op(key: str):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_status_error(402)
        return "ok"

    def is_auth_failure(exc):
        return (
            isinstance(exc, HTTPStatusError) and exc.response.status_code in (401, 402, 429)
        )

    def classify(exc) -> float | None:
        if isinstance(exc, HTTPStatusError):
            code = exc.response.status_code
            if code == 402:
                return EXHAUSTED_TTL_402_SECONDS
            if code == 429:
                return EXHAUSTED_TTL_429_SECONDS
        return None

    result = await pool.with_retry(
        op, is_auth_failure=is_auth_failure, classify_failure=classify
    )
    assert result == "ok"
    assert attempts["n"] == 2
    stats = pool.stats()
    quarantined = [k for k in stats["keys"] if k["quarantine_remaining_s"] > 0]
    assert any(k["quarantine_remaining_s"] >= 86000 for k in quarantined)


@pytest.mark.asyncio
async def test_oauth_refresh_succeeds_no_quarantine():
    """A configured oauth_refresher rescues a 401 without quarantining."""

    def refresh(_old: str) -> str:
        return "fresh_token"

    pool = CredentialPool(keys=["expired_token"], oauth_refresher=refresh)

    attempts = {"n": 0}

    async def op(key: str):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_status_error(401)
        return f"ok with {key}"

    def is_auth_failure(exc):
        return isinstance(exc, HTTPStatusError) and exc.response.status_code == 401

    result = await pool.with_retry(op, is_auth_failure=is_auth_failure)
    # Refresh swapped the key; second attempt used the refreshed value.
    assert "fresh_token" in result
    stats = pool.stats()
    # Refreshed entry is NOT quarantined.
    assert stats["keys"][0]["quarantined"] is False


@pytest.mark.asyncio
async def test_oauth_refresh_failure_quarantines():
    """If oauth_refresher returns the same key, treat as failure → quarantine."""

    def no_op_refresh(old: str) -> str:
        return old  # refresh returned same key — failure signal

    pool = CredentialPool(keys=["expired_token"], oauth_refresher=no_op_refresh)

    async def op(key: str):
        raise _http_status_error(401)

    def is_auth_failure(exc):
        return isinstance(exc, HTTPStatusError) and exc.response.status_code == 401

    with pytest.raises(Exception):
        await pool.with_retry(op, is_auth_failure=is_auth_failure)
    stats = pool.stats()
    assert stats["keys"][0]["quarantined"] is True


@pytest.mark.asyncio
async def test_async_oauth_refresh_supported():
    """oauth_refresher may be async."""

    async def refresh(_old: str) -> str:
        return "fresh_async_token"

    pool = CredentialPool(keys=["expired"], oauth_refresher=refresh)

    attempts = {"n": 0}

    async def op(key: str):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_status_error(401)
        return f"ok with {key}"

    def is_auth_failure(exc):
        return isinstance(exc, HTTPStatusError) and exc.response.status_code == 401

    result = await pool.with_retry(op, is_auth_failure=is_auth_failure)
    assert "fresh_async_token" in result


@pytest.mark.asyncio
async def test_backwards_compat_no_classify_no_refresher():
    """A caller passing neither new kwarg works as before."""
    pool = CredentialPool(keys=["a", "b"])

    async def op(key: str):
        return f"ok-{key}"

    result = await pool.with_retry(op, is_auth_failure=lambda e: False)
    assert result.startswith("ok-")
