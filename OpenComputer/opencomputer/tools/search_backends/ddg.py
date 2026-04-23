"""DuckDuckGo HTML backend — keyless, the fallback default.

Scrapes `html.duckduckgo.com/html/`. The HTML interface is intentionally
scraper-friendly (no JS required) and rate limits are lenient. No API key.

DDG wraps result links through `/l/?uddg=<encoded>` redirects which we
unwrap to expose the real destination URL. Otherwise the agent ends up
sending users back to DDG instead of the actual page.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from opencomputer.tools.search_backends.base import (
    SearchBackend,
    SearchBackendError,
    SearchHit,
)

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DEFAULT_USER_AGENT = "OpenComputer/0.1 (+https://github.com/sakshamzip2-sys/opencomputer)"


def _unwrap_ddg_redirect(href: str) -> str:
    """`/l/?uddg=<encoded-url>` → unencoded destination. No-op for plain URLs."""
    if not href:
        return href
    parsed = urlparse(href)
    if "duckduckgo" in (parsed.netloc or "") and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return href


def _parse_html_results(html: str, max_results: int) -> list[SearchHit]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[SearchHit] = []
    for result in soup.select(".result"):
        title_el = result.select_one(".result__title a") or result.select_one("h2 a")
        snippet_el = result.select_one(".result__snippet")
        if title_el is None:
            continue
        title = title_el.get_text(" ", strip=True)
        href = _unwrap_ddg_redirect(title_el.get("href", "") or "")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        if not title or not href:
            continue
        out.append(SearchHit(title=title, url=href, snippet=snippet))
        if len(out) >= max_results:
            break
    return out


class DuckDuckGoBackend(SearchBackend):
    id = "ddg"
    env_var = ""  # keyless
    signup_url = ""

    async def search(
        self,
        *,
        query: str,
        max_results: int,
        timeout_s: float,
    ) -> list[SearchHit]:
        async with httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        ) as client:
            resp = await client.post(DDG_HTML_URL, data={"q": query})
        if resp.status_code >= 400:
            raise SearchBackendError(f"HTTP {resp.status_code} from DuckDuckGo")
        return _parse_html_results(resp.text, max_results)


__all__ = ["DuckDuckGoBackend"]
