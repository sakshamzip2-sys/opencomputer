"""AI Elements ``Sources`` port — contract tests.

Pins the ported component's API + extraction contract against the
TypeScript reference at https://github.com/vercel/ai-elements
(packages/elements/src/sources.tsx).

Tests cover:
1. **API parity** — Source dataclass field names match AI Elements'
   anchor props (``href``, ``title``); SourcesView accepts ``count``
   verbatim.
2. **Extraction** — ``ReasoningTurn.sources`` finds URLs in WebSearch
   markdown output AND WebFetch input.url with no new instrumentation.
3. **Render** — collapsed shows "Used N sources ›"; expanded shows
   each source as a row with title + URL.
4. **Wire-up** — when a turn has sources, the ReasoningView expanded
   body includes them as a subsection.
5. **Empty case** — turn with no web tools renders no Sources section
   (graceful degradation).
6. **Dedup** — same URL across multiple tool calls renders once.
"""
from __future__ import annotations

import io

from rich.console import Console

from opencomputer.cli_ui.reasoning_store import (
    ReasoningTurn,
    Source,
    ToolAction,
)
from opencomputer.cli_ui.reasoning_view import (
    ReasoningView,
    SourcesView,
)

# ─── 1. API parity ───────────────────────────────────────────────────


def test_source_dataclass_has_ai_elements_field_names() -> None:
    """``href`` + ``title`` mirror AI Elements' SourceProps anchor."""
    s = Source(href="https://example.com", title="Example")
    assert s.href == "https://example.com"
    assert s.title == "Example"
    # OC-only metadata (not in AI Elements but useful in terminal).
    assert s.tool is None
    assert s.snippet is None


def test_sources_view_accepts_count_prop() -> None:
    """SourcesTriggerProps.count mirror — explicit override."""
    sources = (Source(href="https://a", title="A"),)
    sv = SourcesView(sources=sources, count=42)
    assert sv.count == 42  # explicit override
    sv2 = SourcesView(sources=sources)
    assert sv2.count == 1  # defaults to len(sources)


def test_sources_view_open_default_collapsed() -> None:
    sv = SourcesView(sources=())
    assert sv.is_open is False


# ─── 2. Extraction from existing tool outputs ───────────────────────


def test_extract_sources_from_websearch_markdown() -> None:
    """ReasoningTurn.sources parses the WebSearch markdown listing
    (pattern: ``N. **Title**\\n   url``)."""
    output = (
        "# Results for 'rust async runtimes'\n"
        "\n"
        "1. **Tokio: An asynchronous runtime for Rust**\n"
        "   https://tokio.rs/\n"
        "\n"
        "2. **async-std**\n"
        "   https://async.rs/\n"
    )
    actions = (
        ToolAction(
            name="WebSearch",
            args_preview="rust async runtimes",
            ok=True,
            duration_s=0.3,
            output=output,
        ),
    )
    turn = ReasoningTurn(
        turn_id=1, thinking="", duration_s=0.3,
        tool_actions=actions, summary="x",
    )
    sources = turn.sources
    assert len(sources) == 2
    assert sources[0].href == "https://tokio.rs/"
    assert sources[0].title == "Tokio: An asynchronous runtime for Rust"
    assert sources[0].tool == "WebSearch"
    assert sources[1].href == "https://async.rs/"


def test_extract_source_from_webfetch_input_url() -> None:
    """ReasoningTurn.sources uses WebFetch's input.url (no markdown
    parsing — there's no structured title in a raw page fetch)."""
    actions = (
        ToolAction(
            name="WebFetch",
            args_preview="url=https://example.com/article",
            ok=True,
            duration_s=0.5,
            input={"url": "https://example.com/article"},
        ),
    )
    turn = ReasoningTurn(
        turn_id=1, thinking="", duration_s=0.5,
        tool_actions=actions, summary="x",
    )
    sources = turn.sources
    assert len(sources) == 1
    assert sources[0].href == "https://example.com/article"
    assert sources[0].title == "example.com/article"
    assert sources[0].tool == "WebFetch"


def test_extract_dedupes_same_url() -> None:
    """Same URL across WebSearch + WebFetch should render once
    (preserving first-seen order)."""
    actions = (
        ToolAction(
            name="WebSearch", args_preview="x", ok=True, duration_s=0.1,
            output="1. **Example**\n   https://example.com/page\n",
        ),
        ToolAction(
            name="WebFetch", args_preview="url=https://example.com/page",
            ok=True, duration_s=0.2,
            input={"url": "https://example.com/page"},
        ),
    )
    turn = ReasoningTurn(
        turn_id=1, thinking="", duration_s=0.3,
        tool_actions=actions, summary="x",
    )
    sources = turn.sources
    assert len(sources) == 1
    # First-seen wins → WebSearch's title preserved (not WebFetch's host).
    assert sources[0].title == "Example"
    assert sources[0].tool == "WebSearch"


def test_no_sources_when_no_web_tools() -> None:
    """Turn with only Read/Edit/Bash → empty sources tuple."""
    actions = (
        ToolAction(name="Read", args_preview="x", ok=True, duration_s=0.1),
        ToolAction(name="Edit", args_preview="y", ok=True, duration_s=0.1),
    )
    turn = ReasoningTurn(
        turn_id=1, thinking="", duration_s=0.0,
        tool_actions=actions, summary="x",
    )
    assert turn.sources == ()


# ─── 3. Render: collapsed vs expanded ───────────────────────────────


def _render(view) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    console.print(view)
    return buf.getvalue()


def test_collapsed_renders_used_n_sources_with_chevron_right() -> None:
    """Mirror of AI Elements' SourcesTrigger:
    ``<p>Used {count} sources</p>`` + ChevronDownIcon."""
    sources = (
        Source(href="https://a", title="A"),
        Source(href="https://b", title="B"),
        Source(href="https://c", title="C"),
    )
    sv = SourcesView(sources=sources, open=False)
    text = _render(sv)
    assert "Used 3 source" in text
    assert "›" in text


def test_collapsed_singular_when_one_source() -> None:
    """No plural 's' when count is 1."""
    sv = SourcesView(sources=(Source(href="https://a", title="A"),), open=False)
    text = _render(sv)
    assert "Used 1 source" in text
    assert "Used 1 sources" not in text


def test_expanded_renders_each_source_with_title_and_href() -> None:
    sources = (
        Source(href="https://tokio.rs/", title="Tokio", tool="WebSearch"),
        Source(href="https://async.rs/", title="async-std", tool="WebSearch"),
    )
    sv = SourcesView(sources=sources, open=True)
    text = _render(sv)
    assert "Tokio" in text
    assert "async-std" in text
    assert "tokio.rs" in text
    assert "async.rs" in text


# ─── 4. Wire-up: ReasoningView includes Sources subsection ───────────


def test_reasoning_view_expanded_includes_sources_subsection() -> None:
    """When a turn has sources, the ReasoningView expanded body must
    include them as a child subsection (mirrors AI Elements composition
    where <Sources> sits as a sibling under the Reasoning aggregate)."""
    actions = (
        ToolAction(
            name="WebSearch", args_preview="rust", ok=True, duration_s=0.3,
            output="1. **Rust Lang**\n   https://www.rust-lang.org/\n",
        ),
    )
    turn = ReasoningTurn(
        turn_id=1, thinking="searching for rust info", duration_s=0.3,
        tool_actions=actions, summary="Researched Rust",
    )
    rv = ReasoningView(turn=turn, open=True)
    text = _render(rv)
    assert "Used 1 source" in text
    assert "Rust Lang" in text
    assert "rust-lang.org" in text


def test_reasoning_view_no_sources_section_when_no_web_tools() -> None:
    """Turn with only Bash → no 'Used N sources' section in the
    expanded body."""
    actions = (
        ToolAction(name="Bash", args_preview="ls", ok=True, duration_s=0.1),
    )
    turn = ReasoningTurn(
        turn_id=1, thinking="", duration_s=0.0,
        tool_actions=actions, summary="x",
    )
    rv = ReasoningView(turn=turn, open=True)
    text = _render(rv)
    assert "Used" not in text or "source" not in text


# ─── 5. Empty / edge cases ──────────────────────────────────────────


def test_sources_view_with_empty_list_renders_no_sources() -> None:
    sv = SourcesView(sources=(), open=True)
    text = _render(sv)
    assert "Used 0 source" in text
    assert "(no sources)" in text


def test_websearch_with_no_matches_returns_empty_sources() -> None:
    """WebSearch output saying 'No results' must NOT extract any
    URLs."""
    actions = (
        ToolAction(
            name="WebSearch", args_preview="xyzqwe", ok=True, duration_s=0.1,
            output="No results for 'xyzqwe' via duckduckgo",
        ),
    )
    turn = ReasoningTurn(
        turn_id=1, thinking="", duration_s=0.1,
        tool_actions=actions, summary="x",
    )
    assert turn.sources == ()
