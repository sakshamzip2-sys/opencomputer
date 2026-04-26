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
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+")

# Slack legacy bot/personal tokens — ``xoxb-...`` (bot) or ``xoxp-...``
# (personal). Both prefixes share a layout of dash-separated segments
# at least 20 chars on the trailing segment to avoid matching prose.
_SLACK_TOKEN_RE = re.compile(r"xox[bp]-[A-Za-z0-9-]{20,}")

# Telegram bot tokens — ``<bot-id>:<random>``. The colon split + the
# trailing random segment ≥20 chars rules out most non-token integers.
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d+:[A-Za-z0-9_-]{20,}\b")

# Anthropic API keys — ``sk-ant-...``. Caught BEFORE the generic
# OpenAI ``sk-`` rule so the more specific replacement label wins.
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]+")

# OpenAI-style API keys — ``sk-...`` (≥20 chars after the prefix).
# Excludes the ``sk-ant-`` shape so we don't double-match Anthropic
# keys (anchored ``\b`` plus the ``[A-Za-z0-9]`` body keeps it tight).
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")

# AWS access key IDs — ``AKIA`` + 16 uppercase alnum chars.  These
# are ID-side only; secret access keys are 40 chars of base64 with
# no fixed prefix and intentionally NOT matched here (would catch
# UUIDs / hashes).
_AWS_AKID_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

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


def _redact_slack_tokens(text: str) -> tuple[str, int]:
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "<SLACK_TOKEN_REDACTED>"

    out = _SLACK_TOKEN_RE.sub(_sub, text)
    return out, count


def _redact_telegram_tokens(text: str) -> tuple[str, int]:
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "<TELEGRAM_TOKEN_REDACTED>"

    out = _TELEGRAM_TOKEN_RE.sub(_sub, text)
    return out, count


def _redact_anthropic_keys(text: str) -> tuple[str, int]:
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "<ANTHROPIC_KEY_REDACTED>"

    out = _ANTHROPIC_KEY_RE.sub(_sub, text)
    return out, count


def _redact_openai_keys(text: str) -> tuple[str, int]:
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "<OPENAI_KEY_REDACTED>"

    out = _OPENAI_KEY_RE.sub(_sub, text)
    return out, count


def _redact_aws_keys(text: str) -> tuple[str, int]:
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "<AWS_AKID_REDACTED>"

    out = _AWS_AKID_RE.sub(_sub, text)
    return out, count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Stable order of patterns — used by the empty counter and reporting.
# P-16 added five token-shaped patterns (slack/telegram/anthropic/openai/aws);
# they run AFTER the generic API-key pass so the more specific labels win.
PATTERN_NAMES: tuple[str, ...] = (
    "api_key",
    "file_path",
    "email",
    "ip",
    "bearer_token",
    "slack_token",
    "telegram_token",
    "anthropic_key",
    "openai_key",
    "aws_akid",
)


def empty_counts() -> dict[str, int]:
    """Return a fresh ``{pattern_name: 0}`` counter for all 5 patterns."""
    return {name: 0 for name in PATTERN_NAMES}


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Apply all redaction patterns to *text*.

    Returns a tuple ``(redacted_text, counts)`` where ``counts`` is a dict
    mapping pattern-name to number of matches replaced. Order of application
    matters because some patterns are subsets of others. The current chain is:

    1. ``anthropic_key``  — ``sk-ant-...`` redacted before the generic ``sk-``
       pattern so the Anthropic-specific label wins.
    2. ``openai_key``     — ``sk-...`` (≥20 chars), excluding the Anthropic
       prefix already consumed.
    3. ``slack_token``    — ``xox[bp]-...``.
    4. ``telegram_token`` — ``<digits>:<random>``.
    5. ``aws_akid``       — ``AKIA...`` (16-char access key id).
    6. ``api_key``        — generic ``sk-/anthropic-/github_pat_/gh*_`` legacy
       pattern. Most precise prefix-only matches have already been consumed
       above, but kept for backwards-compat with existing trajectories.
    7. ``file_path``      — ``/Users/<name>/`` username scrubbing.
    8. ``email``          — RFC-5322 lite.
    9. ``ip``             — IPv4 (loopback skipped).
    10. ``bearer_token``  — ``Bearer <token>`` last so a bearer wrapping a
        Slack token still has its inner token caught above.

    P-16 added entries 1-5 + the AWS key pattern; the long-standing
    behavior (entries 6-10) is preserved.
    """
    counts = empty_counts()
    out = text
    out, counts["anthropic_key"] = _redact_anthropic_keys(out)
    out, counts["openai_key"] = _redact_openai_keys(out)
    out, counts["slack_token"] = _redact_slack_tokens(out)
    out, counts["telegram_token"] = _redact_telegram_tokens(out)
    out, counts["aws_akid"] = _redact_aws_keys(out)
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
