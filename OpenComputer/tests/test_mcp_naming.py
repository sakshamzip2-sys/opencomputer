"""Gap D — MCP tool name sanitize + truncate + collision suffix.

mcp-openclaw-port follow-up. OC's bundle-MCP namespacing produces
``<plugin_id>__<server_name>__<tool_name>`` which can exceed the MCP
wire limit (64 chars by convention) on long IDs. This module owns:

* :func:`sanitize_mcp_tool_name` — strip unsafe chars, collapse runs.
* :func:`truncate_mcp_tool_name` — cap at 64 chars deterministically.
* :func:`compose_mcp_tool_name` — assemble server + tool with the
  ``__`` separator + sanitize + truncate + (when needed) append a
  numeric collision suffix until the name is unique in a given set.

Tests cover unsafe-char replacement, length cap, collision-suffix
escalation, plus the round-trip property: ``compose_mcp_tool_name``
always produces a name that survives MCP wire transport.
"""

from __future__ import annotations

import re

import pytest

from opencomputer.mcp.naming import (
    MAX_MCP_TOOL_NAME_LEN,
    MCP_TOOL_NAME_RE,
    compose_mcp_tool_name,
    sanitize_mcp_tool_name,
    truncate_mcp_tool_name,
)

# ─── sanitize ─────────────────────────────────────────────────────


def test_sanitize_passthrough_for_clean_names() -> None:
    assert sanitize_mcp_tool_name("memory") == "memory"
    assert sanitize_mcp_tool_name("read_file") == "read_file"
    assert sanitize_mcp_tool_name("read-file") == "read-file"


def test_sanitize_replaces_unsafe_chars_with_underscore() -> None:
    assert sanitize_mcp_tool_name("read file") == "read_file"
    assert sanitize_mcp_tool_name("read/file") == "read_file"
    assert sanitize_mcp_tool_name("a.b.c") == "a_b_c"
    assert sanitize_mcp_tool_name("a!@#b") == "a___b"


def test_sanitize_handles_unicode() -> None:
    # Non-ASCII letters get replaced
    assert sanitize_mcp_tool_name("café") == "caf_"
    assert sanitize_mcp_tool_name("日本") == "__"


def test_sanitize_empty_returns_underscore_unknown() -> None:
    assert sanitize_mcp_tool_name("") == "_unknown"
    assert sanitize_mcp_tool_name("...") == "___"


def test_sanitize_leading_digit_kept() -> None:
    """Digits OK anywhere — MCP wire doesn't disallow leading digits."""
    assert sanitize_mcp_tool_name("3d-render") == "3d-render"


def test_sanitize_post_condition_matches_regex() -> None:
    for raw in ("a!b", "memory", "café", "long" * 100, ""):
        out = sanitize_mcp_tool_name(raw)
        assert MCP_TOOL_NAME_RE.fullmatch(out), (
            f"sanitize_mcp_tool_name({raw!r}) → {out!r} violates regex"
        )


# ─── truncate ─────────────────────────────────────────────────────


def test_truncate_passthrough_for_short_names() -> None:
    assert truncate_mcp_tool_name("memory") == "memory"


def test_truncate_caps_at_max_len() -> None:
    long = "a" * 200
    out = truncate_mcp_tool_name(long)
    assert len(out) == MAX_MCP_TOOL_NAME_LEN
    assert out == "a" * MAX_MCP_TOOL_NAME_LEN


def test_truncate_max_len_is_64() -> None:
    assert MAX_MCP_TOOL_NAME_LEN == 64


def test_truncate_idempotent() -> None:
    truncated = truncate_mcp_tool_name("a" * 100)
    assert truncate_mcp_tool_name(truncated) == truncated


# ─── compose with collision suffix ───────────────────────────────


def test_compose_simple_no_collision() -> None:
    existing: set[str] = set()
    name = compose_mcp_tool_name("plug", "memory", "read", existing)
    assert name == "plug__memory__read"
    assert name in existing  # compose adds to existing


def test_compose_with_collision_appends_2() -> None:
    existing: set[str] = {"plug__memory__read"}
    name = compose_mcp_tool_name("plug", "memory", "read", existing)
    assert name == "plug__memory__read-2"
    assert "plug__memory__read-2" in existing


def test_compose_with_double_collision_appends_3() -> None:
    existing: set[str] = {"plug__memory__read", "plug__memory__read-2"}
    name = compose_mcp_tool_name("plug", "memory", "read", existing)
    assert name == "plug__memory__read-3"


def test_compose_truncates_for_long_components() -> None:
    long_plug = "a" * 50
    long_srv = "b" * 50
    long_tool = "c" * 50
    existing: set[str] = set()
    name = compose_mcp_tool_name(long_plug, long_srv, long_tool, existing)
    assert len(name) <= MAX_MCP_TOOL_NAME_LEN
    # The name must still be deterministic — same inputs produce same output
    existing2: set[str] = set()
    name2 = compose_mcp_tool_name(long_plug, long_srv, long_tool, existing2)
    assert name == name2


def test_compose_long_with_collision_still_fits_in_max() -> None:
    """Long base + collision suffix MUST still fit within 64 chars."""
    base = compose_mcp_tool_name("a" * 50, "b" * 50, "c" * 50, set())
    existing: set[str] = {base}
    name = compose_mcp_tool_name("a" * 50, "b" * 50, "c" * 50, existing)
    assert len(name) <= MAX_MCP_TOOL_NAME_LEN
    assert name != base
    assert re.search(r"-\d+$", name), (
        f"expected collision suffix on {name!r}"
    )


def test_compose_empty_plugin_uses_double_underscore_form() -> None:
    """When plugin_id is empty (user-configured MCP), the produced name
    is ``<server>__<tool>`` without the plugin prefix."""
    existing: set[str] = set()
    name = compose_mcp_tool_name("", "memory", "read", existing)
    assert name == "memory__read"


def test_compose_runs_out_of_collision_suffixes_raises() -> None:
    """When we've exhausted -2 through -99 we raise rather than overflow."""
    existing: set[str] = {"plug__memory__read"}
    for i in range(2, 100):
        existing.add(f"plug__memory__read-{i}")
    with pytest.raises(ValueError) as ei:
        compose_mcp_tool_name("plug", "memory", "read", existing)
    assert "collision" in str(ei.value).lower()


def test_compose_sanitizes_each_component() -> None:
    """Unsafe chars in any component → sanitized in the final name."""
    existing: set[str] = set()
    name = compose_mcp_tool_name("a.b", "c d", "e!f", existing)
    assert MCP_TOOL_NAME_RE.fullmatch(name)
    # Each unsafe char should have become _
    assert "." not in name
    assert " " not in name
    assert "!" not in name
