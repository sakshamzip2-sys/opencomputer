"""Gemini OAuth provider — Google Cloud Code Assist via desktop OAuth (PKCE).

Status: **fully wired**. Authenticates via Google's PKCE OAuth desktop client
(``opencomputer auth login google``), reads + refreshes credentials from
``~/.opencomputer/auth/google_oauth.json``, and routes inference through
``cloudcode-pa.googleapis.com/v1internal:*`` (Google's Cloud Code Assist
backend — same one ``gemini-cli`` uses).

Project resolution order:

  1. ``OPENCOMPUTER_GEMINI_PROJECT_ID`` env var
  2. ``GOOGLE_CLOUD_PROJECT`` / ``GOOGLE_CLOUD_PROJECT_ID`` env vars
  3. Stored ``project_id`` in google_oauth.json
  4. ``loadCodeAssist`` preflight (Google reports user's tier + project)
  5. ``onboardUser`` on free-tier (auto-provision if no tier yet)

Inference uses the OC ``BaseProvider`` interface directly — Cloud Code Assist
is NOT OpenAI-compatible, so the OpenAIProvider subclass pattern doesn't
work here. The :class:`CloudCodeTransport` handles the wire format.

To set up:

  1. ``opencomputer auth login google``  (PKCE browser flow)
  2. Done — wizard discovers ``gemini-oauth`` and your account is ready.

Env overrides:

  OPENCOMPUTER_GEMINI_PROJECT_ID  — override discovered project_id
  OPENCOMPUTER_GEMINI_CLIENT_ID   — override OAuth client_id (defaults to
                                    Google's public gemini-cli client)
  OPENCOMPUTER_GEMINI_CLIENT_SECRET — same, for client_secret
"""
from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

_TRANSPORT_DIR = Path(__file__).resolve().parent
if str(_TRANSPORT_DIR) not in sys.path:
    sys.path.insert(0, str(_TRANSPORT_DIR))

from cloudcode_transport import CloudCodeTransport  # type: ignore[import-not-found]  # noqa: E402

from opencomputer.auth.google_code_assist import (  # noqa: E402
    ProjectContext,
    resolve_project_context,
)
from opencomputer.auth.google_oauth import (  # noqa: E402
    DEFAULT_GEMINI_CLOUDCODE_BASE_URL,
    get_valid_access_token,
    load_credentials,
)
from plugin_sdk.core import Message  # noqa: E402
from plugin_sdk.provider_contract import (  # noqa: E402
    BaseProvider,
    ProviderResponse,
    StreamEvent,
)
from plugin_sdk.tool_contract import ToolSchema  # noqa: E402


class GeminiOAuthProvider(BaseProvider):
    """Google Gemini via OAuth + Cloud Code Assist."""

    name = "gemini-oauth"
    default_model = "gemini-2.5-pro"

    def __init__(
        self,
        api_key: str | None = None,  # ignored for OAuth flow; param for compat
        base_url: str | None = None,
        api_mode: str | None = None,  # accepted for ModelConfig.api_mode plumbing
    ) -> None:
        # Pre-flight: ensure the user is logged in (don't make HTTP calls yet)
        creds = load_credentials()
        if not creds:
            raise RuntimeError(
                "Gemini OAuth: not logged in. Run "
                "`opencomputer auth login google` to authenticate "
                "(opens a browser for Google's PKCE consent flow). "
                "See https://aistudio.google.com for sign-up."
            )

        self._base = base_url or DEFAULT_GEMINI_CLOUDCODE_BASE_URL
        self._api_key = creds.access_token  # exposed for compatibility
        self._project_context: ProjectContext | None = None

        self._transport = CloudCodeTransport(
            access_token_provider=lambda: get_valid_access_token(),
            project_id_provider=self._get_project_id,
        )

    def _get_project_id(self) -> str:
        """Lazy project resolution — runs once, caches the result.

        Defers the loadCodeAssist / onboardUser HTTP traffic until first
        inference call so wizard discovery doesn't trigger network I/O.
        """
        if self._project_context is None:
            access_token = get_valid_access_token()
            creds = load_credentials()
            configured = (creds.project_id if creds else "") or ""
            self._project_context = resolve_project_context(
                access_token=access_token,
                configured_project_id=configured,
            )
        return self._project_context.project_id

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stream: bool = False,
        runtime_extras: dict | None = None,
    ) -> ProviderResponse:
        return await self._transport.complete(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        runtime_extras: dict | None = None,
    ) -> AsyncIterator[StreamEvent]:
        async for event in self._transport.stream_complete(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            yield event
