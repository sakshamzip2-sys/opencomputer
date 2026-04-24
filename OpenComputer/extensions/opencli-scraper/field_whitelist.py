"""Per-adapter field whitelists for the 15 curated OpenCLI adapters.

Design §9: Unknown adapter → empty result + warning log. Fail-closed means
an implementer MUST add an explicit entry before any new adapter ships data.

PII note (§13.1): Whitelisting stops unknown fields but does NOT redact PII
within whitelisted fields (e.g. ``linkedin.author`` may contain a real name).
A per-field redactor is planned for Phase C2.5.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# ── Per-adapter field whitelists ───────────────────────────────────────────────

FIELD_WHITELISTS: dict[str, set[str]] = {
    "github/user": {"login", "name", "bio", "public_repos", "followers", "html_url"},
    "reddit/user": {"name", "karma", "created_utc"},
    "reddit/posts": {"id", "title", "url", "subreddit", "score", "created_utc"},
    "reddit/comments": {"id", "body", "subreddit", "score", "created_utc"},
    "linkedin/timeline": {"author", "text", "posted_at", "url"},
    "twitter/profile": {"username", "name", "bio", "followers_count", "following_count"},
    "twitter/tweets": {"id", "text", "created_at", "retweet_count", "favorite_count"},
    "hackernews/user": {"id", "karma", "created", "submitted_count"},
    "stackoverflow/user": {"display_name", "reputation", "answer_count", "question_count"},
    "youtube/user": {"channel_id", "title", "subscriber_count", "video_count"},
    "medium/user": {"username", "name", "follower_count", "post_count"},
    "bluesky/profile": {
        "handle",
        "displayName",
        "description",
        "followersCount",
        "followsCount",
    },
    "arxiv/search": {"id", "title", "authors", "summary", "published", "pdf_url"},
    "wikipedia/user-contributions": {
        "user",
        "title",
        "timestamp",
        "comment",
        "size_diff",
    },
    "producthunt/user": {"username", "name", "headline", "products_count"},
}


# ── Filtering ──────────────────────────────────────────────────────────────────


def filter_output(adapter: str, raw: dict | list) -> dict | list:
    """Filter *raw* to only include fields in ``FIELD_WHITELISTS[adapter]``.

    Parameters
    ----------
    adapter:
        The adapter slug, e.g. ``"github/user"``.
    raw:
        The raw output from the opencli subprocess — either a dict or a
        list of dicts.

    Returns
    -------
    dict | list
        Filtered output. Unknown adapter → empty dict / list + warning log.

    Raises
    ------
    TypeError
        If *raw* is neither a dict nor a list (programming error).
    """
    allowed = FIELD_WHITELISTS.get(adapter)

    if allowed is None:
        log.warning(
            "field_whitelist: unknown adapter %r — returning empty result. "
            "Add an explicit entry to FIELD_WHITELISTS before shipping this adapter.",
            adapter,
        )
        if isinstance(raw, list):
            return []
        if isinstance(raw, dict):
            return {}
        raise TypeError(
            f"filter_output: expected dict or list, got {type(raw).__name__!r}"
        )

    if isinstance(raw, list):
        return [_filter_dict(item, allowed, adapter) for item in raw]
    if isinstance(raw, dict):
        return _filter_dict(raw, allowed, adapter)
    raise TypeError(
        f"filter_output: expected dict or list for adapter {adapter!r}, "
        f"got {type(raw).__name__!r}"
    )


def _filter_dict(item: object, allowed: set[str], adapter: str) -> dict:
    """Return a copy of *item* with only *allowed* keys.

    If *item* is not a dict, logs a warning and returns an empty dict.
    """
    if not isinstance(item, dict):
        log.warning(
            "field_whitelist: expected dict in list for adapter %r, got %r — skipping",
            adapter,
            type(item).__name__,
        )
        return {}
    return {k: v for k, v in item.items() if k in allowed}


__all__ = ["FIELD_WHITELISTS", "filter_output"]
