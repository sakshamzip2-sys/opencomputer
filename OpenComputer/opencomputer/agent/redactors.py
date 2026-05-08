"""Secret-pattern redaction for ``security.redact_secrets`` (Hermes config v2).

Conservative regex set — opt-in (off by default). Patterns require enough
length to avoid false positives on legitimate strings (e.g., ``sk-1`` is
NOT redacted, but ``sk-abc123def456...`` IS).

Usage: call :func:`maybe_redact_secrets` (gates on the flag) at the
tool-output boundary BEFORE the text enters conversation context AND
before it lands in logs. Off-by-default preserves prior behavior.
"""
from __future__ import annotations

import re

#: Patterns ordered by specificity. Each pattern matches a likely-secret
#: substring and is replaced with ``[REDACTED]``. First-match wins; no
#: overlap-aware passes (acceptable for this redaction tier).
_DEFAULT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenAI / Anthropic style: sk-<20+ alnum/dash/underscore>.
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    # GitHub fine-grained PAT — ``github_pat_`` + 22 alnum + ``_`` + 59 alnum.
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    # GitHub classic PAT — ``ghp_`` + 36 alnum.
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    # GitHub OAuth tokens.
    re.compile(r"gho_[A-Za-z0-9]{36}"),
    # Slack bot/user/app tokens.
    re.compile(r"xox[bpoa]-[A-Za-z0-9-]{20,}"),
    # AWS access keys.
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # ``Bearer <token>`` in Authorization headers (case-insensitive).
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9_.\-]{30,}"),
)


def redact_secrets_in_text(
    text: str, patterns: tuple[re.Pattern[str], ...] = _DEFAULT_PATTERNS
) -> str:
    """Replace each match of any pattern with ``[REDACTED]``.

    Conservative: patterns require length thresholds to avoid false
    positives. Multiple patterns may match overlapping text — first
    match wins (regex passes run sequentially).
    """
    for pattern in patterns:
        text = pattern.sub("[REDACTED]", text)
    return text


def maybe_redact_secrets(text: str, *, redact: bool) -> str:
    """Apply :func:`redact_secrets_in_text` iff ``redact`` is True.

    Convenience wrapper for callers that want a single-line gate against
    ``cfg.security.redact_secrets``.
    """
    return redact_secrets_in_text(text) if redact else text


__all__ = [
    "maybe_redact_secrets",
    "redact_secrets_in_text",
]
