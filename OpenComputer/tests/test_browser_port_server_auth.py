"""Unit tests for server auth + token bootstrap."""

from __future__ import annotations

import base64

import pytest
from extensions.browser_control.server.auth import (
    BrowserAuth,
    ensure_browser_control_auth,
    generate_browser_control_token,
    is_authorized,
    parse_basic_password,
    parse_bearer_token,
    resolve_browser_control_auth,
    should_auto_generate_browser_auth,
)

# ─── token gen ───────────────────────────────────────────────────────


def test_generate_browser_control_token_length() -> None:
    t = generate_browser_control_token()
    assert len(t) == 48
    # 48 hex chars = 24 random bytes = 192 bits.
    int(t, 16)


def test_two_tokens_differ() -> None:
    assert generate_browser_control_token() != generate_browser_control_token()


# ─── env-based resolution ────────────────────────────────────────────


def test_resolve_browser_control_auth_empty() -> None:
    assert resolve_browser_control_auth({}).is_anonymous_allowed()


def test_resolve_token_from_env() -> None:
    env = {"OPENCOMPUTER_BROWSER_AUTH_TOKEN": "abcdef"}
    a = resolve_browser_control_auth(env)
    assert a.token == "abcdef"
    assert a.password is None


def test_resolve_password_from_env() -> None:
    env = {"OPENCOMPUTER_BROWSER_AUTH_PASSWORD": "secret"}
    a = resolve_browser_control_auth(env)
    assert a.password == "secret"


def test_resolve_strips_whitespace() -> None:
    env = {"OPENCOMPUTER_BROWSER_AUTH_TOKEN": "  spaces  "}
    a = resolve_browser_control_auth(env)
    assert a.token == "spaces"


# ─── auto-gen gate ───────────────────────────────────────────────────


def test_no_auto_gen_in_test_env() -> None:
    assert not should_auto_generate_browser_auth({"OPENCOMPUTER_ENV": "test"})


def test_no_auto_gen_under_pytest() -> None:
    assert not should_auto_generate_browser_auth(
        {"PYTEST_CURRENT_TEST": "foo::bar"}
    )


def test_auto_gen_in_prod_env() -> None:
    # Empty env (no PYTEST_CURRENT_TEST, no OPENCOMPUTER_ENV) → auto-gen ok.
    assert should_auto_generate_browser_auth({})


# ─── ensure ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_returns_existing_creds() -> None:
    env = {"OPENCOMPUTER_BROWSER_AUTH_TOKEN": "presupplied"}
    a = await ensure_browser_control_auth(env=env)
    assert a.token == "presupplied"


@pytest.mark.asyncio
async def test_ensure_in_test_env_returns_empty() -> None:
    a = await ensure_browser_control_auth(env={"OPENCOMPUTER_ENV": "test"})
    assert a.is_anonymous_allowed()


@pytest.mark.asyncio
async def test_ensure_force_auto_gen() -> None:
    a = await ensure_browser_control_auth(env={}, auto_generate=True)
    assert a.token and len(a.token) == 48


# ─── header parsers ─────────────────────────────────────────────────


def test_parse_bearer_basic() -> None:
    assert parse_bearer_token("Bearer abc") == "abc"
    assert parse_bearer_token("bearer abc") == "abc"  # case-insensitive scheme
    assert parse_bearer_token(" Bearer  abc ") == "abc"


def test_parse_bearer_rejects_empty() -> None:
    assert parse_bearer_token("Bearer ") is None
    assert parse_bearer_token(None) is None
    assert parse_bearer_token("") is None
    assert parse_bearer_token("Token abc") is None


def test_parse_basic_password() -> None:
    payload = base64.b64encode(b"user:pw").decode()
    assert parse_basic_password(f"Basic {payload}") == "pw"


def test_parse_basic_no_user() -> None:
    payload = base64.b64encode(b":onlypw").decode()
    assert parse_basic_password(f"Basic {payload}") == "onlypw"


def test_parse_basic_invalid_base64_returns_none() -> None:
    assert parse_basic_password("Basic !!!notb64") is None


def test_parse_basic_no_colon_returns_none() -> None:
    payload = base64.b64encode(b"nopassword").decode()
    assert parse_basic_password(f"Basic {payload}") is None


# ─── is_authorized ──────────────────────────────────────────────────


def test_authorized_anonymous_allowed_when_empty_creds() -> None:
    assert is_authorized({}, BrowserAuth())


def test_authorized_bearer_match() -> None:
    auth = BrowserAuth(token="t1")
    assert is_authorized({"Authorization": "Bearer t1"}, auth)


def test_authorized_bearer_mismatch() -> None:
    auth = BrowserAuth(token="t1")
    assert not is_authorized({"Authorization": "Bearer t2"}, auth)


def test_authorized_x_password() -> None:
    auth = BrowserAuth(password="pw")
    assert is_authorized({"X-OpenComputer-Password": "pw"}, auth)


def test_authorized_basic_password() -> None:
    auth = BrowserAuth(password="pw")
    payload = base64.b64encode(b"user:pw").decode()
    assert is_authorized({"Authorization": f"Basic {payload}"}, auth)


def test_authorized_no_headers_with_creds_set_fails() -> None:
    auth = BrowserAuth(token="t1")
    assert not is_authorized({}, auth)


def test_authorized_case_insensitive_header_lookup() -> None:
    auth = BrowserAuth(token="t1")
    assert is_authorized({"AUTHORIZATION": "Bearer t1"}, auth)


def test_authorized_supports_both_token_and_password_either_passes() -> None:
    auth = BrowserAuth(token="t1", password="pw")
    assert is_authorized({"Authorization": "Bearer t1"}, auth)
    assert is_authorized({"X-OpenComputer-Password": "pw"}, auth)
