"""Inline OCR helper — direct mss + rapidocr-onnxruntime use.

Ships its own OCR codepath rather than importing from
``extensions/coding-harness/introspection/ocr.py`` so the cross-plugin
boundary stays clean (per the isolation test).

Imports are LAZY — first call pays the ~5s model-load cost; subsequent
calls reuse the memoized ``_ocr`` instance.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("opencomputer.screen_awareness.ocr_inline")

# Memoized RapidOCR instance to avoid re-loading model weights between
# calls in the same process. None until first invocation.
_ocr = None  # type: ignore[var-annotated]


def ocr_text_from_screen() -> str:
    """Capture primary monitor + OCR; return joined text.

    Raises any underlying exceptions — caller wraps to ToolResult.
    """
    global _ocr

    import mss  # type: ignore[import-not-found]
    import mss.tools  # type: ignore[import-not-found]

    with mss.mss() as sct:
        monitors = sct.monitors
        if len(monitors) < 2:
            raise RuntimeError("no monitors available for capture")
        # monitors[0] is the union of all displays; [1] is the primary.
        primary = monitors[1]
        sct_img = sct.grab(primary)
        png_bytes = mss.tools.to_png(sct_img.rgb, sct_img.size)

    if _ocr is None:
        # Lazy import + lazy model-load (~5 s on first call).
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

        _ocr = RapidOCR()

    result, _elapsed = _ocr(png_bytes)
    if not result:
        return ""
    # Each result entry is (bbox, text, confidence). Join texts.
    return "\n".join(line[1] for line in result)


__all__ = ["ocr_text_from_screen"]
