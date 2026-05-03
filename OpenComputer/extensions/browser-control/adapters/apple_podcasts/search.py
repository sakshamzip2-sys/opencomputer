"""Adapter: apple_podcasts/search — Apple iTunes Search API for podcasts.

Pure PUBLIC. The iTunes Search API is free, unauthenticated, and returns
JSON for podcasts, music, books, etc. We pin the entity to ``podcast``.
"""

from __future__ import annotations

from urllib.parse import quote

from extensions.adapter_runner import Strategy, adapter


@adapter(
    site="apple_podcasts",
    name="search",
    description="Apple Podcasts search via iTunes Search API.",
    domain="itunes.apple.com",
    strategy=Strategy.PUBLIC,
    browser=False,
    args=[
        {"name": "term", "type": "string", "required": True, "help": "Search term"},
        {"name": "limit", "type": "int", "default": 10, "help": "Max results"},
        {"name": "country", "type": "string", "default": "us", "help": "ISO country code"},
    ],
    columns=["name", "artist", "genre", "url", "feed_url"],
)
async def run(args, ctx):
    term = (args.get("term") or "").strip()
    if not term:
        return []
    limit = max(1, min(50, int(args.get("limit") or 10)))
    country = (args.get("country") or "us").lower()
    url = (
        "https://itunes.apple.com/search?"
        f"term={quote(term)}&entity=podcast&limit={limit}&country={country}"
    )
    data = await ctx.fetch(url)
    if not isinstance(data, dict):
        return []
    rows: list[dict] = []
    for item in data.get("results", []):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "name": item.get("collectionName") or item.get("trackName", ""),
                "artist": item.get("artistName", ""),
                "genre": item.get("primaryGenreName", ""),
                "url": item.get("collectionViewUrl") or item.get("trackViewUrl", ""),
                "feed_url": item.get("feedUrl", ""),
            }
        )
    return rows
