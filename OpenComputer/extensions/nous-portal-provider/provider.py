"""Nous Portal provider — OAuth device-code flow + OpenAI-compatible inference.

Hermes uses ``hermes-cli`` as their registered client_id with Nous Portal.
That's *their* registration; OC needs its own (or env-var override).

Flow:
  1. User picks "Nous Portal" in wizard.
  2. ``run_device_code_login`` is called: hits portal device-endpoint,
     prints user_code + verification URL, polls token endpoint until
     user approves in browser.
  3. Token persisted via ``opencomputer.auth.token_store.save_token``.
  4. At inference time, the provider reads the token and uses it as
     ``Authorization: Bearer <access_token>`` against the inference API.

Inference is OpenAI-compatible at https://inference-api.nousresearch.com/v1
— so we subclass OpenAIProvider, just with the OAuth-derived token
instead of a static API key.

Env vars:
  NOUS_PORTAL_CLIENT_ID  — OAuth client_id (default: ``opencomputer-cli``;
                            override if you've registered your own).
  NOUS_PORTAL_API_KEY    — set after device-code flow completes; can
                            also be a pre-issued portal API key from
                            https://portal.nousresearch.com if you
                            prefer non-OAuth auth.
  NOUS_PORTAL_BASE_URL   — inference endpoint override (default:
                            https://inference-api.nousresearch.com/v1).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_NOUS_PORTAL_BASE_URL = "https://inference-api.nousresearch.com/v1"
DEFAULT_NOUS_PORTAL_CLIENT_ID = "opencomputer-cli"
DEFAULT_NOUS_PORTAL_AUTH_BASE_URL = "https://portal.nousresearch.com"


class NousPortalProvider(OpenAIProvider):
    """Subscription-backed provider via Nous Portal.

    Token is sourced from one of (in order):
      1. ``NOUS_PORTAL_API_KEY`` env (set after device-code flow OR
         manually for pre-issued portal keys)
      2. ``opencomputer.auth.token_store`` entry under provider="nous-portal"
         (set by ``run_device_code_login`` below)
    """

    name = "nous-portal"
    default_model = "Hermes-3-Llama-3.1-405B"
    _api_key_env: str = "NOUS_PORTAL_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        # Try env var first
        resolved_key = api_key or os.environ.get(self._api_key_env)

        # Fall back to OAuth-derived token from the auth store
        if not resolved_key:
            try:
                from opencomputer.auth import load_token
                token = load_token("nous-portal")
                if token and not token.is_expired():
                    resolved_key = token.access_token
            except Exception:  # noqa: BLE001
                pass

        if not resolved_key:
            raise RuntimeError(
                f"{self._api_key_env} is not set and no OAuth token in store. "
                "Run `oc setup --new` and pick Nous Portal to authenticate, "
                "or get a pre-issued key at https://portal.nousresearch.com."
            )

        resolved_base = (
            base_url
            or os.environ.get("NOUS_PORTAL_BASE_URL")
            or DEFAULT_NOUS_PORTAL_BASE_URL
        )
        super().__init__(api_key=resolved_key, base_url=resolved_base)


def run_device_code_login(
    *,
    client_id: str | None = None,
    portal_base_url: str | None = None,
    print_fn=print,
) -> None:
    """Drive the device-code flow end-to-end + persist the token.

    Called from the wizard when user picks Nous Portal. Returns silently
    on success (token saved to ~/.opencomputer/auth_tokens.json); raises
    DeviceCodeError on failure.
    """
    from opencomputer.auth import save_token
    from opencomputer.auth.device_code import (
        poll_for_token,
        request_device_code,
        to_oauth_token,
    )

    client_id = client_id or os.environ.get(
        "NOUS_PORTAL_CLIENT_ID", DEFAULT_NOUS_PORTAL_CLIENT_ID,
    )
    base = (
        portal_base_url
        or os.environ.get("NOUS_PORTAL_BASE_URL")
        or DEFAULT_NOUS_PORTAL_AUTH_BASE_URL
    ).rstrip("/")
    device_code_url = f"{base}/api/oauth/device/code"
    token_url = f"{base}/api/oauth/token"

    print_fn(f"  Starting Nous Portal device-code login (client_id={client_id})…")
    response = request_device_code(
        device_code_url=device_code_url,
        client_id=client_id,
        scope="inference:read inference:write",
    )

    print_fn("")
    print_fn(f"  Visit: {response.verification_uri_complete}")
    print_fn(f"  Or go to {response.verification_uri} and enter code: "
             f"{response.user_code}")
    print_fn(f"  (Code expires in {response.expires_in // 60} minutes.)")
    print_fn("")
    print_fn("  Polling for completion — approve in your browser…")

    token_response = poll_for_token(
        token_url=token_url,
        client_id=client_id,
        device_code=response.device_code,
        interval=response.interval,
        max_wait_seconds=response.expires_in,
    )

    save_token(to_oauth_token("nous-portal", token_response))
    print_fn("  ✓ Nous Portal authentication complete.")
