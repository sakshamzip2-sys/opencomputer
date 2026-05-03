"""Adapter: github/notifications — your GitHub notifications.

Demonstrates header-token auth via env var. Strategy.COOKIE per BLUEPRINT
§2 (header tokens are a sub-case of COOKIE, not a separate Strategy).
The token comes from ``GITHUB_TOKEN`` (a fine-grained personal access
token with ``notifications:read`` scope).
"""

from __future__ import annotations

import os

from extensions.adapter_runner import Strategy, adapter


@adapter(
    site="github",
    name="notifications",
    description=(
        "Fetch GitHub notifications for the authenticated user via REST API. "
        "Requires GITHUB_TOKEN env var with notifications scope. Returns repo, "
        "subject, reason, updated_at. Use for inbox triage; prefer over WebFetch."
    ),
    domain="github.com",
    strategy=Strategy.COOKIE,
    browser=False,
    args=[
        {"name": "all", "type": "bool", "default": False, "help": "Include read notifications"},
        {"name": "limit", "type": "int", "default": 30, "help": "Max items"},
    ],
    columns=["repo", "type", "title", "reason", "updated_at", "url"],
)
async def run(args, ctx):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        from extensions.browser_control._utils.errors import (  # type: ignore[import-not-found]
            AuthRequiredError,
        )

        raise AuthRequiredError(
            "set GITHUB_TOKEN env var (fine-grained PAT with notifications:read)"
        )
    include_all = bool(args.get("all"))
    limit = max(1, int(args.get("limit") or 30))
    url = f"https://api.github.com/notifications?per_page={limit}"
    if include_all:
        url += "&all=true"
    data = await ctx.fetch(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if not isinstance(data, list):
        return []
    rows: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        repo = item.get("repository", {}).get("full_name", "")
        subject = item.get("subject", {})
        rows.append(
            {
                "repo": repo,
                "type": subject.get("type", ""),
                "title": subject.get("title", ""),
                "reason": item.get("reason", ""),
                "updated_at": item.get("updated_at", ""),
                "url": subject.get("url") or item.get("url", ""),
            }
        )
    return rows
