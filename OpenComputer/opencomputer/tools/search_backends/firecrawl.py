"""Firecrawl Search API backend.

Firecrawl bundles search + scrape — `/v1/search` returns hits, optionally
with rendered page content. Free tier exists. API:
  POST https://api.firecrawl.dev/v1/search
  Header: Authorization: Bearer <key>
  JSON: {"query": "...", "limit": N}

Get a key at https://www.firecrawl.dev/.

Response shape:
  {"data": [{"title": ..., "url": ..., "description": ...}, ...]}
"""

from __future__ import annotations

import os

import httpx

from opencomputer.tools.search_backends.base import (
    SearchBackend,
    SearchBackendError,
    SearchHit,
)

FIRECRAWL_URL = "https://api.firecrawl.dev/v1/search"


class FirecrawlBackend(SearchBackend):
    id = "firecrawl"
    env_var = "FIRECRAWL_API_KEY"
    signup_url = "https://www.firecrawl.dev/"

    async def search(
        self,
        *,
        query: str,
        max_results: int,
        timeout_s: float,
    ) -> list[SearchHit]:
        api_key = os.environ.get(self.env_var, "").strip()
        if not api_key:
            raise SearchBackendError(f"{self.env_var} not set. Get a key at {self.signup_url}.")
        payload = {
            "query": query,
            "limit": max(1, min(max_results, 20)),
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(FIRECRAWL_URL, json=payload, headers=headers)
        if resp.status_code == 401:
            raise SearchBackendError("Firecrawl: 401 unauthorized — check FIRECRAWL_API_KEY")
        if resp.status_code == 429:
            raise SearchBackendError("Firecrawl: 429 rate limit — wait or upgrade tier")
        if resp.status_code >= 400:
            raise SearchBackendError(f"Firecrawl: HTTP {resp.status_code}")

        data = resp.json()
        # Firecrawl wraps the array under either "data" or "results" depending
        # on plan/version — accept both.
        results = data.get("data") or data.get("results") or []
        hits: list[SearchHit] = []
        for r in results[:max_results]:
            title = str(r.get("title", "")).strip()
            url = str(r.get("url", "")).strip()
            snippet = str(r.get("description") or r.get("snippet") or "").strip()
            if not title or not url:
                continue
            hits.append(SearchHit(title=title, url=url, snippet=snippet))
        return hits


__all__ = ["FirecrawlBackend"]
