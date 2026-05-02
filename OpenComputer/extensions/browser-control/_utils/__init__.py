"""Shared leaf utilities for the OpenClaw browser port.

Public surface:
  - atomic_write_text / atomic_write_bytes / atomic_write_json
  - url_match (modes: exact, glob, substring)
  - sanitize_filename
  - move_to_trash
  - BrowserServiceError + .from_response(status, body)
"""

from __future__ import annotations

from .atomic_write import atomic_write_bytes, atomic_write_json, atomic_write_text
from .errors import BrowserServiceError
from .safe_filename import sanitize as sanitize_filename
from .trash import move_to_trash
from .url_pattern import UrlPatternMode
from .url_pattern import match as url_match

__all__ = [
    "BrowserServiceError",
    "UrlPatternMode",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "move_to_trash",
    "sanitize_filename",
    "url_match",
]
