"""Auto-fetch URLs from incoming user messages and inject summaries.

The injection runs every turn. It scans the last user message for HTTP(S)
URLs, fetches each one (with SSRF guards + per-session caching), and
returns a system-prompt addendum like::

    ## Link summaries
    The user's most recent message contains URLs we pre-fetched:

    ### https://example.com/article
    [first 1500 chars of article text]

    ### https://example.com/another
    [first 1500 chars of another article]

This way the agent sees the URL content inline alongside the user's
message instead of having to call ``WebFetch`` itself first.

Wiring: registered in ``register(api)`` of any plugin that wants to
opt into auto-link-fetch. The bundled coding-harness plugin enables it
by default; users can disable per-profile by mutating
``link_understanding.DEFAULT_CONFIG.enabled = False`` in their startup
hook or by removing this provider from the registration list.
"""

from __future__ import annotations

import logging

from opencomputer.agent.link_understanding import (
    DEFAULT_CONFIG,
    LinkFetcher,
    LinkUnderstandingConfig,
    _cache_for,
    extract_urls,
    is_safe_url,
)
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext

logger = logging.getLogger("opencomputer.agent.injection_providers.link_summary")


class LinkUnderstandingInjectionProvider(DynamicInjectionProvider):
    """Pre-fetch URLs in the latest user message and inject summaries.

    Priority is mid-range (``50``) — we want the injection to land
    AFTER identity / mode injections (which are higher-priority,
    typically ``100``+) but BEFORE skill-activation (``80``) so the
    activated skill can react to the link content. Lower number = lower
    priority in the existing convention.
    """

    priority = 50

    def __init__(
        self,
        *,
        config: LinkUnderstandingConfig | None = None,
        fetcher: LinkFetcher | None = None,
    ) -> None:
        # Tests can pass a custom config + fetcher; production code uses
        # the module defaults.
        self._config = config or DEFAULT_CONFIG
        self._fetcher = fetcher or LinkFetcher.shared()

    @property
    def provider_id(self) -> str:
        return "link-understanding"

    async def collect(self, ctx: InjectionContext) -> str | None:
        if not self._config.enabled:
            return None

        # Find the last user message's text.
        last_user_text = ""
        for msg in reversed(ctx.messages or ()):
            role = getattr(msg, "role", None)
            role_value = getattr(role, "value", role) if role is not None else None
            if role_value == "user":
                last_user_text = getattr(msg, "content", "") or ""
                break
        if not last_user_text:
            return None

        urls = extract_urls(last_user_text, max_urls=self._config.max_urls_per_message)
        if not urls:
            return None

        # SSRF + cache + fetch. We don't bail on the first bad URL — a
        # mix of safe + blocked URLs in one message should still produce
        # summaries for the safe ones.
        cache = _cache_for(ctx.session_id or "default", self._config.cache_max_per_session)
        summaries: list[tuple[str, str]] = []  # (url, body) in user-typed order

        for url in urls:
            cached = cache.get(url)
            if cached is not None:
                summaries.append((url, cached))
                continue
            if not is_safe_url(url):
                logger.info("link_understanding: refusing unsafe URL %r", url)
                summaries.append((url, "[refused: unsafe URL (private IP, cloud metadata, or unresolvable)]"))
                continue
            body = await self._fetcher.fetch(
                url,
                max_chars=self._config.per_url_max_chars,
                timeout_s=self._config.timeout_s,
            )
            if body is None:
                summaries.append((url, "[fetch failed — see logs]"))
                continue
            cache.put(url, body)
            summaries.append((url, body))

        if not summaries:
            return None

        sections = ["## Link summaries",
                    "The user's most recent message contains URLs we pre-fetched:"]
        for url, body in summaries:
            sections.append("")
            sections.append(f"### {url}")
            sections.append(body)
        return "\n".join(sections)


__all__ = ["LinkUnderstandingInjectionProvider"]
