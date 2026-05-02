"""Generic OAuth device-code flow (RFC 8628).

Modeled after Hermes's per-provider device-code logic
(``hermes_cli/auth.py::_request_device_code`` etc) but unified into one
provider-agnostic module. Provider plugins call ``request_device_code``
+ ``poll_for_token`` with the URLs + client_id from their own manifest.

Pattern:
    response = request_device_code(
        device_code_url="https://provider.example/oauth/device/code",
        client_id="my-app",
        scope="read write",
    )
    print(f"Visit {response.verification_uri} and enter {response.user_code}")
    token_response = poll_for_token(
        token_url="https://provider.example/oauth/token",
        client_id="my-app",
        device_code=response.device_code,
        interval=response.interval,
        max_wait_seconds=response.expires_in,
    )
    save_token(to_oauth_token("my-provider", token_response))
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from opencomputer.auth.token_store import OAuthToken


class DeviceCodeError(Exception):
    """Raised on device-code flow failures (invalid_client, expired_token,
    timeout, etc.). Message includes the OAuth error code from the server
    response when available."""


@dataclass(frozen=True, slots=True)
class DeviceCodeResponse:
    """Response from the device authorization endpoint (RFC 8628 § 3.2)."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int = 5
    """Polling interval in seconds (server hint; can be bumped by slow_down)."""


def request_device_code(
    device_code_url: str,
    client_id: str,
    scope: str = "",
    *,
    timeout_seconds: float = 30.0,
) -> DeviceCodeResponse:
    """RFC 8628 § 3.1 — request a device + user code from the provider.

    Returns a parsed DeviceCodeResponse. Raises DeviceCodeError on
    server error (4xx) or unparseable response.
    """
    payload: dict[str, str] = {"client_id": client_id}
    if scope:
        payload["scope"] = scope

    try:
        response = httpx.post(
            device_code_url,
            data=payload,
            headers={"Accept": "application/json"},
            timeout=timeout_seconds,
        )
    except httpx.RequestError as e:
        raise DeviceCodeError(f"request failed: {e}") from e

    if response.status_code != 200:
        try:
            err = response.json().get("error", "unknown")
        except (ValueError, KeyError):
            err = f"http {response.status_code}"
        raise DeviceCodeError(
            f"device code request failed: {err} ({response.status_code})"
        )

    try:
        data = response.json()
    except ValueError as e:
        raise DeviceCodeError(f"unparseable response: {e}") from e

    return DeviceCodeResponse(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data["verification_uri"],
        verification_uri_complete=data.get(
            "verification_uri_complete", data["verification_uri"],
        ),
        expires_in=int(data.get("expires_in", 900)),
        interval=int(data.get("interval", 5)),
    )


def poll_for_token(
    token_url: str,
    client_id: str,
    device_code: str,
    *,
    interval: int = 5,
    max_wait_seconds: int = 900,
    grant_type: str = "urn:ietf:params:oauth:grant-type:device_code",
) -> dict[str, Any]:
    """RFC 8628 § 3.4 — poll the token endpoint until access granted,
    timed out, or rejected.

    Returns the raw token response dict (access_token, refresh_token,
    expires_in, scope, token_type, ...). Caller can pass through to
    ``to_oauth_token`` for storage.

    Raises DeviceCodeError on any non-recoverable server response
    (expired_token, access_denied, invalid_grant) or when
    max_wait_seconds elapses without success.
    """
    start = time.monotonic()
    current_interval = interval

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= max_wait_seconds:
            raise DeviceCodeError(
                f"polling timed out after {max_wait_seconds}s "
                "(user did not complete device-code activation)"
            )

        # Sleep BEFORE polling (the device authorization endpoint
        # already returned; user needs time to visit the URL).
        time.sleep(current_interval)

        try:
            response = httpx.post(
                token_url,
                data={
                    "grant_type": grant_type,
                    "client_id": client_id,
                    "device_code": device_code,
                },
                headers={"Accept": "application/json"},
                timeout=30.0,
            )
        except httpx.RequestError:
            # Transient network blip — keep polling
            continue

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as e:
                raise DeviceCodeError(f"unparseable token response: {e}") from e

        # Error path — examine the OAuth error code (RFC 8628 § 3.5)
        try:
            err = response.json().get("error", "unknown")
        except (ValueError, KeyError):
            err = f"http {response.status_code}"

        if err == "authorization_pending":
            # User hasn't activated yet — keep polling at current interval
            continue
        if err == "slow_down":
            # Server says we're polling too fast — bump interval by 5s per RFC
            current_interval += 5
            continue
        if err in ("expired_token", "access_denied", "invalid_grant"):
            raise DeviceCodeError(f"device code {err}")
        # Unknown error — surface it
        raise DeviceCodeError(f"unexpected token-endpoint error: {err}")


def to_oauth_token(provider: str, response: dict[str, Any]) -> OAuthToken:
    """Convert a token-endpoint response dict to an OAuthToken for
    storage via ``opencomputer.auth.token_store.save_token``."""
    expires_in = int(response.get("expires_in", 0))
    expires_at = int(time.time()) + expires_in if expires_in > 0 else 0

    extra: dict[str, Any] = {}
    for k in ("token_type", "id_token"):
        v = response.get(k)
        if v:
            extra[k] = v

    return OAuthToken(
        provider=provider,
        access_token=response["access_token"],
        refresh_token=response.get("refresh_token"),
        expires_at=expires_at,
        scope=response.get("scope", ""),
        extra=extra,
    )


__all__ = [
    "DeviceCodeError",
    "DeviceCodeResponse",
    "poll_for_token",
    "request_device_code",
    "to_oauth_token",
]
