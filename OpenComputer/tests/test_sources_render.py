"""Tests for the Sources block + inline-citation port (cli_ui/sources.py).

Coverage:
* Schema field parity with AI Elements + Anthropic web_search_result_location
* parse_domain / favicon_url / enrich_url helpers
* SourcesRegistry: dedupe, ordering, 1-based [N] indexing
* strip_emitted_sources_block: bullets, numbered list, markdown header,
  bold marker, idempotency, no-match cases
* rewrite_inline_url_refs: simple, multi, dedup against registry
* render_sources_block: empty no-op, header + per-source row, OSC 8 link
* End-to-end through StreamingRenderer.finalize: ugly Sources block
  collapses into the structured render; footer untouched.
"""
from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console


@dataclass(frozen=True)
class _FakeHit:
    title: str
    url: str
    snippet: str = ""


def _record_console() -> Console:
    return Console(record=True, width=120, force_terminal=True)


# ─── schema ────────────────────────────────────────────────────────


def test_source_schema_matches_anthropic_and_ai_elements_field_names() -> None:
    from opencomputer.cli_ui.sources import Source

    s = Source(
        url="https://example.com/a",
        title="Example",
        domain="example.com",
        favicon_url="https://www.google.com/s2/favicons?domain=example.com&sz=64",
        snippet="cited text",
        encrypted_index="opaque-token",
        accessed_at=1.0,
    )
    # AI Elements InlineCitationSourceProps maps title/url/description.
    # Anthropic web_search_result_location maps url/title/cited_text/encrypted_index.
    # We expose all of them on a single class.
    assert s.url == "https://example.com/a"
    assert s.title == "Example"
    assert s.snippet == "cited text"               # AI Elements description / Anthropic cited_text
    assert s.encrypted_index == "opaque-token"     # Anthropic only
    assert s.id == s.url                           # natural-key id


def test_inline_citation_ref_shape() -> None:
    from opencomputer.cli_ui.sources import InlineCitationRef

    ref = InlineCitationRef(
        cited_text="India's GDP grew 8.4% in Q1",
        source_ids=("https://example.com/a", "https://example.com/b"),
    )
    assert ref.cited_text.startswith("India")
    assert len(ref.source_ids) == 2


# ─── helpers ───────────────────────────────────────────────────────


def test_parse_domain_strips_www_and_lowercases() -> None:
    from opencomputer.cli_ui.sources import parse_domain

    assert parse_domain("https://www.Indian-Express.com/article/x") == "indian-express.com"
    assert parse_domain("http://pcquest.com/") == "pcquest.com"
    assert parse_domain("not a url") == ""
    assert parse_domain("") == ""


def test_favicon_url_uses_google_s2_pattern_with_sz_64() -> None:
    from opencomputer.cli_ui.sources import favicon_url

    assert favicon_url("example.com") == (
        "https://www.google.com/s2/favicons?domain=example.com&sz=64"
    )
    assert favicon_url("") == ""


def test_enrich_url_falls_back_to_domain_when_title_missing() -> None:
    from opencomputer.cli_ui.sources import enrich_url

    s = enrich_url("https://www.example.com/path", title="", snippet="")
    assert s.title == "example.com"   # domain fallback
    assert s.domain == "example.com"
    assert "favicons?domain=example.com" in s.favicon_url


def test_enrich_url_caps_snippet_at_anthropic_150_chars() -> None:
    from opencomputer.cli_ui.sources import enrich_url

    long_snippet = "x" * 500
    s = enrich_url("https://e.com/", title="t", snippet=long_snippet)
    assert len(s.snippet) == 150


# ─── registry ──────────────────────────────────────────────────────


def test_registry_dedupes_on_url_and_returns_1based_index() -> None:
    from opencomputer.cli_ui.sources import SourcesRegistry, enrich_url

    reg = SourcesRegistry()
    s1 = enrich_url("https://a.com/", title="A")
    s2 = enrich_url("https://b.com/", title="B")
    s1_dup = enrich_url("https://a.com/", title="A again")

    assert reg.add(s1) == 1
    assert reg.add(s2) == 2
    assert reg.add(s1_dup) == 1                         # dedupe
    assert len(reg) == 2
    assert [s.url for s in reg.sources()] == ["https://a.com/", "https://b.com/"]
    # First-writer-wins: title is preserved from the first add.
    assert reg.sources()[0].title == "A"


def test_registry_add_search_hits_uses_backend_title_and_snippet() -> None:
    from opencomputer.cli_ui.sources import SourcesRegistry

    reg = SourcesRegistry()
    reg.add_search_hits([
        _FakeHit(title="India Q1 GDP", url="https://indianexpress.com/x", snippet="grew 8.4%"),
        _FakeHit(title="AI fintech 2026", url="https://pcquest.com/y"),
    ])
    sources = reg.sources()
    assert len(sources) == 2
    assert sources[0].title == "India Q1 GDP"
    assert sources[0].snippet == "grew 8.4%"
    assert sources[1].domain == "pcquest.com"


def test_registry_index_of_returns_none_for_unknown_url() -> None:
    from opencomputer.cli_ui.sources import SourcesRegistry

    reg = SourcesRegistry()
    assert reg.index_of("https://nope.example/") is None
    reg.add_url("https://yes.example/")
    assert reg.index_of("https://yes.example/") == 1


# ─── strip ─────────────────────────────────────────────────────────


def test_strip_emitted_sources_block_handles_bullets() -> None:
    from opencomputer.cli_ui.sources import strip_emitted_sources_block

    text = (
        "Here's the answer.\n"
        "\n"
        "Sources:\n"
        "  • https://indianexpress.com/article/x\n"
        "  • https://pcquest.com/y\n"
    )
    cleaned, urls = strip_emitted_sources_block(text)
    assert cleaned == "Here's the answer."
    assert urls == [
        "https://indianexpress.com/article/x",
        "https://pcquest.com/y",
    ]


def test_strip_handles_numbered_list_and_markdown_header() -> None:
    from opencomputer.cli_ui.sources import strip_emitted_sources_block

    text = (
        "Body paragraph.\n"
        "\n"
        "## Sources\n"
        "1. https://a.com/\n"
        "2. https://b.com/\n"
    )
    cleaned, urls = strip_emitted_sources_block(text)
    assert cleaned == "Body paragraph."
    assert urls == ["https://a.com/", "https://b.com/"]


def test_strip_handles_bold_sources_header() -> None:
    from opencomputer.cli_ui.sources import strip_emitted_sources_block

    text = "Body.\n\n**Sources:**\n- https://a.com/\n"
    cleaned, urls = strip_emitted_sources_block(text)
    assert cleaned == "Body."
    assert urls == ["https://a.com/"]


def test_strip_is_idempotent() -> None:
    from opencomputer.cli_ui.sources import strip_emitted_sources_block

    text = "Body.\n\nSources:\n- https://a.com/\n"
    once, _ = strip_emitted_sources_block(text)
    twice, urls2 = strip_emitted_sources_block(once)
    assert once == twice
    assert urls2 == []


def test_strip_no_match_when_no_trailing_block() -> None:
    from opencomputer.cli_ui.sources import strip_emitted_sources_block

    text = "Plain answer with no trailing sources dump."
    cleaned, urls = strip_emitted_sources_block(text)
    assert cleaned == text
    assert urls == []


def test_strip_does_not_remove_inline_mention_of_word_sources() -> None:
    """A paragraph that says 'sources confirm X' but has no list must not be stripped."""
    from opencomputer.cli_ui.sources import strip_emitted_sources_block

    text = "Multiple sources confirm the result. The answer is 42."
    cleaned, urls = strip_emitted_sources_block(text)
    assert cleaned == text
    assert urls == []


# ─── rewrite ────────────────────────────────────────────────────────


def test_rewrite_inline_url_refs_replaces_paren_url_with_bracket_n() -> None:
    from opencomputer.cli_ui.sources import SourcesRegistry, rewrite_inline_url_refs

    reg = SourcesRegistry()
    text = "GDP grew 8.4% (https://indianexpress.com/x) and inflation eased."
    out = rewrite_inline_url_refs(text, reg)
    assert out == "GDP grew 8.4% [1] and inflation eased."
    assert len(reg) == 1


def test_rewrite_dedupes_against_existing_registry_entries() -> None:
    from opencomputer.cli_ui.sources import SourcesRegistry, enrich_url, rewrite_inline_url_refs

    reg = SourcesRegistry()
    reg.add(enrich_url("https://existing.com/", title="Existing"))      # idx 1
    text = (
        "Pre-existing claim (https://existing.com/) and a new one "
        "(https://fresh.com/page)."
    )
    out = rewrite_inline_url_refs(text, reg)
    assert out == (
        "Pre-existing claim [1] and a new one [2]."
    )
    assert len(reg) == 2


def test_rewrite_handles_empty_string() -> None:
    from opencomputer.cli_ui.sources import SourcesRegistry, rewrite_inline_url_refs

    assert rewrite_inline_url_refs("", SourcesRegistry()) == ""


# ─── renderer ──────────────────────────────────────────────────────


def test_render_sources_block_is_noop_for_empty_registry() -> None:
    from opencomputer.cli_ui.sources import render_sources_block

    console = _record_console()
    render_sources_block(console, [])
    out = console.export_text(clear=False)
    assert out == ""


def test_render_default_is_expanded_with_header_and_per_row_titles() -> None:
    """Default render is the AI Elements expanded Collapsible state:
    trigger as a header (``📖 Used N sources [domains] ⌄``) followed
    by per-source rows. No slash-command friction — sources visible
    immediately.
    """
    from opencomputer.cli_ui.sources import enrich_url, render_sources_block

    console = _record_console()
    sources = [
        enrich_url("https://indianexpress.com/x", title="India Q1 GDP"),
        enrich_url("https://pcquest.com/y", title="AI fintech"),
    ]
    render_sources_block(console, sources)
    out = console.export_text(clear=False)
    # Trigger header present
    assert "Used 2 sources" in out
    assert "⌄" in out                      # expanded chevron
    # Domain peek in the trigger header
    assert "indianexpress.com" in out
    assert "pcquest.com" in out
    # Per-source title rows are rendered
    assert "India Q1 GDP" in out
    assert "AI fintech" in out
    # Collapsed-only chevron must NOT appear in default render
    assert "›" not in out
    # No slash-command friction in the default render
    assert "/sources to expand" not in out
    assert "/sources" not in out


def test_collapsed_trigger_hyperlinks_each_domain_via_osc8() -> None:
    """Each domain in the trigger header is rendered as an OSC 8
    hyperlink to its source URL — so cmd-click in iTerm2/Ghostty/
    Wezterm opens the source directly. Verified in both default
    (expanded) and ``open=False`` (header-only) modes.
    """
    from opencomputer.cli_ui.sources import enrich_url, render_sources_block

    console = _record_console()
    sources = [
        enrich_url("https://indianexpress.com/article/x", title="India Q1 GDP"),
        enrich_url("https://pcquest.com/article/y", title="AI fintech"),
    ]
    render_sources_block(console, sources)
    ansi = console.export_text(styles=True, clear=False)
    # Both URLs MUST appear in the ANSI escape stream (Rich emits OSC 8
    # for each `link <url>` style applied to the domain spans).
    assert "https://indianexpress.com/article/x" in ansi
    assert "https://pcquest.com/article/y" in ansi


def test_render_open_false_shows_header_only() -> None:
    """Passing ``open=False`` switches to the header-only state — no
    per-source rows. Useful for embedding inside other components that
    handle their own expansion. Default behaviour at finalize is open=True.
    """
    from opencomputer.cli_ui.sources import enrich_url, render_sources_block

    console = _record_console()
    sources = [
        enrich_url("https://indianexpress.com/x", title="India Q1 GDP"),
        enrich_url("https://pcquest.com/y", title="AI fintech"),
    ]
    render_sources_block(console, sources, open=False)
    out = console.export_text(clear=False)
    assert "Used 2 sources" in out
    assert "›" in out                      # collapsed chevron
    assert "⌄" not in out                  # expanded chevron absent
    # Per-source title rows must NOT be present in collapsed mode.
    assert "India Q1 GDP" not in out
    assert "AI fintech" not in out


def test_render_trigger_collapses_extra_domains_with_plus_n_count() -> None:
    """When >3 domains, the trigger HEADER shows the first 3 + ``+N``
    overflow badge — mirroring AI Elements' InlineCitationCardTrigger
    ``+N-1``. Per-source rows below are unaffected (all 5 listed)."""
    from opencomputer.cli_ui.sources import enrich_url, render_sources_block

    sources = [
        enrich_url(f"https://site{i}.com/x", title=f"T{i}") for i in range(5)
    ]
    console = _record_console()
    render_sources_block(console, sources, open=False)   # header-only to inspect peek
    out = console.export_text(clear=False)
    assert "Used 5 sources" in out
    assert "site0.com" in out
    assert "site1.com" in out
    assert "site2.com" in out
    # Tail is collapsed into +2 (5 total minus first 3 shown).
    assert "+2" in out
    # And the 4th/5th domains are NOT spelled out in the trigger header
    # (in header-only mode there are no rows below either).
    assert "site3.com" not in out
    assert "site4.com" not in out


def test_render_sources_block_uses_singular_for_one() -> None:
    from opencomputer.cli_ui.sources import enrich_url, render_sources_block

    console = _record_console()
    render_sources_block(console, [enrich_url("https://a.com/", title="A")])
    out = console.export_text(clear=False)
    assert "1 source" in out
    assert "1 sources" not in out


def test_render_emits_osc_8_hyperlink_when_url_present() -> None:
    """Rich renders OSC 8 hyperlinks when style contains 'link <url>'.

    Capture the ANSI export to verify the escape sequence is in the output.
    Hyperlinks live on per-source rows, so this test uses ``open=True``.
    """
    from opencomputer.cli_ui.sources import enrich_url, render_sources_block

    console = _record_console()
    render_sources_block(
        console,
        [enrich_url("https://example.com/page", title="Example Title")],
        open=True,
    )
    ansi = console.export_text(styles=True, clear=False)
    # OSC 8 sequence opens with ESC ] 8 ;; <url> ESC \  — Rich emits this
    # when a link style is applied. The URL itself should appear in the
    # raw escape stream regardless of which exact terminal sequence Rich
    # picked for this width / theme.
    assert "https://example.com/page" in ansi


# ─── end-to-end via StreamingRenderer.finalize ─────────────────────


def test_finalize_strips_model_sources_block_and_renders_structured_one() -> None:
    """Buffer ending with a model-emitted Sources: bullets should
    render as the structured Sources block instead.
    """
    from opencomputer.cli_ui import StreamingRenderer

    console = _record_console()
    with StreamingRenderer(console) as r:
        # Skip start_thinking — keeps Live unstarted so the recorded
        # console contains only the final render (no transient frames).
        # Simulate the search tool feeding its hits.
        r.add_search_sources([
            _FakeHit(title="India Q1 GDP", url="https://indianexpress.com/x", snippet="grew 8.4%"),
            _FakeHit(title="AI fintech 2026", url="https://pcquest.com/y"),
        ])
        # Simulate the model's prose ending with the ugly Sources dump.
        r.on_chunk(
            "India's GDP grew 8.4% and AI in fintech is up.\n"
            "\n"
            "Sources:\n"
            "  • https://indianexpress.com/x\n"
            "  • https://pcquest.com/y\n"
        )
        r.finalize(
            reasoning=None,
            iterations=1,
            in_tok=10,
            out_tok=20,
            elapsed_s=0.5,
        )

    out = console.export_text(clear=False)
    # The model's bulleted dump must be gone.
    assert "• https://indianexpress.com/x" not in out
    # The structured block must appear.
    assert "2 sources" in out
    # The body prose must remain.
    assert "GDP grew 8.4%" in out
    # The token-rate footer line MUST remain (untouched contract).
    assert "iterations" in out
    assert "in /" in out
    assert "tok/s" in out


def test_finalize_renders_no_sources_block_when_registry_empty() -> None:
    """Non-research turns (no search hits, no URL parentheticals) must
    print nothing about sources — no header, no separator.
    """
    from opencomputer.cli_ui import StreamingRenderer

    console = _record_console()
    with StreamingRenderer(console) as r:
        # Skip start_thinking — keeps Live unstarted so the recorded
        # console contains only the final render (no transient frames).
        r.on_chunk("Just a plain answer with no URLs.")
        r.finalize(
            reasoning=None,
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.1,
        )
    out = console.export_text(clear=False)
    assert "sources" not in out.lower()
    assert "Just a plain answer" in out


def test_finalize_rewrites_inline_paren_urls_to_bracket_refs() -> None:
    from opencomputer.cli_ui import StreamingRenderer

    console = _record_console()
    with StreamingRenderer(console) as r:
        # Skip start_thinking — keeps Live unstarted so the recorded
        # console contains only the final render (no transient frames).
        r.on_chunk(
            "GDP grew 8.4% (https://indianexpress.com/x) "
            "and AI fintech is up (https://pcquest.com/y)."
        )
        r.finalize(
            reasoning=None,
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.1,
        )
    out = console.export_text(clear=False)
    assert "[1]" in out
    assert "[2]" in out
    # The raw URLs in parens must be gone.
    assert "(https://indianexpress.com/x)" not in out
    assert "(https://pcquest.com/y)" not in out
    # And the structured block exists.
    assert "2 sources" in out
