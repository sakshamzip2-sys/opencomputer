"""Tavily Search API backend.

Tavily is LLM-native (built for RAG / agent use). Free tier: 1000
queries/month. API:
  POST https://api.tavily.com/search
  JSON: {"api_key": "...", "query": "...", "max_results": N}

Get a key at https://app.tavily.com/.

Response shape:
  {"results": [{"title": ..., "url": ..., "content": ...}, ...]}
"""

from __future__ import annotations

import os

import httpx

from opencomputer.tools.search_backends.base import (
    SearchBackend,
    SearchBackendError,
    SearchHit,
)

TAVILY_URL = "https://api.tavily.com/search"


class TavilyBackend(SearchBackend):
    id = "tavily"
    env_var = "TAVILY_API_KEY"
    signup_url = "https://app.tavily.com/"

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
        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": max(1, min(max_results, 20)),
        }
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(TAVILY_URL, json=payload)
        if resp.status_code == 401:
            raise SearchBackendError("Tavily: 401 unauthorized — check TAVILY_API_KEY")
        if resp.status_code == 429:
            raise SearchBackendError("Tavily: 429 rate limit — wait or upgrade tier")
        if resp.status_code >= 400:
            raise SearchBackendError(f"Tavily: HTTP {resp.status_code}")

        data = resp.json()
        results = data.get("results") or []
        hits: list[SearchHit] = []
        for r in results[:max_results]:
            title = str(r.get("title", "")).strip()
            url = str(r.get("url", "")).strip()
            snippet = str(r.get("content", "")).strip()
            if not title or not url:
                continue
            hits.append(SearchHit(title=title, url=url, snippet=snippet))
        return hits


__all__ = ["TavilyBackend"]
