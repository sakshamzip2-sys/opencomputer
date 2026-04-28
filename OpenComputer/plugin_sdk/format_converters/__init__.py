"""Per-platform format converters.

Each module exports ``convert(text: str) -> str`` and may export helpers.
All converters fall back to plain text on parse error (never raise).
"""
