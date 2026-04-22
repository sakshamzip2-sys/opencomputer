"""Exa.ai (formerly Metaphor) Search API backend.

Exa is neural-search-native, optimised for finding similar pages and
"high-quality" sources. Paid (no free tier as of writing). API:
  POST https://api.exa.ai/search
  Header: x-api-key: <key>
  JSON: {"query": "...", "numResults": N}

Get a key at https://exa.ai/.

Response shape:
  {"results": [{"title": ..., "url": ..., "text": ...?}, ...]}
Snippet field is variable — Exa supports `contents` requests for full
page text but the cheap default `search` returns just title + url.
"""

from __future__ import annotations

import os

import httpx

from opencomputer.tools.search_backends.base import (
    SearchBackend,
    SearchBackendError,
    SearchHit,
)

EXA_URL = "https://api.exa.ai/search"


class ExaBackend(SearchBackend):
    id = "exa"
    env_var = "EXA_API_KEY"
    signup_url = "https://exa.ai/"

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
            "numResults": max(1, min(max_results, 25)),
        }
        headers = {
            "Accept": "application/json",
            "x-api-key": api_key,
        }
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(EXA_URL, json=payload, headers=headers)
        if resp.status_code == 401:
            raise SearchBackendError("Exa: 401 unauthorized — check EXA_API_KEY")
        if resp.status_code == 429:
            raise SearchBackendError("Exa: 429 rate limit — wait or upgrade tier")
        if resp.status_code >= 400:
            raise SearchBackendError(f"Exa: HTTP {resp.status_code}")

        data = resp.json()
        results = data.get("results") or []
        hits: list[SearchHit] = []
        for r in results[:max_results]:
            title = str(r.get("title", "")).strip()
            url = str(r.get("url", "")).strip()
            # Exa's snippet field varies by query type; check both.
            snippet = str(r.get("text") or r.get("snippet") or "").strip()
            if not title or not url:
                continue
            hits.append(SearchHit(title=title, url=url, snippet=snippet))
        return hits


__all__ = ["ExaBackend"]
