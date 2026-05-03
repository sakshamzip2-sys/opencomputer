"""Adapter: hackernews/top — Hacker News top stories (PUBLIC, no browser).

Demonstrates the simplest tier — pure HTTP via httpx, no auth, no cookies.
The Firebase backing API is open and free.
"""

from __future__ import annotations

from extensions.adapter_runner import Strategy, adapter


@adapter(
    site="hackernews",
    name="top",
    description="Hacker News top stories — rank/title/score/author/comments.",
    domain="news.ycombinator.com",
    strategy=Strategy.PUBLIC,
    browser=False,
    args=[
        {"name": "limit", "type": "int", "default": 20, "help": "Number of stories"},
    ],
    columns=["rank", "title", "score", "author", "comments", "url"],
)
async def run(args, ctx):
    limit = max(1, int(args.get("limit") or 20))
    ids = await ctx.fetch("https://hacker-news.firebaseio.com/v0/topstories.json")
    if not isinstance(ids, list):
        return []
    # Fetch a few extra IDs to compensate for deleted/dead items.
    ids = ids[: min(limit + 10, 50)]

    results: list[dict] = []
    for idx, item_id in enumerate(ids):
        item = await ctx.fetch(
            f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
        )
        if not isinstance(item, dict):
            continue
        if item.get("deleted") or item.get("dead") or not item.get("title"):
            continue
        results.append(
            {
                "rank": idx + 1,
                "title": item.get("title", ""),
                "score": item.get("score", 0),
                "author": item.get("by", ""),
                "comments": item.get("descendants", 0),
                "url": item.get("url") or f"https://news.ycombinator.com/item?id={item_id}",
            }
        )
        if len(results) >= limit:
            break
    return results
