"""Unit tests for `snapshot/screenshot.py` — 7 × 6 size/quality grid."""

from __future__ import annotations

import io

import pytest
from extensions.browser_control.snapshot import (
    DEFAULT_BROWSER_SCREENSHOT_MAX_BYTES,
    DEFAULT_BROWSER_SCREENSHOT_MAX_SIDE,
    JPEG_QUALITY_STEPS,
    ScreenshotTooLargeError,
    normalize_screenshot,
)
from extensions.browser_control.snapshot.screenshot import _build_side_grid
from PIL import Image

# ─── side-grid construction ───────────────────────────────────────────


def test_side_grid_descending_unique_clamped() -> None:
    """Seed plus base (1800/1600/.../800) deduped, clamped <= max_side, descending."""
    grid = _build_side_grid(seed=1500, max_side=2000)
    # Highest-value item is 1800 (the largest <= max_side from base + seed).
    assert grid[0] == 1800
    assert 1500 in grid
    assert grid == sorted(grid, reverse=True)
    # All items <= max_side.
    assert all(s <= 2000 for s in grid)
    # No duplicates.
    assert len(grid) == len(set(grid))


def test_side_grid_seed_above_base() -> None:
    grid = _build_side_grid(seed=2200, max_side=2200)
    # seed clamps to 2200; 1800/1600/.../800 all included.
    assert grid[0] == 2200
    for s in (1800, 1600, 1400, 1200, 1000, 800):
        assert s in grid


def test_side_grid_seed_below_base() -> None:
    grid = _build_side_grid(seed=900, max_side=900)
    # All base steps > 900 are dropped, only 900 (seed) and 800 remain.
    assert grid == [900, 800]


# ─── happy paths ──────────────────────────────────────────────────────


def _make_png(size_px: int) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (size_px, size_px), color=(255, 0, 0))
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_small_png_passes_through_untouched() -> None:
    src = _make_png(50)
    out = await normalize_screenshot(src)
    assert out.buffer == src
    assert out.content_type == "image/png"


@pytest.mark.asyncio
async def test_oversized_png_resized_to_jpeg() -> None:
    """Image larger than max_side must be re-encoded to JPEG under the cap."""
    src = _make_png(3000)  # 3000x3000 > 2000 max_side
    out = await normalize_screenshot(src, max_side=1000, max_bytes=10_000_000)
    assert out.content_type == "image/jpeg"
    # JPEG output must decode and have side <= 1000.
    decoded = Image.open(io.BytesIO(out.buffer))
    decoded.load()
    assert max(decoded.size) <= 1000


@pytest.mark.asyncio
async def test_oversized_bytes_falls_to_smaller_quality() -> None:
    """A buffer over max_bytes triggers re-encode even if dimensions fit."""
    # 1500x1500 PNG with high-entropy bytes so PNG compression can't squeeze it.
    import os as _os
    img = Image.frombytes("RGB", (1500, 1500), _os.urandom(1500 * 1500 * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    src = buf.getvalue()
    # Sanity: random-byte PNG is way over 200KB.
    assert len(src) > 200_000, f"need a non-compressing PNG; got {len(src)} bytes"
    out = await normalize_screenshot(src, max_side=2000, max_bytes=200_000)
    assert out.content_type == "image/jpeg"
    assert len(out.buffer) <= 200_000


@pytest.mark.asyncio
async def test_unfittable_raises() -> None:
    """If even quality 35 at side 800 doesn't fit, raise ScreenshotTooLargeError."""
    src = _make_png(800)  # solid red — already tiny
    # Set max_bytes ridiculously small so nothing fits.
    with pytest.raises(ScreenshotTooLargeError):
        await normalize_screenshot(src, max_bytes=10)


@pytest.mark.asyncio
async def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        await normalize_screenshot(b"")
    src = _make_png(50)
    with pytest.raises(ValueError):
        await normalize_screenshot(src, max_side=0)
    with pytest.raises(ValueError):
        await normalize_screenshot(src, max_bytes=0)


# ─── grid constants are stable ────────────────────────────────────────


def test_quality_steps_are_descending() -> None:
    assert list(JPEG_QUALITY_STEPS) == sorted(JPEG_QUALITY_STEPS, reverse=True)
    assert JPEG_QUALITY_STEPS[0] == 85
    assert JPEG_QUALITY_STEPS[-1] == 35
    assert len(JPEG_QUALITY_STEPS) == 6


def test_default_caps() -> None:
    assert DEFAULT_BROWSER_SCREENSHOT_MAX_SIDE == 2000
    assert DEFAULT_BROWSER_SCREENSHOT_MAX_BYTES == 5_000_000
