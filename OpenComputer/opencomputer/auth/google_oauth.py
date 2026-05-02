"""Google OAuth (PKCE) flow for the Gemini provider.

Implements Authorization Code + PKCE (S256) against accounts.google.com.
The resulting access_token authorizes Google's *Cloud Code Assist* backend
(``cloudcode-pa.googleapis.com/v1internal:*``) — the same backend that
powers the official ``gemini-cli``.

Storage: ``$OPENCOMPUTER_HOME/auth/google_oauth.json`` (chmod 0o600).

  {
    "access_token":  "...",
    "refresh_token": "...",
    "expires_ms":    1744848000000,   // unix MILLISECONDS, like gemini-cli
    "email":         "user@example.com",
    "project_id":    "..."
  }

Public client credentials
-------------------------
The ``client_id`` and ``client_secret`` defaults are Google's PUBLIC desktop
OAuth client baked into every copy of the open-source ``gemini-cli`` npm
package. Desktop OAuth uses PKCE for security — the client_secret has no
secret-keeping requirement. Reusing them mirrors what
``opencode-gemini-auth`` and Hermes do.

Override with ``OPENCOMPUTER_GEMINI_CLIENT_ID`` /
``OPENCOMPUTER_GEMINI_CLIENT_SECRET`` env vars if you have your own.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .external import (
    generate_pkce_pair,
    open_url,
    wait_for_redirect_callback,
)

# =============================================================================
# Endpoints & constants
# =============================================================================

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v1/userinfo"

# Gemini Cloud Code Assist marker — actual HTTP traffic uses
# https://cloudcode-pa.googleapis.com/v1internal:* via a dedicated adapter
# (see follow-up roadmap doc). The base_url we expose to the rest of OC is
# this scheme to signal "the OpenAI HTTP adapter MUST NOT be used here".
DEFAULT_GEMINI_CLOUDCODE_BASE_URL = "cloudcode-pa://google"
CLOUDCODE_INFERENCE_BASE_URL = "https://cloudcode-pa.googleapis.com/v1internal"

OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile"
)

DEFAULT_REDIRECT_PORT = 8085
REDIRECT_HOST = "localhost"
CALLBACK_PATH = "/oauth2callback"

REFRESH_SKEW_SECONDS = 60
TOKEN_REQUEST_TIMEOUT_SECONDS = 20.0
CALLBACK_WAIT_SECONDS = 300.0

# =============================================================================
# Public OAuth client (Google's gemini-cli desktop client)
# =============================================================================

ENV_CLIENT_ID = "OPENCOMPUTER_GEMINI_CLIENT_ID"
ENV_CLIENT_SECRET = "OPENCOMPUTER_GEMINI_CLIENT_SECRET"

# Composed piecewise to keep each fragment paired with its non-confidentiality
# rationale. Source: github.com/google-gemini/gemini-cli/blob/main/packages/core/src/code_assist/oauth2.ts
_PUBLIC_CLIENT_ID_PROJECT_NUM = "681255809395"
_PUBLIC_CLIENT_ID_HASH = "oo8ft2oprdrnp9e3aqf6av3hmdib135j"
_PUBLIC_CLIENT_SECRET_SUFFIX = "4uHgMPm-1o7Sk-geV6Cu5clXFsxl"

_DEFAULT_CLIENT_ID = (
    f"{_PUBLIC_CLIENT_ID_PROJECT_NUM}-{_PUBLIC_CLIENT_ID_HASH}"
    ".apps.googleusercontent.com"
)
_DEFAULT_CLIENT_SECRET = f"GOCSPX-{_PUBLIC_CLIENT_SECRET_SUFFIX}"


def resolve_client_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) — env override → shipped defaults."""
    cid = os.environ.get(ENV_CLIENT_ID) or _DEFAULT_CLIENT_ID
    cs = os.environ.get(ENV_CLIENT_SECRET) or _DEFAULT_CLIENT_SECRET
    return cid, cs


# =============================================================================
# Credential dataclass + persistence
# =============================================================================

@dataclass
class GoogleCredentials:
    access_token: str
    refresh_token: str
    expires_ms: int  # unix milliseconds
    email: str = ""
    project_id: str = ""

    def is_expiring(self, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
        """True if the access_token expires within ``skew_seconds``."""
        return time.time() + skew_seconds >= (self.expires_ms / 1000)


def _opencomputer_home() -> Path:
    """Resolve the active OpenComputer home (default ~/.opencomputer)."""
    override = os.environ.get("OPENCOMPUTER_HOME")
    if override:
        return Path(override)
    return Path.home() / ".opencomputer"


def _credentials_path() -> Path:
    return _opencomputer_home() / "auth" / "google_oauth.json"


def save_credentials(creds: GoogleCredentials) -> None:
    """Write credentials to disk with 0600 perms."""
    p = _credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(creds), indent=2), encoding="utf-8")
    p.chmod(0o600)


def load_credentials() -> GoogleCredentials | None:
    """Read credentials from disk; None if absent or unparseable."""
    p = _credentials_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "access_token" not in data:
        return None
    return GoogleCredentials(
        access_token=data.get("access_token", ""),
        refresh_token=data.get("refresh_token", ""),
        expires_ms=int(data.get("expires_ms", 0) or 0),
        email=data.get("email", "") or "",
        project_id=data.get("project_id", "") or "",
    )


def logout() -> None:
    """Delete the credentials file. Idempotent — no error if missing."""
    p = _credentials_path()
    try:
        p.unlink()
    except FileNotFoundError:
        pass


# =============================================================================
# Auth URL + token exchange + refresh
# =============================================================================

def build_auth_url(
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scopes: str = OAUTH_SCOPES,
) -> str:
    """Build the Google OAuth consent URL (browser navigates here)."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code_for_tokens(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> GoogleCredentials:
    """POST authorization_code + verifier → tokens. Persists creds + returns them."""
    cid, cs = resolve_client_credentials()
    if client_id:
        cid = client_id
    if client_secret:
        cs = client_secret

    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "code": code,
            "client_id": cid,
            "client_secret": cs,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        },
        timeout=TOKEN_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Google token exchange failed: {response.status_code} "
            f"{response.text[:200]}"
        )
    payload = response.json()
    access_token = payload["access_token"]
    refresh_token = payload.get("refresh_token", "")
    expires_in = int(payload.get("expires_in", 3600))
    expires_ms = int((time.time() + expires_in) * 1000)

    email = ""
    try:
        userinfo_response = httpx.get(
            USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TOKEN_REQUEST_TIMEOUT_SECONDS,
        )
        if userinfo_response.status_code == 200:
            email = (userinfo_response.json() or {}).get("email", "") or ""
    except httpx.HTTPError:
        pass  # Non-fatal: we can still use the token

    creds = GoogleCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_ms=expires_ms,
        email=email,
    )
    save_credentials(creds)
    return creds


def refresh_access_token() -> GoogleCredentials:
    """Use the stored refresh_token to mint a new access_token. Persists + returns."""
    creds = load_credentials()
    if not creds or not creds.refresh_token:
        raise RuntimeError(
            "Google OAuth: not logged in (no refresh_token). "
            "Run `opencomputer auth login google` first."
        )
    cid, cs = resolve_client_credentials()
    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "refresh_token": creds.refresh_token,
            "client_id": cid,
            "client_secret": cs,
            "grant_type": "refresh_token",
        },
        timeout=TOKEN_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Google OAuth refresh failed: {response.status_code} "
            f"{response.text[:200]}"
        )
    payload = response.json()
    new_access = payload["access_token"]
    new_refresh = payload.get("refresh_token", creds.refresh_token)
    expires_in = int(payload.get("expires_in", 3600))
    new_expires_ms = int((time.time() + expires_in) * 1000)

    new_creds = GoogleCredentials(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_ms=new_expires_ms,
        email=creds.email,
        project_id=creds.project_id,
    )
    save_credentials(new_creds)
    return new_creds


def get_valid_access_token(*, force_refresh: bool = False) -> str:
    """Return a non-expired access_token. Refreshes from disk creds if needed."""
    creds = load_credentials()
    if creds is None or not creds.access_token:
        raise RuntimeError(
            "Google OAuth: not logged in. Run `opencomputer auth login google` first."
        )
    if force_refresh or creds.is_expiring():
        creds = refresh_access_token()
    return creds.access_token


# =============================================================================
# Interactive login flow (browser-redirect via external.py)
# =============================================================================

def login_interactive(
    *,
    redirect_port: int = DEFAULT_REDIRECT_PORT,
    timeout_seconds: float = CALLBACK_WAIT_SECONDS,
    open_browser: bool = True,
) -> GoogleCredentials:
    """Run the full PKCE login flow: open browser → wait for callback → exchange.

    Returns the persisted GoogleCredentials. Raises ``RuntimeError`` for any
    OAuth failure (declined consent, redirect-uri mismatch, etc.).
    """
    cid, _ = resolve_client_credentials()
    redirect_uri = f"http://{REDIRECT_HOST}:{redirect_port}{CALLBACK_PATH}"
    pkce = generate_pkce_pair()
    import secrets

    state = secrets.token_urlsafe(16)
    auth_url = build_auth_url(
        client_id=cid,
        redirect_uri=redirect_uri,
        code_challenge=pkce.challenge,
        state=state,
    )

    if open_browser:
        open_url(auth_url)

    callback = wait_for_redirect_callback(
        redirect_uri, timeout_seconds=timeout_seconds
    )
    if callback.get("error"):
        raise RuntimeError(
            f"Google OAuth failed: {callback['error']} "
            f"({callback.get('error_description', '')})"
        )
    if callback.get("state") != state:
        raise RuntimeError("Google OAuth state mismatch — possible CSRF.")
    if not callback.get("code"):
        raise RuntimeError("Google OAuth: no authorization code received.")

    return exchange_code_for_tokens(
        code=callback["code"],
        code_verifier=pkce.verifier,
        redirect_uri=redirect_uri,
    )


__all__ = [
    "AUTH_ENDPOINT",
    "CLOUDCODE_INFERENCE_BASE_URL",
    "DEFAULT_GEMINI_CLOUDCODE_BASE_URL",
    "GoogleCredentials",
    "TOKEN_ENDPOINT",
    "build_auth_url",
    "exchange_code_for_tokens",
    "get_valid_access_token",
    "load_credentials",
    "login_interactive",
    "logout",
    "refresh_access_token",
    "resolve_client_credentials",
    "save_credentials",
]
