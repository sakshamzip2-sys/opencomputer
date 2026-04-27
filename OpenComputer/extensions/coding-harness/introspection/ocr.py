"""OCR backend for ExtractScreenTextTool.

Default backend: rapidocr-onnxruntime (cross-platform pure-pip wheel; bundles
its ONNX model — no system Tesseract install required).

Imports of rapidocr are LAZY (inside the function body) so loading this module
doesn't pay the ~70 MB / ~5 s model-load cost for users who never call OCR.
"""

from __future__ import annotations

import io

# Memoized RapidOCR instance to avoid re-loading model weights between calls
# in the same process. None until first invocation.
_ocr = None  # type: ignore[var-annotated]


def ocr_text_from_screen() -> str:
    """Capture the primary monitor and OCR it; return joined text.

    Uses ``mss`` for the capture (matching ScreenshotTool) and rapidocr-onnx
    for OCR. The first call in a process pays the model-load cost (~5 s);
    subsequent calls reuse the memoized ``_ocr`` instance.

    Returns an empty string when OCR finds nothing (blank screen, locked
    screen, etc.). Raises any underlying exceptions — the caller (the tool's
    ``execute``) wraps them into a ``ToolResult(is_error=True)``.
    """
    # Lazy imports — see module docstring rationale.
    import mss
    import mss.tools
    from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

    global _ocr
    if _ocr is None:
        _ocr = RapidOCR()

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        png = mss.tools.to_png(shot.rgb, shot.size)

    result, _elapsed = _ocr(io.BytesIO(png))
    if not result:
        return ""

    # rapidocr returns [[bbox, text, confidence], ...]; index 1 is text.
    return "\n".join(line[1] for line in result if len(line) >= 2)
