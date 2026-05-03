"""Bearer-token + password auth, with timing-safe compare.

Two acceptable headers:

  - ``Authorization: Bearer <token>`` (token mode) — case-insensitive
    scheme; token bytes preserved verbatim.
  - ``X-OpenComputer-Password: <pw>`` OR ``Authorization: Basic
    base64(user:pw)`` (password mode) — username segment ignored.

If both modes are configured, **either** matching is sufficient.

Token format: ``secrets.token_hex(24)`` → 48 hex chars (192 bits of
entropy). Matches OpenClaw's ``crypto.randomBytes(24).toString("hex")``
exactly so a hand-typed token from another install would still validate.

``ensure_browser_control_auth(env=…)`` either resolves existing creds
from environment (``OPENCOMPUTER_BROWSER_AUTH_TOKEN`` /
``OPENCOMPUTER_BROWSER_AUTH_PASSWORD``) or auto-generates a new token
unless we're in test mode (``OPENCOMPUTER_ENV=test`` or
``PYTEST_CURRENT_TEST`` set, in which case it returns empty so the
operator must explicitly pass creds).
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(slots=True)
class BrowserAuth:
    """Resolved auth state. Either or both fields may be set."""

    token: str | None = None
    password: str | None = None

    def is_anonymous_allowed(self) -> bool:
        """No creds configured → no auth gate (anonymous loopback access)."""
        return not self.token and not self.password


# ─── token generation ────────────────────────────────────────────────


def generate_browser_control_token() -> str:
    """48 hex chars (24 random bytes), matching OpenClaw's format."""
    return secrets.token_hex(24)


# ─── env resolution ──────────────────────────────────────────────────


def should_auto_generate_browser_auth(env: Mapping[str, str] | None = None) -> bool:
    """Refuse auto-gen in test environments."""
    e = env if env is not None else os.environ
    if e.get("OPENCOMPUTER_ENV", "").lower() == "test":
        return False
    return not e.get("PYTEST_CURRENT_TEST")


def resolve_browser_control_auth(
    env: Mapping[str, str] | None = None,
) -> BrowserAuth:
    """Read auth from env, returning empty if neither var is set."""
    e = env if env is not None else os.environ
    token = (e.get("OPENCOMPUTER_BROWSER_AUTH_TOKEN") or "").strip() or None
    password = (e.get("OPENCOMPUTER_BROWSER_AUTH_PASSWORD") or "").strip() or None
    return BrowserAuth(token=token, password=password)


async def ensure_browser_control_auth(
    *,
    env: Mapping[str, str] | None = None,
    auto_generate: bool | None = None,
) -> BrowserAuth:
    """Bootstrap auth.

    1. If env var creds are set → return them.
    2. If we're in test mode and auto_generate isn't explicitly True → empty.
    3. Otherwise auto-generate a token in-memory (no disk persistence in
       v0.1; the operator should set OPENCOMPUTER_BROWSER_AUTH_TOKEN if
       they need stable across restarts).
    """
    existing = resolve_browser_control_auth(env)
    if not existing.is_anonymous_allowed():
        return existing

    do_gen = auto_generate
    if do_gen is None:
        do_gen = should_auto_generate_browser_auth(env)

    if not do_gen:
        return existing

    return BrowserAuth(token=generate_browser_control_token())


# ─── header parsing ──────────────────────────────────────────────────


def parse_bearer_token(header_value: str | None) -> str | None:
    if not header_value:
        return None
    s = header_value.strip()
    if len(s) < 7:
        return None
    if s[:7].lower() != "bearer ":
        return None
    token = s[7:].strip()
    return token or None


def parse_basic_password(header_value: str | None) -> str | None:
    if not header_value:
        return None
    s = header_value.strip()
    if len(s) < 6 or s[:6].lower() != "basic ":
        return None
    payload = s[6:].strip()
    try:
        decoded = base64.b64decode(payload, validate=True).decode("utf-8")
    except Exception:
        return None
    sep = decoded.find(":")
    if sep < 0:
        return None
    pw = decoded[sep + 1 :].strip()
    return pw or None


# ─── per-request validation ──────────────────────────────────────────


def is_authorized(headers: Mapping[str, str], auth: BrowserAuth) -> bool:
    """Check ``headers`` against the configured ``auth``.

    Headers are looked up case-insensitively (caller normalizes).
    Empty/missing headers → False unless ``auth.is_anonymous_allowed()``.
    """
    if auth.is_anonymous_allowed():
        return True

    auth_header = _header(headers, "authorization")

    if auth.token:
        bearer = parse_bearer_token(auth_header)
        if bearer and hmac.compare_digest(bearer.encode(), auth.token.encode()):
            return True

    if auth.password:
        x_pw = (_header(headers, "x-opencomputer-password") or "").strip() or None
        if x_pw and hmac.compare_digest(x_pw.encode(), auth.password.encode()):
            return True
        basic_pw = parse_basic_password(auth_header)
        if basic_pw and hmac.compare_digest(basic_pw.encode(), auth.password.encode()):
            return True

    return False


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup."""
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None
