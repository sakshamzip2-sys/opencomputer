"""URL pattern matching: exact / glob / substring.

The glob mode supports `*` (zero or more characters) only. OpenClaw's
url-pattern.ts docstring claimed `?` as a single-char wildcard but never
implemented it; we drop the claim rather than carry a stale promise.

Case-sensitive. Trailing slashes are normalized in `exact` mode so
`https://x.com` and `https://x.com/` compare equal.
"""

from __future__ import annotations

import re
from typing import Literal

UrlPatternMode = Literal["exact", "glob", "substring"]


def _strip_one_trailing_slash(s: str) -> str:
    return s[:-1] if len(s) > 1 and s.endswith("/") else s


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    parts: list[str] = []
    for ch in glob:
        if ch == "*":
            parts.append(".*")
        else:
            parts.append(re.escape(ch))
    return re.compile(rf"\A{''.join(parts)}\Z", re.DOTALL)


def match(pattern: str, url: str, *, mode: UrlPatternMode) -> bool:
    if mode == "exact":
        return _strip_one_trailing_slash(pattern) == _strip_one_trailing_slash(url)
    if mode == "substring":
        return pattern in url
    if mode == "glob":
        return bool(_glob_to_regex(pattern).match(url))
    raise ValueError(f"unknown url_pattern mode: {mode!r}")
