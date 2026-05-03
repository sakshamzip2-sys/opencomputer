"""Screenshot normalization — 7 × 6 size/quality grid.

Algorithm (deep-dive §6):

  1. If the buffer is already under both ``max_bytes`` and ``max_side``
     (in either dimension) → return it untouched (preserves PNG).
  2. Otherwise iterate side ∈ [seed, 1800, 1600, 1400, 1200, 1000, 800]
     (descending, deduped, clamped ≤ max_side) outer × quality ∈ [85, 75,
     65, 55, 45, 35] inner.
  3. Each step re-encodes from the *original* buffer (never re-recompresses
     a JPEG). First variant under ``max_bytes`` wins; we return its bytes
     and ``contentType="image/jpeg"``.
  4. If no variant fits → raise ``ScreenshotTooLargeError`` with the
     smallest variant's size for diagnostic purposes.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Final, Literal

from PIL import Image, ImageOps

DEFAULT_BROWSER_SCREENSHOT_MAX_SIDE: Final[int] = 2000
DEFAULT_BROWSER_SCREENSHOT_MAX_BYTES: Final[int] = 5_000_000

# Inner loop — JPEG quality steps. Mirrors OpenClaw's IMAGE_REDUCE_QUALITY_STEPS.
JPEG_QUALITY_STEPS: Final[tuple[int, ...]] = (85, 75, 65, 55, 45, 35)

# Outer loop — base side grid (descending). Caller's max_side is used as
# the seed; this list contributes the additional steps.
SIDE_GRID_BASE: Final[tuple[int, ...]] = (1800, 1600, 1400, 1200, 1000, 800)

# Hard pixel-count cap (matches OpenClaw's 25M limit). Inputs above this
# are rejected before any image ops to avoid OOM on a malicious uploads.
MAX_INPUT_PIXELS: Final[int] = 25_000_000


@dataclass(frozen=True, slots=True)
class NormalizedScreenshot:
    buffer: bytes
    content_type: Literal["image/png", "image/jpeg"]


class ScreenshotTooLargeError(Exception):
    """No grid combination produced a buffer under ``max_bytes``."""


def _build_side_grid(seed: int, max_side: int) -> list[int]:
    candidates = [seed, *SIDE_GRID_BASE]
    out: list[int] = []
    seen: set[int] = set()
    for s in candidates:
        clamped = min(int(s), int(max_side))
        if clamped < 1:
            continue
        if clamped in seen:
            continue
        seen.add(clamped)
        out.append(clamped)
    out.sort(reverse=True)
    return out


def _resize_to_jpeg(
    img: Image.Image,
    *,
    max_side: int,
    quality: int,
) -> bytes:
    """Resize within bounding box (no enlarge), JPEG-encode at quality.

    EXIF rotation auto-applied (matches OpenClaw's `sharp.rotate()`)
    so portrait photos don't end up rotated 90° after re-encode.
    """
    work = ImageOps.exif_transpose(img)
    if work is None:
        work = img
    # Convert to RGB for JPEG (JPEG doesn't support alpha).
    if work.mode in ("RGBA", "LA", "P") or work.mode != "RGB":
        work = work.convert("RGB")

    w, h = work.size
    longest = max(w, h)
    if longest > max_side:
        scale = max_side / float(longest)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        work = work.resize((new_w, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    work.save(buf, format="JPEG", quality=int(quality), optimize=True)
    return buf.getvalue()


def _read_metadata(buffer: bytes) -> tuple[int, int]:
    """Return (width, height) or (0, 0) if undecidable."""
    try:
        with Image.open(io.BytesIO(buffer)) as img:
            w, h = img.size
            if w * h > MAX_INPUT_PIXELS:
                raise ScreenshotTooLargeError(
                    f"Image dimensions exceed the {MAX_INPUT_PIXELS:,} pixel input limit"
                )
            return int(w), int(h)
    except ScreenshotTooLargeError:
        raise
    except Exception:  # noqa: BLE001 — metadata is best-effort
        return 0, 0


async def normalize_screenshot(
    buffer: bytes,
    *,
    max_side: int = DEFAULT_BROWSER_SCREENSHOT_MAX_SIDE,
    max_bytes: int = DEFAULT_BROWSER_SCREENSHOT_MAX_BYTES,
) -> NormalizedScreenshot:
    """Normalize a screenshot to fit under ``max_bytes`` & ``max_side``.

    Falls within the limits already → returns the original buffer
    untouched with content_type="image/png" (caller can override).

    Re-encoded outputs are always image/jpeg.
    """
    if max_side < 1:
        raise ValueError("max_side must be >= 1")
    if max_bytes < 1:
        raise ValueError("max_bytes must be >= 1")
    if not buffer:
        raise ValueError("normalize_screenshot: empty buffer")

    width, height = _read_metadata(buffer)
    long_dim = max(width, height)

    if (
        len(buffer) <= max_bytes
        and (long_dim == 0 or (width <= max_side and height <= max_side))
    ):
        return NormalizedScreenshot(buffer=buffer, content_type="image/png")

    side_seed = max_side if long_dim == 0 else min(max_side, long_dim)
    side_grid = _build_side_grid(side_seed, max_side)

    smallest: bytes | None = None
    smallest_size = -1
    with Image.open(io.BytesIO(buffer)) as img:
        # Force-load so the file handle isn't held while we iterate.
        img.load()
        for side in side_grid:
            for quality in JPEG_QUALITY_STEPS:
                out = _resize_to_jpeg(img, max_side=side, quality=quality)
                if smallest is None or len(out) < smallest_size:
                    smallest = out
                    smallest_size = len(out)
                if len(out) <= max_bytes:
                    return NormalizedScreenshot(buffer=out, content_type="image/jpeg")

    smallest_size_str = (
        f"{smallest_size / 1_000_000:.2f}MB" if smallest is not None else "unknown"
    )
    raise ScreenshotTooLargeError(
        f"Browser screenshot could not be reduced below "
        f"{max_bytes / 1_000_000:.1f}MB (best variant: {smallest_size_str})"
    )
