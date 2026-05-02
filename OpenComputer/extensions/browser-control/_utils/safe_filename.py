"""Sanitize untrusted file basenames.

Strips both POSIX and Windows directory components, drops control chars
(< 0x20 + 0x7F), then caps to `max_len`. Extension is preserved when
capping so `report-...long....pdf` stays a `.pdf` after truncation
(better UX than OpenClaw's bare slice).
"""

from __future__ import annotations

import os
from pathlib import PurePosixPath, PureWindowsPath

_DEFAULT = "untitled"
_MAX_PRESERVED_EXT = 10  # don't preserve absurdly long extensions ("foo.thisisnotatype")


def sanitize(name: str, *, max_len: int = 200) -> str:
    if max_len <= 0:
        raise ValueError("max_len must be > 0")
    candidate = (name or "").strip()
    if not candidate:
        return _DEFAULT
    candidate = PurePosixPath(candidate).name
    candidate = PureWindowsPath(candidate).name
    candidate = "".join(c for c in candidate if ord(c) >= 0x20 and ord(c) != 0x7F)
    candidate = candidate.replace(os.sep, "_").replace("/", "_").strip()
    if candidate in ("", ".", ".."):
        return _DEFAULT
    if len(candidate) > max_len:
        stem, dot, ext = candidate.rpartition(".")
        if dot and stem and 1 <= len(ext) <= _MAX_PRESERVED_EXT:
            keep = max_len - len(ext) - 1
            candidate = stem[:keep] + "." + ext if keep >= 1 else candidate[:max_len]
        else:
            candidate = candidate[:max_len]
    return candidate
