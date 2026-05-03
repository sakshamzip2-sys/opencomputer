"""Adapter: reddit/hot — hot posts in a subreddit (PUBLIC, no browser).

Reddit's ``/.json`` suffix on any subreddit URL returns a public JSON feed.
No auth required for browse-only flows. We send a User-Agent because
Reddit's edge sometimes 429s the default httpx UA.
"""

from __future__ import annotations

from extensions.adapter_runner import Strategy, adapter

_UA = "OpenComputer-Adapter/0.4 (https://github.com/sakshamzip2-sys/opencomputer)"


@adapter(
    site="reddit",
    name="hot",
    description="Reddit hot posts in a subreddit — title/score/comments/author.",
    domain="reddit.com",
    strategy=Strategy.PUBLIC,
    browser=False,
    args=[
        {"name": "subreddit", "type": "string", "default": "all", "help": "Subreddit (no /r/)"},
        {"name": "limit", "type": "int", "default": 25, "help": "Max posts"},
    ],
    columns=["rank", "title", "score", "author", "comments", "subreddit", "url"],
)
async def run(args, ctx):
    sub = (args.get("subreddit") or "all").strip().strip("/")
    if sub.startswith("r/"):
        sub = sub[2:]
    limit = max(1, min(100, int(args.get("limit") or 25)))
    url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
    data = await ctx.fetch(
        url,
        headers={"User-Agent": _UA, "Accept": "application/json"},
    )
    if not isinstance(data, dict):
        return []
    children = data.get("data", {}).get("children", [])
    if not isinstance(children, list):
        return []
    results: list[dict] = []
    for idx, child in enumerate(children, start=1):
        post = child.get("data") if isinstance(child, dict) else None
        if not isinstance(post, dict):
            continue
        results.append(
            {
                "rank": idx,
                "title": post.get("title", ""),
                "score": post.get("score", 0),
                "author": post.get("author", ""),
                "comments": post.get("num_comments", 0),
                "subreddit": post.get("subreddit", sub),
                "url": "https://www.reddit.com" + (post.get("permalink") or ""),
            }
        )
    return results
