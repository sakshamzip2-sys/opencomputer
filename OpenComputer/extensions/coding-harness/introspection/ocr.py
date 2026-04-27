"""OCR backend for ExtractScreenTextTool. Default: rapidocr-onnxruntime."""

from __future__ import annotations


def ocr_text_from_screen() -> str:
    """Capture the current screen and return extracted text.

    Implementation lands in T6 (rapidocr-onnxruntime + mss).
    """
    raise NotImplementedError("Implementation in T6")
