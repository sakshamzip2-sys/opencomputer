"""Sensitive-app filter for the ambient foreground sensor.

A snapshot is "sensitive" if its app_name OR window_title matches any
regex in (defaults + user overrides). Sensitive snapshots are filtered
to ``app_name="<filtered>"`` BEFORE publish — raw values never leave
the sensor.

The filter's contract: ``is_sensitive`` returns a bool only. It never
returns the matched pattern, the matched value, or any other diagnostic
information that could leak the underlying data.
"""

from __future__ import annotations

import re
from pathlib import Path

from .foreground import ForegroundSnapshot

_DEFAULT_PATTERNS: tuple[str, ...] = (
    # Password managers
    r"(?i)1Password",
    r"(?i)Bitwarden",
    r"(?i)KeePass",
    r"(?i)Dashlane",
    r"(?i)LastPass",
    # Banking — generic + region-specific
    r"(?i)\bbank\b",
    r"(?i)Chase",
    r"(?i)HDFC",
    r"(?i)ICICI",
    r"(?i)\bSBI\b",
    r"(?i)Robinhood",
    r"(?i)Coinbase",
    r"(?i)MetaMask",
    r"(?i)Zerodha",
    r"(?i)Groww",
    r"(?i)Schwab",
    r"(?i)Fidelity",
    r"(?i)Vanguard",
    # Healthcare
    r"(?i)MyChart",
    r"(?i)Teladoc",
    r"(?i)Healow",
    # Private browsing / secure
    r"(?i)Private Browsing",
    r"(?i)Incognito",
    r"(?i)Tor Browser",
    r"(?i)Signal",
    r"(?i)ProtonMail",
)


def load_user_overrides(path: Path) -> list[str]:
    """Read additional regex patterns from a user-managed text file.

    Format: one regex per line; lines starting with ``#`` are comments;
    blank lines ignored. Returns an empty list if the file doesn't exist.
    """
    if not path.exists():
        return []
    out: list[str] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    except OSError:
        return []
    return out


def is_sensitive(
    snap: ForegroundSnapshot,
    extra_patterns: list[str] | None = None,
) -> bool:
    """Return True iff snapshot's app_name OR window_title matches any pattern.

    Malformed user-supplied regexes are silently skipped (never raised) so
    a bad config can't break the daemon.
    """
    haystack = f"{snap.app_name}\n{snap.window_title}"
    patterns = list(_DEFAULT_PATTERNS)
    if extra_patterns:
        patterns.extend(extra_patterns)
    for pat in patterns:
        try:
            if re.search(pat, haystack):
                return True
        except re.error:
            continue
    return False
