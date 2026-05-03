"""Sources block + inline-citation rendering for the chat TUI.

Port of Vercel AI Elements' ``sources.tsx`` and ``inline-citation.tsx``
adapted to a terminal medium. Field names mirror Anthropic's
``web_search_result_location`` (url, title, cited_text, encrypted_index)
so that when a hosted-search provider returns server-side citations we
consume them verbatim.

The renderer is invoked from ``StreamingRenderer.finalize`` after the
markdown body is printed but before the token-rate footer. Empty
registry → renders nothing.

Reference (verbatim API quoted in audit):
    Sources:        Collapsible {className: "not-prose mb-4 text-primary text-xs"}
    SourcesTrigger: count: number; "Used {count} sources" + chevron
    SourcesContent: collapsible body
    Source:         <a href title> {favicon} {title}
    InlineCitationCardTrigger: Badge sources: string[]; shows
                               new URL(sources[0]).hostname [+N-1]

What we keep:
    * Field names: url, title, snippet (== Anthropic cited_text), domain,
      favicon_url, encrypted_index, accessed_at.
    * Count-in-header layout (``Used N sources``).
    * Hostname-first row layout (matches InlineCitationCardTrigger).

What we adapt for terminal:
    * No <a>/<img>; use Rich OSC 8 hyperlinks on the title cell.
    * Always-expanded list (no collapse — terminal has no fold state
      across turns; the next prompt scrolls the block away regardless).
    * Favicon stored as URL only; ``·`` glyph stands in visually.
"""
from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

from rich.console import Console
from rich.text import Text

# ─────────────────────────── schema ─────────────────────────────────


@dataclass(frozen=True, slots=True)
class Source:
    """One web source.

    Field names mirror two contracts simultaneously:

    1. **Vercel AI Elements** ``InlineCitationSourceProps`` (title, url,
       description). We use ``snippet`` for the description slot since
       it matches Anthropic's wire name.
    2. **Anthropic web search** ``web_search_result_location`` block:
       url, title, cited_text (snippet), encrypted_index.

    ``id`` is the URL itself — stable, human-readable, ideal for [N]
    resolution. We dedupe on ``url`` in :class:`SourcesRegistry`.
    """

    url: str
    title: str
    domain: str
    favicon_url: str
    snippet: str = ""                       # Anthropic: cited_text (≤150 chars)
    encrypted_index: str | None = None      # Anthropic-only opaque token
    accessed_at: float = 0.0                # epoch seconds; 0 = unknown

    @property
    def id(self) -> str:
        """Stable id for [N]-style resolution. URL is the natural key."""
        return self.url


@dataclass(frozen=True, slots=True)
class InlineCitationRef:
    """Maps a span of answer prose to one or more :class:`Source` ids.

    Mirrors Anthropic's ``web_search_result_location`` attachment shape:
    an inline citation node that points back at the search result(s)
    it summarises.

    ``cited_text`` is the prose span being attributed (not the source
    snippet — those live on :class:`Source`). ``source_ids`` are URLs.
    """

    cited_text: str
    source_ids: tuple[str, ...]


# ─────────────────────────── helpers ────────────────────────────────


def parse_domain(url: str) -> str:
    """Return the registrable hostname from a URL, or '' on failure.

    Strips ``www.`` so ``www.example.com`` → ``example.com`` (matches the
    way AI Elements renders ``new URL(sources[0]).hostname`` after the
    common UI convention of dropping the ``www.`` prefix).
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, TypeError):
        return ""
    return host[4:] if host.startswith("www.") else host


def favicon_url(domain: str) -> str:
    """Default Google s2 favicon URL pattern (sz=64, matches reference).

    Returns '' if domain is empty so downstream renderers can fall back
    to a glyph instead of fetching a 1×1 placeholder.
    """
    if not domain:
        return ""
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=64"


def enrich_url(
    url: str,
    *,
    title: str = "",
    snippet: str = "",
    encrypted_index: str | None = None,
    accessed_at: float | None = None,
) -> Source:
    """Convert a raw URL (+ optional metadata) into an enriched :class:`Source`.

    Title and snippet are taken from the search backend response when
    available — search providers already return both, so no extra fetch
    is needed during the response render. If ``title`` is empty, the
    domain is used as the visual fallback.
    """
    domain = parse_domain(url)
    return Source(
        url=url,
        title=title or domain or url,
        domain=domain,
        favicon_url=favicon_url(domain),
        snippet=snippet[:150] if snippet else "",   # Anthropic cap (≤150)
        encrypted_index=encrypted_index,
        accessed_at=accessed_at if accessed_at is not None else time.time(),
    )


# ─────────────────────────── registry ───────────────────────────────


class _HitLike(Protocol):
    """Duck-typed search hit (matches ``SearchHit`` in tools/search_backends).

    We take a Protocol instead of importing SearchHit to avoid coupling
    cli_ui to the tools package.
    """
    title: str
    url: str
    snippet: str


@dataclass
class SourcesRegistry:
    """Per-turn source accumulator. Dedupes on URL; preserves insertion order.

    Sources land here from two paths:
    * search-tool callbacks (deterministic — backend-supplied title +
      snippet; favicon synthesised from domain),
    * fallback URL extraction from the model's prose (no title; domain
      stands in).
    """

    _by_url: dict[str, Source] = field(default_factory=dict)

    def add(self, src: Source) -> int:
        """Add (or merge) a source. Returns its 1-based index for [N] refs.

        Existing sources are NOT overwritten — first writer wins, since
        the search-tool path has richer data than the prose-fallback path.
        """
        if src.url not in self._by_url:
            self._by_url[src.url] = src
        return list(self._by_url).index(src.url) + 1

    def add_search_hits(self, hits: Iterable[_HitLike]) -> None:
        """Bulk-add hits from a search backend."""
        for h in hits:
            self.add(
                enrich_url(h.url, title=h.title, snippet=h.snippet)
            )

    def add_url(self, url: str) -> int:
        """Add a bare URL (no title/snippet) and return its 1-based index."""
        return self.add(enrich_url(url))

    def index_of(self, url: str) -> int | None:
        """1-based index for an existing URL, or None if not registered."""
        if url not in self._by_url:
            return None
        return list(self._by_url).index(url) + 1

    def sources(self) -> list[Source]:
        """All sources in insertion order."""
        return list(self._by_url.values())

    def __len__(self) -> int:
        return len(self._by_url)


# ─────────────────────────── prose post-processing ──────────────────


# A trailing block the model commonly appends:
#
#     <blank line>
#     Sources:                       (or **Sources:** or ## Sources)
#     - https://...
#     • https://...
#     1. https://...
#
# Match defensively — if there's no clear list of URLs after the header
# we leave the text alone (could be the model talking about sources in
# prose, not dumping them).
_EMITTED_SOURCES_HEADER_RE = re.compile(
    r"""
    \n[ \t]*\n            # required blank-line gap before the block
    [ \t]*
    (?:\#{1,6}[ \t]+)?    # optional markdown header chars
    (?:\*{1,3})?          # optional opening bold/italic markers
    Sources?              # the literal "Source" or "Sources"
    [ \t]*:?[ \t]*        # optional colon (may sit inside bold)
    (?:\*{1,3})?          # optional closing bold/italic markers
    [ \t]*\n              # end of the header line
    (?P<body>(?:[ \t]*(?:[-*•]|\d+\.)[ \t]+\S.*\n?)+)   # ≥1 list item
    \s*$                  # block must run to EOF
    """,
    re.VERBOSE | re.IGNORECASE,
)


_URL_RE = re.compile(r"https?://[^\s\)\]\>\<\"']+", re.IGNORECASE)


def strip_emitted_sources_block(text: str) -> tuple[str, list[str]]:
    """Strip a trailing ``Sources:\\n  • ...`` block from model output.

    Returns ``(cleaned_text, urls_found_in_stripped_block)``. The URL
    list lets the caller register any URLs the model dumped that weren't
    already captured from search-tool callbacks.

    Idempotent — running twice yields the same result and an empty URL
    list on the second pass.
    """
    if not text:
        return text, []

    m = _EMITTED_SOURCES_HEADER_RE.search(text)
    if not m:
        return text, []

    body = m.group("body")
    urls = _URL_RE.findall(body)
    cleaned = text[: m.start()].rstrip()
    return cleaned, urls


# Inline ``(https://example.com/...)`` parentheticals — common model
# style for inline attribution. We rewrite to ``[N]``.
_INLINE_PAREN_URL_RE = re.compile(
    r"""
    \(                                    # opening paren
    \s*
    (?P<url>https?://[^\s\)]+)            # the URL
    \s*
    \)                                    # closing paren
    """,
    re.VERBOSE | re.IGNORECASE,
)


def rewrite_inline_url_refs(text: str, registry: SourcesRegistry) -> str:
    """Replace ``(https://...)`` parentheticals with ``[N]`` references.

    URLs are auto-registered into ``registry`` so the resulting [N]
    matches the rendered Sources block. Order is stable across runs of
    the same input.
    """
    if not text:
        return text

    def _sub(m: re.Match[str]) -> str:
        url = m.group("url")
        n = registry.add_url(url)
        return f"[{n}]"

    return _INLINE_PAREN_URL_RE.sub(_sub, text)


# ─────────────────────────── renderer ───────────────────────────────


#: Cap on how many domains we list inside the collapsed trigger badge.
#: Mirrors AI Elements' ``InlineCitationCardTrigger`` which shows
#: ``hostname [+N-1]`` — i.e. the first source's hostname plus a count
#: of the rest. We show up to 3 domains for terminal legibility before
#: collapsing the tail into ``+N-3``.
_TRIGGER_DOMAIN_PEEK = 3


def _render_trigger(sources: list[Source], *, is_open: bool) -> Text:
    """Port of <SourcesTrigger> — ``📖 Used N sources [domains] ⌄/›``.

    Acts as a header above the per-source list when ``is_open`` (the
    default — see render_sources_block). Each domain in the peek is
    OSC 8 hyperlinked to its source URL so cmd-click opens the source
    in the browser (iTerm2 / Ghostty / Wezterm / GNOME Terminal /
    modern Konsole). Terminals that don't support OSC 8 silently
    render plain text; no degradation.
    """
    n = len(sources)
    trigger = Text()
    trigger.append("📖 ", style="dim")
    trigger.append(
        f"Used {n} source{'' if n == 1 else 's'}", style="bold dim cyan"
    )

    # Pair each domain with its source URL for OSC 8 hyperlinks.
    peek_pairs: list[tuple[str, str]] = []
    for s in sources:
        if s.domain:
            peek_pairs.append((s.domain, s.url))
        if len(peek_pairs) >= _TRIGGER_DOMAIN_PEEK:
            break
    rest = sum(1 for s in sources if s.domain) - len(peek_pairs)

    if peek_pairs:
        trigger.append("  [", style="dim")
        for i, (domain, url) in enumerate(peek_pairs):
            if i > 0:
                trigger.append(", ", style="dim")
            # OSC 8 hyperlink — cmd-click opens the URL in the browser.
            trigger.append(domain, style=f"dim cyan link {url}" if url else "dim cyan")
        if rest > 0:
            trigger.append(f" +{rest}", style="dim")
        trigger.append("]", style="dim")

    trigger.append("  ", style="")
    trigger.append("⌄" if is_open else "›", style="dim")
    return trigger


def _render_expanded_list(sources: list[Source]) -> list[Text]:
    """Per-source rows for the expanded state (one Text per source)."""
    rows: list[Text] = []
    for i, s in enumerate(sources, 1):
        row = Text()
        row.append(f" {i:>2} ", style="dim")
        row.append(s.domain or s.url, style="dim cyan")
        row.append("  ·  ", style="dim")
        if s.url:
            row.append(s.title, style=f"link {s.url}")
        else:
            row.append(s.title)
        rows.append(row)
    return rows


def render_sources_block(
    console: Console,
    sources: list[Source],
    *,
    open: bool = True,                    # noqa: A002 — mirror AI Elements
) -> None:
    """Render the Sources block to the console.

    **Default is expanded** (``open=True``) — the structured per-source
    list is the value we're delivering, so we show it without making
    the user reach for a slash command. Trigger sits above as a
    header:

        📖 Used 3 sources  [indianexpress.com, pcquest.com, reuters.com]  ⌄
         1 indianexpress.com  ·  India's Q1 GDP up 8.4%
         2 pcquest.com        ·  AI in Indian fintech 2026 review
         3 reuters.com        ·  RBI policy ahead

    Pass ``open=False`` for the collapsed-only state (header alone, no
    rows) — useful from inside other components that handle their own
    expansion / deferred render. The default behaviour at finalize
    always renders expanded.

    Click-to-toggle the chevron isn't wired — Rich's ``console.print``
    produces immutable scrollback once Live has stopped, so retroactive
    re-render of a past turn (e.g. one that scrolled off-screen) goes
    through the ``/sources`` slash command, mirroring how the reasoning
    card uses ``/reasoning show``. Empty list → no-op.

    Each domain in the trigger header carries an OSC 8 hyperlink to its
    source URL so cmd-click opens it directly in the browser. Each row
    title also OSC 8 hyperlinks to its URL.
    """
    if not sources:
        return

    console.print(_render_trigger(sources, is_open=open))
    if open:
        for row in _render_expanded_list(sources):
            console.print(row)
    console.print("")  # trailing blank line so the footer breathes


__all__ = [
    "InlineCitationRef",
    "Source",
    "SourcesRegistry",
    "enrich_url",
    "favicon_url",
    "parse_domain",
    "render_sources_block",
    "rewrite_inline_url_refs",
    "strip_emitted_sources_block",
]
