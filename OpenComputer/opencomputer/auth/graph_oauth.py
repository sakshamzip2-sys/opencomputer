"""Microsoft Graph OAuth — device-code flow + token refresh.

Build-chunk 2 of Milestone 3. This module *obtains and refreshes* the
Microsoft Graph access token that build-chunk 1's
:class:`opencomputer.integrations.graph.client.GraphClient` consumes;
``GraphClient`` itself is agnostic about where its token comes from.

It is a thin Graph-specific layer over two already-shipped primitives:

* :mod:`opencomputer.auth.device_code` — the generic RFC 8628 device-code
  client (``request_device_code`` / ``poll_for_token``). It already handles
  ``authorization_pending`` / ``slow_down`` / ``expired_token`` / timeout, so
  this module does not reimplement device-code polling.
* :mod:`opencomputer.auth.token_store` — keyed-by-provider JSON token
  persistence (``auth_tokens.json``, file mode ``0600``). The Graph token
  lands under provider key :data:`PROVIDER` (``"graph"``).

What is genuinely Graph-specific — and therefore lives here — is:

1. The Microsoft identity-platform v2.0 endpoint URLs + the ``common`` tenant
   (so both personal Microsoft accounts and work/school accounts can sign in).
2. Resolving the public-client app id from ``OPENCOMPUTER_GRAPH_CLIENT_ID``
   (mirrors how :mod:`opencomputer.auth.google_oauth` resolves its client id).
3. :func:`refresh_access_token` — ``device_code.py`` only does the *initial*
   grant; the standard ``grant_type=refresh_token`` exchange is added here.
4. :func:`get_valid_access_token` — load the stored token, refresh proactively
   when it is at/near expiry, persist, return.

Security: the access + refresh tokens are stored at file mode ``0600`` by
``token_store`` and are **never** logged or placed in an error message.

App-registration prerequisite
-----------------------------
The device-code flow needs an Azure AD **public-client** app registration
with "Allow public client flows" enabled and the delegated permissions
``Mail.Send`` / ``Calendars.Read`` / ``Files.Read`` / ``offline_access``
added. That is a one-time Azure-portal action with no code substitute — OC
does not ship its own registration. Until the operator creates one and
exports its (non-secret) client id as ``OPENCOMPUTER_GRAPH_CLIENT_ID``, the
Graph login is inert and :func:`resolve_client_id` raises a clear error.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from opencomputer.auth.device_code import (
    DeviceCodeError,
    DeviceCodeResponse,
    poll_for_token,
    request_device_code,
    to_oauth_token,
)
from opencomputer.auth.token_store import (
    OAuthToken,
    delete_token,
    load_token,
    save_token,
)

# =============================================================================
# Endpoints & constants
# =============================================================================

#: Microsoft identity-platform v2.0 tenant. ``common`` accepts BOTH personal
#: Microsoft accounts and work/school accounts (``organizations`` = work/school
#: only, ``consumers`` = personal only). Overridable via env for operators who
#: must pin a single tenant — see :data:`ENV_TENANT`.
DEFAULT_TENANT = "common"

#: Microsoft identity-platform v2.0 authority host.
AUTHORITY_HOST = "https://login.microsoftonline.com"

#: Provider key the token is stored under in ``auth_tokens.json``.
PROVIDER = "graph"

#: Delegated scopes requested at login.
#:
#: * ``Mail.Send`` — ``POST /me/sendMail``.
#: * ``Calendars.Read`` — ``GET /me/calendarView`` with full event bodies.
#: * ``Files.Read`` — ``GET /me/drive/root/children``.
#: * ``offline_access`` — **mandatory**: without it Microsoft returns no
#:   refresh token, so the agent would have to re-prompt every ~1 hour.
GRAPH_SCOPES: tuple[str, ...] = (
    "Mail.Send",
    "Calendars.Read",
    "Files.Read",
    "offline_access",
)

#: :data:`GRAPH_SCOPES` as the single space-separated string the OAuth
#: endpoints expect.
SCOPE_STRING = " ".join(GRAPH_SCOPES)

#: Env var holding the Azure AD public-client app id. There is no shipped
#: default — see the module docstring's "App-registration prerequisite".
ENV_CLIENT_ID = "OPENCOMPUTER_GRAPH_CLIENT_ID"

#: Env var to override the tenant (rarely needed; ``common`` is the default).
ENV_TENANT = "OPENCOMPUTER_GRAPH_TENANT"

#: Refresh proactively once the access token is within this many seconds of
#: expiry. Graph access tokens last ~1 hour; a 5-minute skew leaves ample
#: room for clock drift and a slow refresh round-trip.
REFRESH_SKEW_SECONDS = 300

#: Timeout (seconds) for the one-shot refresh HTTP request.
REFRESH_TIMEOUT_SECONDS = 30.0


class GraphOAuthError(Exception):
    """Raised on Microsoft Graph OAuth failures.

    Covers a missing client id, a refused/timed-out device-code activation,
    a failed refresh, and "not logged in". Token values are **never**
    interpolated into the message.
    """


# =============================================================================
# Client-id resolution
# =============================================================================


def resolve_client_id() -> str:
    """Return the Azure AD public-client app id from the environment.

    Unlike Google's gemini-cli client id, there is no public OC Microsoft
    app registration to bake in as a default — the operator must register
    their own public-client app (see the module docstring) and export its id.

    Raises:
        GraphOAuthError: If ``OPENCOMPUTER_GRAPH_CLIENT_ID`` is unset or blank,
            with guidance on how to fix it.
    """
    client_id = (os.environ.get(ENV_CLIENT_ID) or "").strip()
    if not client_id:
        raise GraphOAuthError(
            "Microsoft Graph client id is not configured. Set the "
            f"{ENV_CLIENT_ID} environment variable to the Application "
            "(client) ID of an Azure AD public-client app registration "
            "that has 'Allow public client flows' enabled and the "
            "Mail.Send, Calendars.Read, Files.Read and offline_access "
            "delegated permissions. See "
            "docs/refs/microsoft-graph/2026-05-16-survey.md (section 4.6)."
        )
    return client_id


def resolve_tenant() -> str:
    """Return the identity-platform tenant — env override → ``common``."""
    return (os.environ.get(ENV_TENANT) or "").strip() or DEFAULT_TENANT


def _devicecode_url(tenant: str) -> str:
    return f"{AUTHORITY_HOST}/{tenant}/oauth2/v2.0/devicecode"


def _token_url(tenant: str) -> str:
    return f"{AUTHORITY_HOST}/{tenant}/oauth2/v2.0/token"


# =============================================================================
# Device-code login
# =============================================================================


@dataclass(frozen=True, slots=True)
class GraphLoginPrompt:
    """The user-facing instruction returned by :func:`begin_device_login`.

    The CLI prints :attr:`verification_uri` + :attr:`user_code` for the user,
    then hands the whole object to :func:`complete_device_login` to poll.
    """

    verification_uri: str
    user_code: str
    message: str
    expires_in: int
    interval: int
    _device_code: str
    _client_id: str
    _tenant: str


def begin_device_login() -> GraphLoginPrompt:
    """Start the device-code flow: request a device + user code from Microsoft.

    This is step one of a two-step login — the caller shows the returned
    :class:`GraphLoginPrompt` to the user, then calls
    :func:`complete_device_login` to poll until the user finishes signing in.

    The request carries :data:`SCOPE_STRING` — note that ``offline_access`` is
    included so the token response will include a refresh token.

    Raises:
        GraphOAuthError: If the client id is unconfigured, or Microsoft
            rejects the device-code request.
    """
    client_id = resolve_client_id()
    tenant = resolve_tenant()
    try:
        response: DeviceCodeResponse = request_device_code(
            device_code_url=_devicecode_url(tenant),
            client_id=client_id,
            scope=SCOPE_STRING,
        )
    except DeviceCodeError as exc:
        raise GraphOAuthError(
            f"Microsoft Graph device-code request failed: {exc}"
        ) from exc

    # Microsoft does not return `verification_uri_complete`; device_code.py
    # falls it back to `verification_uri`. The CLI must therefore print the
    # user_code separately and not imply the URI pre-fills it.
    return GraphLoginPrompt(
        verification_uri=response.verification_uri,
        user_code=response.user_code,
        message=(
            f"To sign in, open {response.verification_uri} in a browser and "
            f"enter the code {response.user_code}."
        ),
        expires_in=response.expires_in,
        interval=response.interval,
        _device_code=response.device_code,
        _client_id=client_id,
        _tenant=tenant,
    )


def complete_device_login(prompt: GraphLoginPrompt) -> OAuthToken:
    """Poll the token endpoint until the user completes the device-code login.

    Blocks (with the server-mandated polling interval) until Microsoft
    returns a token, the user declines, or the codes expire. On success the
    resulting :class:`OAuthToken` — access token, refresh token, expiry,
    granted scope — is persisted under provider key :data:`PROVIDER` and
    returned.

    Args:
        prompt: The :class:`GraphLoginPrompt` from :func:`begin_device_login`.

    Raises:
        GraphOAuthError: If the user declines, the codes expire, or any other
            non-recoverable device-code error occurs.
    """
    try:
        raw: dict[str, Any] = poll_for_token(
            token_url=_token_url(prompt._tenant),
            client_id=prompt._client_id,
            device_code=prompt._device_code,
            interval=prompt.interval,
            max_wait_seconds=prompt.expires_in,
        )
    except DeviceCodeError as exc:
        raise GraphOAuthError(
            f"Microsoft Graph sign-in did not complete: {exc}"
        ) from exc

    token = to_oauth_token(PROVIDER, raw)
    save_token(token)
    return token


# =============================================================================
# Refresh
# =============================================================================


def refresh_access_token(token: OAuthToken | None = None) -> OAuthToken:
    """Mint a fresh access token from the stored refresh token.

    Performs the standard ``grant_type=refresh_token`` exchange against the
    Microsoft identity-platform ``/token`` endpoint —
    :mod:`opencomputer.auth.device_code` has no refresh helper, so it is done
    here. Microsoft usually rotates the refresh token on each refresh; the new
    one (or the old one, if none is returned) is persisted. Safe to call
    repeatedly.

    Args:
        token: The current :class:`OAuthToken` to refresh. When ``None``, the
            stored Graph token is loaded.

    Returns:
        The refreshed :class:`OAuthToken`, already persisted.

    Raises:
        GraphOAuthError: If there is no stored token / no refresh token, or
            Microsoft rejects the refresh (e.g. a revoked or expired refresh
            token — the user must run ``oc auth login graph`` again).
    """
    current = token if token is not None else load_token(PROVIDER)
    if current is None:
        raise GraphOAuthError(
            "Microsoft Graph: not logged in. Run `oc auth login graph` first."
        )
    if not current.refresh_token:
        raise GraphOAuthError(
            "Microsoft Graph: stored token has no refresh token "
            "(was 'offline_access' granted?). Run `oc auth login graph` again."
        )

    client_id = resolve_client_id()
    tenant = resolve_tenant()
    try:
        response = httpx.post(
            _token_url(tenant),
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": current.refresh_token,
                "scope": SCOPE_STRING,
            },
            headers={"Accept": "application/json"},
            timeout=REFRESH_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as exc:
        raise GraphOAuthError(
            f"Microsoft Graph token refresh failed: {exc}"
        ) from exc

    if response.status_code != 200:
        # Surface the OAuth error code but never the token. A 4xx here is
        # typically `invalid_grant` — the refresh token is dead.
        try:
            err = response.json().get("error", "unknown")
        except (ValueError, AttributeError, KeyError):
            err = f"http {response.status_code}"
        raise GraphOAuthError(
            f"Microsoft Graph token refresh rejected ({err}). "
            "The session may have been revoked — run `oc auth login graph`."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise GraphOAuthError(
            f"Microsoft Graph returned an unparseable refresh response: {exc}"
        ) from exc

    if "access_token" not in payload:
        raise GraphOAuthError(
            "Microsoft Graph refresh response did not include an access token."
        )

    # Microsoft rotates the refresh token on most refreshes; keep the old one
    # only if the response omits a replacement.
    payload.setdefault("refresh_token", current.refresh_token)
    refreshed = to_oauth_token(PROVIDER, payload)
    # Preserve the originally-granted scope string if Microsoft returns none.
    if not refreshed.scope and current.scope:
        refreshed = OAuthToken(
            provider=refreshed.provider,
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token,
            expires_at=refreshed.expires_at,
            scope=current.scope,
            extra=refreshed.extra,
        )
    save_token(refreshed)
    return refreshed


# =============================================================================
# Token accessor (used by C3's Graph tools)
# =============================================================================


def get_valid_access_token(*, force_refresh: bool = False) -> str:
    """Return a usable Microsoft Graph access token, refreshing if needed.

    Loads the stored Graph token and returns its access token. When the token
    is at or near expiry (within :data:`REFRESH_SKEW_SECONDS`) — or
    ``force_refresh`` is set — it is refreshed via :func:`refresh_access_token`
    *before* being returned, so the caller always gets a token good for at
    least the skew window. A still-fresh token is returned without any network
    call.

    This is the entry point build-chunk 3's Graph tools call to obtain a token
    for :class:`opencomputer.integrations.graph.client.GraphClient`.

    Args:
        force_refresh: Refresh even if the stored token is not near expiry —
            e.g. after a Graph ``401`` indicating the token was rejected.

    Returns:
        A Microsoft Graph access token string.

    Raises:
        GraphOAuthError: If there is no stored token (the user has not run
            ``oc auth login graph``), or a needed refresh fails.
    """
    token = load_token(PROVIDER)
    if token is None or not token.access_token:
        raise GraphOAuthError(
            "Microsoft Graph: not logged in. Run `oc auth login graph` first."
        )
    if force_refresh or token.expires_soon(REFRESH_SKEW_SECONDS):
        token = refresh_access_token(token)
    return token.access_token


def has_stored_token() -> bool:
    """Return whether a Microsoft Graph token is persisted.

    Used to gate the Graph tools' registration so they stay inert until the
    user has run ``oc auth login graph``.
    """
    token = load_token(PROVIDER)
    return token is not None and bool(token.access_token)


def logout() -> bool:
    """Delete the stored Microsoft Graph token.

    Returns:
        ``True`` if a token was present and removed, ``False`` if there was
        nothing to delete. Idempotent.
    """
    had_token = has_stored_token()
    delete_token(PROVIDER)
    return had_token


def stored_account_summary() -> str | None:
    """Return a short, non-secret description of the logged-in Graph session.

    Reports the granted scopes and (if Microsoft returned an expiry) how long
    the current access token remains valid. Returns ``None`` when not logged
    in. Never includes the access or refresh token.
    """
    token = load_token(PROVIDER)
    if token is None or not token.access_token:
        return None
    parts: list[str] = []
    if token.scope:
        parts.append(f"scopes: {token.scope}")
    if token.expires_at:
        remaining = token.expires_at - int(time.time())
        if remaining > 0:
            parts.append(f"access token valid for ~{remaining // 60} min")
        else:
            parts.append("access token expired (will refresh on next use)")
    return "; ".join(parts) if parts else "logged in"


__all__ = [
    "AUTHORITY_HOST",
    "DEFAULT_TENANT",
    "ENV_CLIENT_ID",
    "ENV_TENANT",
    "GRAPH_SCOPES",
    "PROVIDER",
    "REFRESH_SKEW_SECONDS",
    "SCOPE_STRING",
    "GraphLoginPrompt",
    "GraphOAuthError",
    "begin_device_login",
    "complete_device_login",
    "get_valid_access_token",
    "has_stored_token",
    "logout",
    "refresh_access_token",
    "resolve_client_id",
    "resolve_tenant",
    "stored_account_summary",
]
