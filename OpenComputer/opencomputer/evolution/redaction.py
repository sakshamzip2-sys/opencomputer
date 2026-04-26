"""Secondary regex sweep for trajectory export bundles (P-14).

The schema-level privacy rule on :class:`opencomputer.evolution.trajectory.TrajectoryEvent`
already rejects any string-value metadata > 200 chars at construction time, so
records on disk are already redacted-by-construction.  This module provides a
SECONDARY sweep applied to short metadata string values (e.g. file paths, error
message previews, tool args) to scrub residual PII before bundling.

Patterns (per plan section P-14):

- API keys     : ``(sk-|anthropic-|github_pat_|gh[pousr]_)[a-zA-Z0-9_]{20,}`` → ``<API_KEY_REDACTED>``
- File paths   : ``/Users/<name>/`` → ``/Users/REDACTED/`` (preserves trailing path tail)
- Email        : ``user@host.tld`` → ``<EMAIL_REDACTED>``
- IP addresses : ``A.B.C.D`` → ``<IP_REDACTED>`` (skips loopback ``127.0.0.1``/``0.0.0.0``)
- Bearer tokens: ``Bearer <token>`` → ``Bearer <REDACTED>``

Public API:

    >>> redacted, counts = redact("contact me at foo@bar.com")
    >>> redacted
    'contact me at <EMAIL_REDACTED>'
    >>> counts
    {'email': 1, 'api_key': 0, 'file_path': 0, 'ip': 0, 'bearer_token': 0}
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# API keys: anthropic / openai / github personal-access-token style prefixes
# followed by ≥20 chars from the secret alphabet.  Conservative — won't catch
# every future API-key style but catches the four most common ones.
_API_KEY_RE = re.compile(r"(?:sk-|anthropic-|github_pat_|gh[pousr]_)[a-zA-Z0-9_]{20,}")

# File paths under macOS/Linux user home: ``/Users/<username>/...`` — replace
# the username segment only.  We keep the trailing path so log lines still make
# sense (e.g. ``/Users/REDACTED/Vscode/foo.py``).
_FILE_PATH_RE = re.compile(r"/Users/[^/\s]+/")

# Email — RFC 5322 is much wider than this but the common case is enough.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# IPv4 — four 1-3 digit octets joined by dots.  Loopback / wildcard skipped
# so localhost references survive (useful for debugging trajectories).
_IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_IP_SAFE = {"127.0.0.1", "0.0.0.0"}

# Bearer tokens — ``Authorization: Bearer …`` style.  Case-sensitive on the
# ``Bearer`` keyword to avoid spurious matches on prose like "bearer scheme".
_BEARER_RE = re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.=]+")

# ---------------------------------------------------------------------------
# Replacement helpers
# ---------------------------------------------------------------------------


def _redact_api_keys(text: str) -> tuple[str, int]:
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "<API_KEY_REDACTED>"

    out = _API_KEY_RE.sub(_sub, text)
    return out, count


def _redact_file_paths(text: str) -> tuple[str, int]:
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "/Users/REDACTED/"

    out = _FILE_PATH_RE.sub(_sub, text)
    return out, count


def _redact_emails(text: str) -> tuple[str, int]:
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "<EMAIL_REDACTED>"

    out = _EMAIL_RE.sub(_sub, text)
    return out, count


def _redact_ips(text: str) -> tuple[str, int]:
    count = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal count
        if m.group(0) in _IP_SAFE:
            return m.group(0)
        count += 1
        return "<IP_REDACTED>"

    out = _IP_RE.sub(_sub, text)
    return out, count


def _redact_bearers(text: str) -> tuple[str, int]:
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "Bearer <REDACTED>"

    out = _BEARER_RE.sub(_sub, text)
    return out, count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Stable order of patterns — used by the empty counter and reporting.
PATTERN_NAMES: tuple[str, ...] = (
    "api_key",
    "file_path",
    "email",
    "ip",
    "bearer_token",
)


def empty_counts() -> dict[str, int]:
    """Return a fresh ``{pattern_name: 0}`` counter for all 5 patterns."""
    return {name: 0 for name in PATTERN_NAMES}


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Apply all 5 redaction patterns to *text*.

    Returns a tuple ``(redacted_text, counts)`` where ``counts`` is a dict
    mapping pattern-name to number of matches replaced.  The order of patterns
    applied is: API keys → file paths → emails → IPs → bearer tokens.  This
    order matters because API keys can contain characters that look like other
    patterns (e.g. an OpenAI key could match the email regex if it contained
    an ``@``); redacting keys first prevents double-counting.
    """
    counts = empty_counts()
    out = text
    out, counts["api_key"] = _redact_api_keys(out)
    out, counts["file_path"] = _redact_file_paths(out)
    out, counts["email"] = _redact_emails(out)
    out, counts["ip"] = _redact_ips(out)
    out, counts["bearer_token"] = _redact_bearers(out)
    return out, counts


def redact_metadata(metadata: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Apply :func:`redact` to every string value in *metadata*.

    Non-string values (int, float, list, dict, None, …) are passed through
    unchanged — the schema-level 200-char privacy rule already prevents large
    string values from being stored, so the redaction sweep need only consider
    short string fields like ``file_path``, ``error_message_preview``, etc.

    Returns a tuple ``(redacted_dict, total_counts)`` where ``total_counts``
    sums hits across all string values in the mapping.
    """
    out: dict[str, Any] = {}
    totals = empty_counts()
    for key, value in metadata.items():
        if isinstance(value, str):
            new_value, hits = redact(value)
            out[key] = new_value
            for name, count in hits.items():
                totals[name] += count
        else:
            out[key] = value
    return out, totals


def merge_counts(*counters: Mapping[str, int]) -> dict[str, int]:
    """Sum multiple counter dicts (e.g. one per event) into a single total."""
    total = empty_counts()
    for c in counters:
        for name in PATTERN_NAMES:
            total[name] += c.get(name, 0)
    return total


__all__ = [
    "PATTERN_NAMES",
    "empty_counts",
    "redact",
    "redact_metadata",
    "merge_counts",
]
