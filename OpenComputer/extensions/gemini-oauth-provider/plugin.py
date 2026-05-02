"""Gemini OAuth provider plugin (Google Cloud Code Assist auth)."""
from __future__ import annotations

from provider import GeminiOAuthProvider  # type: ignore[import-not-found]


def register(api) -> None:
    api.register_provider("gemini-oauth", GeminiOAuthProvider)
