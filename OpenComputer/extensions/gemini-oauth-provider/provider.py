"""Gemini OAuth provider — Google Cloud Code Assist via desktop OAuth (PKCE).

Status: **OAuth foundation shipped**, transport adapter pending.

Inference for OAuth-authenticated Gemini does NOT use Google AI Studio's
OpenAI-compatible endpoint — that endpoint accepts API keys only, not
Bearer access tokens. OAuth tokens authorize Google's *Cloud Code Assist*
backend at ``https://cloudcode-pa.googleapis.com/v1internal:*``, which has
its own JSON-RPC-shaped wire protocol.

This plugin currently:

  - registers a provider entry so the wizard can discover ``gemini-oauth``
  - reads/refreshes credentials from ``~/.opencomputer/auth/google_oauth.json``
    via :mod:`opencomputer.auth.google_oauth`
  - raises a clear ``NotImplementedError`` on any inference call, with
    actionable guidance to either (a) use the Google AI Studio API key
    provider for now or (b) wait for the Cloud Code Assist adapter

To set up:

  1. ``opencomputer auth login google``  (PKCE browser flow)
  2. Wait for the Cloud Code Assist adapter (tracked in the onboarding
     roadmap doc).

For users who want OpenAI-compatible Gemini access today: install a
``gemini-google`` provider plugin that uses an AI Studio API key against
``https://generativelanguage.googleapis.com/v1beta/openai``. The OAuth
flow shipped here is the foundation for the Cloud Code Assist path,
which gives access to higher request quotas tied to the user's Google
account.
"""
from __future__ import annotations

import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

from opencomputer.auth.google_oauth import (  # noqa: E402
    DEFAULT_GEMINI_CLOUDCODE_BASE_URL,
    get_valid_access_token,
    load_credentials,
)


class GeminiOAuthProvider(OpenAIProvider):
    name = "gemini-oauth"
    default_model = "gemini-2.5-pro"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not api_key:
            creds = load_credentials()
            if not creds:
                raise RuntimeError(
                    "Gemini OAuth: not logged in. Run "
                    "`opencomputer auth login google` to authenticate "
                    "(opens a browser for Google's PKCE consent flow). "
                    "See https://aistudio.google.com for sign-up."
                )
            api_key = get_valid_access_token()
        resolved_base = base_url or DEFAULT_GEMINI_CLOUDCODE_BASE_URL
        super().__init__(api_key=api_key, base_url=resolved_base)

    async def _post(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError(
            "Gemini OAuth uses Google's Cloud Code Assist backend "
            "(cloudcode-pa.googleapis.com/v1internal), not an OpenAI-compatible "
            "endpoint. The Cloud Code Assist transport adapter is a pending "
            "follow-up — see docs/superpowers/specs/2026-05-02-hermes-onboarding-roadmap.md. "
            "For OpenAI-compat Gemini access today, install a provider that uses "
            "an AI Studio API key against generativelanguage.googleapis.com/v1beta/openai."
        )

    async def complete(self, *args, **kwargs):  # type: ignore[override]
        return await self._post()

    async def stream_complete(self, *args, **kwargs):  # type: ignore[override]
        await self._post()
        if False:  # pragma: no cover - unreachable
            yield None
