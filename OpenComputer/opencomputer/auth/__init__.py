"""OAuth token storage + future device-code flow for OAuth-based providers.

Built so OC's OAuth provider plugins (Nous Portal, GitHub Copilot,
Google Gemini OAuth, Qwen OAuth) can persist refresh + access tokens
without re-prompting on every chat session.

Public API today:
    from opencomputer.auth import OAuthToken, load_token, save_token, delete_token

Future modules in this package (deferred — separate PRs):
  - device_code.py — generic device-code flow client (request →
    poll → exchange). Needs httpx mocking + real client_id
    registrations with each provider; deferred until OC has
    those registrations.
  - external.py — browser-redirect OAuth (Google Gemini, Qwen).
"""
from opencomputer.auth.token_store import (
    OAuthToken,
    default_store_path,
    delete_token,
    load_token,
    save_token,
)

__all__ = [
    "OAuthToken",
    "default_store_path",
    "delete_token",
    "load_token",
    "save_token",
]
