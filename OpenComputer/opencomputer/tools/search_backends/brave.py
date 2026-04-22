"""Brave Search API backend.

Free tier: 2000 queries/month with attribution. API:
  GET https://api.search.brave.com/res/v1/web/search?q=...
  Header: X-Subscription-Token: <key>

Get a key at https://api.search.brave.com/app/keys.

Response shape:
  {"web": {"results": [{"title": ..., "url": ..., "description": ...}, ...]}}
"""

from __future__ import annotations

import os

import httpx

from opencomputer.tools.search_backends.base import (
    SearchBackend,
    SearchBackendError,
    SearchHit,
)

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


class BraveBackend(SearchBackend):
    id = "brave"
    env_var = "BRAVE_API_KEY"
    signup_url = "https://api.search.brave.com/app/keys"

    async def search(
        self,
        *,
        query: str,
        max_results: int,
        timeout_s: float,
    ) -> list[SearchHit]:
        api_key = os.environ.get(self.env_var, "").strip()
        if not api_key:
            raise SearchBackendError(
                f"{self.env_var} not set. Get a free key at {self.signup_url}."
            )
        params = {"q": query, "count": max(1, min(max_results, 20))}
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        }
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(BRAVE_URL, params=params, headers=headers)
        if resp.status_code == 401:
            raise SearchBackendError("Brave: 401 unauthorized — check BRAVE_API_KEY")
        if resp.status_code == 429:
            raise SearchBackendError("Brave: 429 rate limit — wait or upgrade tier")
        if resp.status_code >= 400:
            raise SearchBackendError(f"Brave: HTTP {resp.status_code}")

        data = resp.json()
        web_block = (data.get("web") or {}).get("results") or []
        hits: list[SearchHit] = []
        for r in web_block[:max_results]:
            title = str(r.get("title", "")).strip()
            url = str(r.get("url", "")).strip()
            snippet = str(r.get("description", "")).strip()
            if not title or not url:
                continue
            hits.append(SearchHit(title=title, url=url, snippet=snippet))
        return hits


__all__ = ["BraveBackend"]
