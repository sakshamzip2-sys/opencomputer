"""Tests for the gateway truncation helper (Wave 6.E.6)."""

from __future__ import annotations

from opencomputer.gateway._truncate import (
    DEFAULT_MAX_LEN,
    ELLIPSIS,
    truncate_for_platform,
    truncate_smart,
)


def test_short_text_passes_through():
    assert truncate_for_platform("hello") == "hello"


def test_long_text_truncated_with_ellipsis():
    text = "x" * 5000
    out = truncate_for_platform(text, max_len=100)
    assert len(out) <= 100
    assert out.endswith(ELLIPSIS)


def test_default_max_len():
    text = "x" * 5000
    out = truncate_for_platform(text)
    assert len(out) <= DEFAULT_MAX_LEN
    assert out.endswith(ELLIPSIS)


def test_smart_truncate_passes_through_short():
    assert truncate_smart("short", max_len=100) == "short"


def test_smart_truncate_avoids_splitting_open_code_fence():
    """When the cut would land mid-fence, walk back to the opening fence
    (so the kept portion has only fully-closed pairs).

    Test crafts a text where the open-fence ``` is within ``lookback``
    chars of the naive cut, so the smart logic finds it.
    """
    closed_pair = "```python\ndef foo():\n    pass\n```\n"
    # First closed pair, then 100 padding chars, then a 3rd ``` (open),
    # then 50 chars of content. Total ~ 200 chars before the open
    # fence; with max_len=300 the cut lands inside the open fence and
    # the lookback (default 200) reaches the open fence.
    text = (
        closed_pair
        + ("p" * 200)
        + "```\n"
        + "open fence content "
        * 50
    )
    out = truncate_smart(text, max_len=300)
    body = out[: -len(ELLIPSIS)] if out.endswith(ELLIPSIS) else out
    # Smart cut should yield an even number of fences in the kept body.
    assert body.count("```") % 2 == 0, (
        f"got odd fence count in body: {body!r}"
    )


def test_smart_truncate_falls_back_when_no_boundary():
    """No fence in the lookback window → naive cut still works."""
    text = "```\n" + ("payload " * 5000) + "\n```"
    out = truncate_smart(text, max_len=200, lookback=20)
    assert len(out) <= 200
    assert out.endswith(ELLIPSIS)


def test_max_len_smaller_than_ellipsis():
    """Pathological tiny cap: return ELLIPSIS truncated to the cap."""
    out = truncate_for_platform("anything", max_len=3)
    assert len(out) <= 3
