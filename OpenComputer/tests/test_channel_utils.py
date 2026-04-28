"""Tests for plugin_sdk.channel_utils."""

from __future__ import annotations

import pytest

from plugin_sdk.channel_utils import (
    SUPPORTED_DOCUMENT_TYPES,
    SUPPORTED_VIDEO_TYPES,
    _prefix_within_utf16_limit,
    truncate_message_smart,
    utf16_len,
)
from plugin_sdk.core import ProcessingOutcome


@pytest.mark.parametrize(
    "s,expected",
    [
        ("hello", 5),
        ("", 0),
        ("café", 4),  # é is BMP, 1 unit
        ("👍", 2),  # emoji = surrogate pair
        ("hi👍there", 9),  # 2+2+5
    ],
)
def test_utf16_len(s, expected):
    assert utf16_len(s) == expected


def test_prefix_within_utf16_limit_simple():
    s = "hello world"
    assert _prefix_within_utf16_limit(s, 5) == 5
    assert _prefix_within_utf16_limit(s, 100) == len(s)


def test_prefix_within_utf16_limit_emoji_boundary():
    # 4 emojis = 8 UTF-16 units. Budget 5 -> at most 2 emojis (4 units).
    s = "👍" * 4
    assert _prefix_within_utf16_limit(s, 5) == 2


def test_truncate_message_smart_short():
    assert truncate_message_smart("hello", max_length=100) == ["hello"]


def test_truncate_message_smart_simple_split():
    text = "x" * 250
    chunks = truncate_message_smart(text, max_length=100)
    for c in chunks:
        assert len(c) <= 100


def test_truncate_message_smart_reopens_code_fence():
    text = "intro\n```python\n" + "x = 1\n" * 50 + "```\nouter"
    chunks = truncate_message_smart(text, max_length=80)
    # Every chunk has balanced fences (even ``` count) when output as a unit
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, f"Unbalanced fences in chunk: {chunk!r}"


def test_truncate_message_smart_indicator_appended():
    text = "x" * 250
    chunks = truncate_message_smart(text, max_length=50)
    assert "(1/" in chunks[0]
    last_idx = len(chunks)
    assert f"({last_idx}/{last_idx})" in chunks[-1]


def test_truncate_message_smart_utf16_aware():
    # 2050 surrogate-pair emojis (4100 utf16 units)
    text = "👍" * 2050
    chunks = truncate_message_smart(text, max_length=4096, len_fn=utf16_len)
    for c in chunks:
        assert utf16_len(c) <= 4096


def test_truncate_message_smart_empty_input():
    assert truncate_message_smart("", max_length=100) == [""]


def test_supported_document_types_has_pdf_md_zip_office():
    assert ".pdf" in SUPPORTED_DOCUMENT_TYPES
    assert ".md" in SUPPORTED_DOCUMENT_TYPES
    assert ".zip" in SUPPORTED_DOCUMENT_TYPES
    assert ".docx" in SUPPORTED_DOCUMENT_TYPES
    assert ".xlsx" in SUPPORTED_DOCUMENT_TYPES
    assert ".pptx" in SUPPORTED_DOCUMENT_TYPES
    for v in SUPPORTED_DOCUMENT_TYPES.values():
        assert "/" in v


def test_supported_video_types_set():
    assert {".mp4", ".mov", ".webm", ".mkv", ".avi"}.issubset(SUPPORTED_VIDEO_TYPES)


# ---------------------------------------------------------------------------
# ProcessingOutcome
# ---------------------------------------------------------------------------


def test_processing_outcome_values():
    assert ProcessingOutcome.SUCCESS.value == "success"
    assert ProcessingOutcome.FAILURE.value == "failure"
    assert ProcessingOutcome.CANCELLED.value == "cancelled"


def test_processing_outcome_is_str_enum():
    # Ensures interop with code that compares against strings
    assert ProcessingOutcome.SUCCESS == "success"
