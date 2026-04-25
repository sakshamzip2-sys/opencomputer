"""User-mention-triggered profile enrichment.

Detects ``@<handle> on <platform>`` patterns in user text, fetches the
corresponding profiles via the FetchProfileTool flow, and renders the
enriched data as a markdown block suitable for injection into the agent's
system prompt.

The 15-platform shortlist mirrors ``_PLATFORM_ADAPTER`` in
``extensions/opencli-scraper/tools.py``:
    github, reddit, linkedin, twitter, hackernews, hn, stackoverflow, so,
    youtube, medium, bluesky, arxiv, wikipedia, producthunt, ph
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import field_whitelist  # type: ignore[import-not-found]  # noqa: E402
from wrapper import OpenCLIWrapper  # type: ignore[import-not-found]  # noqa: E402

log = logging.getLogger(__name__)

# Matches "@<handle> on <platform>" — case-insensitive, handle = word chars.
MENTION_PATTERN = re.compile(r"@(\w+)\s+on\s+(\w+)", re.IGNORECASE)

# Supported platforms — matches keys in tools._PLATFORM_ADAPTER.
_SUPPORTED_PLATFORMS: frozenset[str] = frozenset(
    [
        "github",
        "reddit",
        "linkedin",
        "twitter",
        "hackernews",
        "hn",
        "stackoverflow",
        "so",
        "youtube",
        "medium",
        "bluesky",
        "arxiv",
        "wikipedia",
        "producthunt",
        "ph",
    ]
)

# Map platform → adapter slug (mirrors tools._PLATFORM_ADAPTER).
_PLATFORM_ADAPTER: dict[str, str] = {
    "github": "github/user",
    "reddit": "reddit/user",
    "linkedin": "linkedin/timeline",
    "twitter": "twitter/profile",
    "hackernews": "hackernews/user",
    "hn": "hackernews/user",
    "stackoverflow": "stackoverflow/user",
    "so": "stackoverflow/user",
    "youtube": "youtube/user",
    "medium": "medium/user",
    "bluesky": "bluesky/profile",
    "arxiv": "arxiv/search",
    "wikipedia": "wikipedia/user-contributions",
    "producthunt": "producthunt/user",
    "ph": "producthunt/user",
}


def extract_mentions(text: str) -> list[tuple[str, str]]:
    """Extract ``(handle, platform)`` pairs from *text*.

    Parses all ``@<handle> on <platform>`` occurrences. Platform names are
    normalised to lowercase.

    Parameters
    ----------
    text:
        Any free-form text that may contain user mentions.

    Returns
    -------
    list[tuple[str, str]]
        Ordered list of ``(handle, platform)`` tuples. Duplicates are
        preserved (deduplication is caller's responsibility).

    Examples
    --------
    >>> extract_mentions("Tell me about @octocat on GitHub")
    [('octocat', 'github')]
    """
    return [(handle, platform.lower()) for handle, platform in MENTION_PATTERN.findall(text)]


async def enrich_mentions(
    wrapper: OpenCLIWrapper,
    text: str,
    max_fetches: int = 3,
) -> dict[tuple[str, str], dict]:
    """Fetch profile data for every mention detected in *text*.

    Parameters
    ----------
    wrapper:
        An ``OpenCLIWrapper`` instance.
    text:
        User-provided text to scan for mentions.
    max_fetches:
        Hard cap on the number of profile fetches. Mentions beyond this limit
        are silently skipped. Default ``3``.

    Returns
    -------
    dict[tuple[str, str], dict]
        Mapping of ``(handle, platform)`` → filtered profile dict (or an
        error dict ``{"error": str}`` when the fetch fails).
    """
    mentions = extract_mentions(text)
    result: dict[tuple[str, str], dict] = {}
    fetched = 0

    seen: set[tuple[str, str]] = set()
    for handle, platform in mentions:
        key = (handle, platform)
        if key in seen:
            continue
        seen.add(key)

        if fetched >= max_fetches:
            log.debug(
                "enrich_mentions: max_fetches=%d reached, skipping %r on %r",
                max_fetches,
                handle,
                platform,
            )
            break

        if platform not in _SUPPORTED_PLATFORMS:
            log.debug("enrich_mentions: unsupported platform %r — skipping", platform)
            result[key] = {"error": f"Platform {platform!r} is not in the supported shortlist"}
            continue

        adapter = _PLATFORM_ADAPTER[platform]
        try:
            raw = await wrapper.run(adapter, handle)
            data = raw.get("data", raw) if isinstance(raw, dict) else raw
            allowed = field_whitelist.FIELD_WHITELISTS.get(adapter, set())
            if isinstance(data, dict):
                filtered = {k: v for k, v in data.items() if k in allowed}
            elif isinstance(data, list):
                filtered = {
                    "results": [{k: v for k, v in item.items() if k in allowed} for item in data if isinstance(item, dict)]
                }
            else:
                filtered = {}
            result[key] = filtered
        except Exception as exc:
            log.warning("enrich_mentions: failed for %r on %r — %s", handle, platform, exc)
            result[key] = {"error": str(exc)}

        fetched += 1

    return result


def format_for_context(enriched: dict[tuple[str, str], dict]) -> str:
    """Render enriched profile data as a markdown block.

    Parameters
    ----------
    enriched:
        Output of :func:`enrich_mentions` — mapping of
        ``(handle, platform)`` → profile dict.

    Returns
    -------
    str
        Markdown-formatted string suitable for injection into a system prompt.
        Each profile appears as a level-3 heading followed by key-value pairs.
    """
    if not enriched:
        return "<!-- No enriched context -->"

    lines: list[str] = ["## Enriched User Profiles\n"]
    for (handle, platform), profile in enriched.items():
        lines.append(f"### @{handle} on {platform.capitalize()}\n")
        if "error" in profile:
            lines.append(f"- _Error_: {profile['error']}\n")
        else:
            for key, value in profile.items():
                lines.append(f"- **{key}**: {value}\n")
        lines.append("")

    return "\n".join(lines).rstrip()


__all__ = [
    "MENTION_PATTERN",
    "extract_mentions",
    "enrich_mentions",
    "format_for_context",
]
