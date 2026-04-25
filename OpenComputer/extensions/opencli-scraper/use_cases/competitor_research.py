"""First-party company-page scanner for competitor research.

Scans a company's public website sections (homepage, about, blog, pricing)
and returns structured snippets. Uses a PUBLIC-only strategy — any domain
that would require cookie-based authentication (e.g. LinkedIn company pages)
is refused.

This module does NOT register any tools. It is a library of functions
callable from tests and other plugin code.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from wrapper import OpenCLIWrapper  # type: ignore[import-not-found]  # noqa: E402

log = logging.getLogger(__name__)

# Domains known to require COOKIE strategy — refuse these to keep research
# above-board. This list mirrors the non-public adapters in the main plugin.
_COOKIE_STRATEGY_DOMAINS: frozenset[str] = frozenset(
    [
        "linkedin.com",
        "www.linkedin.com",
        "facebook.com",
        "www.facebook.com",
        "instagram.com",
        "www.instagram.com",
        "x.com",
        "twitter.com",
    ]
)

_DEFAULT_SECTIONS: list[str] = ["homepage", "about", "blog", "pricing"]

# Map section name → typical URL suffix (relative to domain root).
_SECTION_PATHS: dict[str, str] = {
    "homepage": "",
    "about": "/about",
    "blog": "/blog",
    "pricing": "/pricing",
    "team": "/team",
    "careers": "/careers",
    "docs": "/docs",
}


def _normalise_domain(domain: str) -> str:
    """Strip scheme and trailing slash from *domain*."""
    domain = domain.strip().rstrip("/")
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    # Strip path component if provided (keep just host).
    domain = domain.split("/")[0]
    return domain.lower()


def _check_public_strategy(domain: str) -> None:
    """Raise ``ValueError`` if *domain* requires a COOKIE strategy."""
    normalised = _normalise_domain(domain)
    # Check exact match and www. variant.
    if normalised in _COOKIE_STRATEGY_DOMAINS:
        raise ValueError(
            f"Domain {domain!r} requires a COOKIE auth strategy and is not supported "
            "by competitor_research (PUBLIC-only). Use a first-party data source instead."
        )
    # Also block www. variants of any blocked domain.
    base = normalised.removeprefix("www.")
    if base in _COOKIE_STRATEGY_DOMAINS or f"www.{base}" in _COOKIE_STRATEGY_DOMAINS:
        raise ValueError(
            f"Domain {domain!r} requires a COOKIE auth strategy and is not supported "
            "by competitor_research (PUBLIC-only). Use a first-party data source instead."
        )


async def _fetch_section(
    wrapper: OpenCLIWrapper,
    base_url: str,
    section: str,
) -> dict:
    """Fetch a single page section and return a structured snippet dict.

    Parameters
    ----------
    wrapper:
        An ``OpenCLIWrapper`` instance.
    base_url:
        Base URL of the company website (e.g. ``"https://example.com"``).
    section:
        Section name (e.g. ``"about"``).

    Returns
    -------
    dict
        ``{"url": str, "title": str, "snippet": str}`` — or an error dict
        if the fetch fails.
    """
    path = _SECTION_PATHS.get(section, f"/{section}")
    url = f"{base_url}{path}".rstrip("/") if path else base_url

    try:
        raw = await wrapper.run("", url)
        data = raw.get("data", raw) if isinstance(raw, dict) else {}
        title = str(data.get("title", "")).strip() or f"{section.capitalize()} page"
        # Use first 500 chars of body/text as the snippet.
        body = str(data.get("body", data.get("text", data.get("content", "")))).strip()
        snippet = body[:500] if body else ""
        return {"url": url, "title": title, "snippet": snippet}
    except Exception as exc:
        log.warning("competitor_research: failed to fetch %r section for %r — %s", section, base_url, exc)
        return {"url": url, "title": "", "snippet": "", "error": str(exc)}


async def scan_company_page(
    wrapper: OpenCLIWrapper,
    company_domain: str,
    sections: list[str] | None = None,
) -> dict:
    """Scan a company's public website and return section snippets.

    Parameters
    ----------
    wrapper:
        An ``OpenCLIWrapper`` instance.
    company_domain:
        Domain of the company to scan (e.g. ``"stripe.com"`` or
        ``"https://stripe.com"``).
    sections:
        Which pages to fetch. Defaults to
        ``["homepage", "about", "blog", "pricing"]``.

    Returns
    -------
    dict
        ``{"sections": {section_name: {"url": str, "title": str, "snippet": str}}}``

    Raises
    ------
    ValueError
        If the domain requires a COOKIE strategy (e.g. ``"linkedin.com"``).
    """
    _check_public_strategy(company_domain)

    if sections is None:
        sections = list(_DEFAULT_SECTIONS)

    domain = _normalise_domain(company_domain)
    base_url = f"https://{domain}"

    result_sections: dict[str, dict] = {}
    for section in sections:
        result_sections[section] = await _fetch_section(wrapper, base_url, section)

    return {"sections": result_sections}


async def compare_companies(
    wrapper: OpenCLIWrapper,
    domains: list[str],
) -> dict:
    """Scan multiple company pages and return a side-by-side comparison.

    Parameters
    ----------
    wrapper:
        An ``OpenCLIWrapper`` instance.
    domains:
        List of company domains to scan.

    Returns
    -------
    dict
        Mapping of ``domain → scan_company_page result``. Domains that fail
        the PUBLIC-strategy check have an ``{"error": str}`` entry instead.
    """
    comparison: dict[str, dict] = {}
    for domain in domains:
        try:
            comparison[domain] = await scan_company_page(wrapper, domain)
        except ValueError as exc:
            log.warning("compare_companies: refusing %r — %s", domain, exc)
            comparison[domain] = {"error": str(exc)}
    return comparison


__all__ = ["scan_company_page", "compare_companies"]
