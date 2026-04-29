"""Sensitive-app filter — inline regex denylist.

Mirrors ambient-sensors's denylist content but is its own module so
screen-awareness doesn't depend on ambient-sensors being installed.
Sync drift cost is minimal — both lists are small + rarely changed.

Contract: ``is_app_sensitive(app_name) -> bool``. Returns bool only.
Never returns the matched pattern. Never logs the match. Privacy-by-
construction.
"""
from __future__ import annotations

import re

#: Regex denylist — case-insensitive. Mirrors ambient-sensors content.
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
    # Crypto wallets / 2FA apps
    r"(?i)Authy",
    r"(?i)Authenticator",
    r"(?i)Ledger Live",
)

_COMPILED: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in _DEFAULT_PATTERNS)


def is_app_sensitive(app_name: str) -> bool:
    """True iff ``app_name`` matches any pattern in the denylist.

    Returns bool only — never the matched pattern, never logs.
    Empty/None input returns False (no app to check).
    """
    if not app_name:
        return False
    return any(pat.search(app_name) for pat in _COMPILED)


__all__ = ["is_app_sensitive"]
