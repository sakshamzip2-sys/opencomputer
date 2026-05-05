"""Privacy redaction for the social-traces plugin — Phase 7.

The trace network is a privacy-sensitive egress surface: anything that
makes it into a TraceCard ships off-device. This module is the FIRST
LINE of defence — every string that flows into a TraceCard's
``intent`` / ``distilled_insight`` / step summaries gets redacted
through here BEFORE submission.

Layered defence:

1. Caller-supplied filter (``sensitive_filter``) — opt-in, lets a
   plugin or operator mark whole bodies as sensitive (e.g. via
   policy hook).
2. PII regex sweep — credit cards, SSNs, emails, phone numbers.
   Always on, never opt-out.
3. Path / hostname / IP / API-key sweep — opt-in via
   :class:`SocialTracesConfig.privacy`.

If a regex pattern feels too aggressive (false positives swallowing
legitimate domain words) the right fix is to tune the pattern, NOT
to relax the layer. The two security invariants from HANDOVER are
load-bearing — a noisy network beats a leaky one.

What this module does NOT do
-----------------------------

* Trust the LLM to redact. Distiller prompts include "redact paths
  and secrets" instructions, but those are best-effort hints — every
  LLM output goes through this module's regex sweeps before assembly.
* Detect business-context "sensitive" data (e.g. internal project
  codenames). That's the operator's job — they pass a
  ``sensitive_filter`` callable that knows their context.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

_log = logging.getLogger("opencomputer.social_traces.redactor")


#: Replacement sentinels — chosen so a downstream "is body useful?"
#: check (:func:`is_useful_body`) can tell sentinel-only output from
#: real content.
REDACTED = "<redacted>"
REDACTED_PII = "<redacted-pii>"
REDACTED_PATH = "<redacted-path>"
REDACTED_HOST = "<redacted-host>"
REDACTED_KEY = "<redacted-key>"


# ─── PII patterns (always-on layer) ──────────────────────────────────


#: 16-digit number with optional space/hyphen separators in 4-4-4-4
#: shape. We don't Luhn-validate — false positives at this stage are
#: acceptable, false negatives would leak. Same pattern skill-evolution
#: uses (battle-tested over 100s of stagings).
_CREDIT_CARD_RE = re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")

#: SSN-shaped: XXX-XX-XXXX. False positives possible (e.g. legitimate
#: 9-digit serial numbers split this way), but rare enough that
#: redaction is safer than ship-and-hope.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

#: Email addresses. RFC822 is impossible to fully match; this catches
#: the >99% common case.
_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"
)

#: US-shaped phone numbers. Optional country code, area code in
#: parens or with separators. Skip 10-digit-no-separator (catches too
#: many false positives like timestamps).
_PHONE_RE = re.compile(
    r"(?:\+?1[-.\s]?)?"          # optional +1 / 1-
    r"(?:\(\d{3}\)|\d{3})"       # area code
    r"[-.\s]\d{3}"               # exchange
    r"[-.\s]\d{4}"               # subscriber
    r"\b"
)


# ─── secret / API-key patterns ───────────────────────────────────────


#: Provider-prefix patterns — sk- (OpenAI, Anthropic), ghp_ (GitHub),
#: AIza (Google), xoxb-/xoxp- (Slack), Bearer tokens.
_API_KEY_RES: tuple[re.Pattern[str], ...] = (
    # OpenAI / Anthropic / generic ``sk-...`` (length >= 20)
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    # GitHub ``ghp_...`` / ``ghs_...`` / ``gho_...``
    re.compile(r"\b(?:ghp|ghs|gho|ghu|ghr)_[A-Za-z0-9]{20,}\b"),
    # Google API keys ``AIza...``
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    # Slack bot/user tokens ``xoxb-...``
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    # Bearer tokens — used in HTTP headers
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
    # Generic 32+ hex strings preceded by ``token``/``key``/``secret``
    # (catches ``my_api_key=abcdef0123...`` style)
    re.compile(
        r"(?:api[_-]?key|secret|token|password|passwd)\s*[=:]\s*"
        r"[\"']?[A-Za-z0-9_-]{20,}[\"']?",
        re.IGNORECASE,
    ),
)


# ─── path / host / IP patterns (opt-in layer) ────────────────────────


#: Absolute Unix paths (`/Users/x/...`, `/home/y/...`, `/var/...`),
#: tilde paths (`~/...`), Windows-ish paths
#: (`C:\Users\...`). User homes are the highest-value redaction
#: target (filename → username → identification).
_PATH_RE = re.compile(
    r"(?:"
    r"/(?:Users|home|root|var|opt|etc|tmp|usr|mnt)(?:/[A-Za-z0-9._-]+)+"  # POSIX
    r"|~/[A-Za-z0-9._/-]+"                                                 # tilde
    r"|[A-Za-z]:\\(?:Users|Documents|Program Files)(?:\\[A-Za-z0-9._ -]+)+"  # Windows
    r")"
)

#: URL hostnames + bare hostnames (3+ labels deep, to avoid clobbering
#: legitimate phrases like "rsync.org"). We redact the whole URL
#: rather than just the host so the trail stays useful: the agent sees
#: the original URL was "redacted out" instead of "URL with mystery
#: host suffix".
_URL_RE = re.compile(
    r"https?://[A-Za-z0-9.-]+(?:/[A-Za-z0-9._%/?&=+-]*)?",
)

#: Bare hostnames with internal-shaped suffixes (``.local``, ``.lan``,
#: ``.home``, etc.). Public TLDs ("github.com") don't match — we want
#: to catch homelab internals ("nas.lan", "macbook.local"), not
#: common services. Single-label-before-suffix is intentional: real
#: homelab hosts usually have just one (``macbook.lan``,
#: ``server.local``).
_INTERNAL_HOST_RE = re.compile(
    r"\b[a-z0-9][a-z0-9-]*\.(?:local|lan|home|internal|corp|test|dev)\b",
    re.IGNORECASE,
)

#: IPv4 addresses. Both RFC1918 private and public — operators may
#: care about both.
_IPV4_RE = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
)

#: IPv6 addresses. Compressed + full forms. Less common in real
#: traces, but cheap to add.
_IPV6_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b"
)


# ─── public API ──────────────────────────────────────────────────────


def redact_pii(text: str) -> str:
    """Always-on PII sweep. Credit cards, SSNs, emails, phones."""
    if not text:
        return text
    text = _CREDIT_CARD_RE.sub(REDACTED_PII, text)
    text = _SSN_RE.sub(REDACTED_PII, text)
    text = _EMAIL_RE.sub(REDACTED_PII, text)
    text = _PHONE_RE.sub(REDACTED_PII, text)
    return text


def redact_secrets(text: str) -> str:
    """API keys, bearer tokens, password-shaped strings."""
    if not text:
        return text
    for pattern in _API_KEY_RES:
        text = pattern.sub(REDACTED_KEY, text)
    return text


def redact_paths(text: str) -> str:
    """Absolute / tilde / Windows file paths."""
    if not text:
        return text
    return _PATH_RE.sub(REDACTED_PATH, text)


def redact_hostnames(text: str) -> str:
    """URLs and internal-shaped hostnames + IPs.

    Splits the work into URL replacement (whole-URL → sentinel) and
    bare-host replacement so a URL like ``https://nas.lan/share``
    becomes one sentinel, not three.
    """
    if not text:
        return text
    text = _URL_RE.sub(REDACTED_HOST, text)
    text = _INTERNAL_HOST_RE.sub(REDACTED_HOST, text)
    text = _IPV4_RE.sub(REDACTED_HOST, text)
    text = _IPV6_RE.sub(REDACTED_HOST, text)
    return text


def apply_caller_filter(
    text: str,
    sensitive_filter: Callable[[str], bool] | None,
) -> str:
    """Run the operator-supplied filter against the whole body.

    Returns ``REDACTED`` (the whole-body sentinel) if the filter
    flags the text as sensitive. A filter that raises is treated as
    "yes, sensitive" — caller-supplied bugs must NEVER let raw
    content through.
    """
    if sensitive_filter is None or not text:
        return text
    try:
        if sensitive_filter(text):
            return REDACTED
    except Exception:  # noqa: BLE001
        _log.warning(
            "social-traces: sensitive_filter raised — redacting body",
            exc_info=True,
        )
        return REDACTED
    return text


def redact(
    text: str,
    *,
    redact_paths_layer: bool = True,
    redact_hostnames_layer: bool = True,
    sensitive_filter: Callable[[str], bool] | None = None,
) -> str:
    """Full redaction pipeline.

    Order matters:

    1. Caller filter — whole-body collapse.
    2. PII (always on) — credit cards, SSNs, emails, phones.
    3. Secrets (always on) — API keys, bearer tokens.
    4. Paths (opt-in) — file paths.
    5. Hostnames + IPs (opt-in) — URLs, internal hosts, IP addresses.

    The opt-in layers default to ON (see
    :class:`SocialTracesConfig.privacy`). Operators who want to share
    file paths or hostnames must explicitly opt out.
    """
    text = apply_caller_filter(text, sensitive_filter)
    if text == REDACTED:
        return text  # short-circuit — already collapsed
    text = redact_pii(text)
    text = redact_secrets(text)
    if redact_paths_layer:
        text = redact_paths(text)
    if redact_hostnames_layer:
        text = redact_hostnames(text)
    return text


def is_useful_body(text: str, *, min_chars: int = 20) -> bool:
    """Reject bodies that are empty, sentinel-only, or too short.

    Used by the distiller to decide whether to ship a TraceCard or
    discard it: a body that's entirely sentinels means redaction
    nuked the content, which means the original was sensitive,
    which means we should NOT submit anything for this session.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False

    no_sentinels = stripped
    for sentinel in (
        REDACTED, REDACTED_PII, REDACTED_PATH, REDACTED_HOST, REDACTED_KEY,
    ):
        no_sentinels = no_sentinels.replace(sentinel, "")
    no_sentinels = re.sub(r"\s+", " ", no_sentinels).strip()
    return len(no_sentinels) >= min_chars


__all__ = [
    "REDACTED",
    "REDACTED_HOST",
    "REDACTED_KEY",
    "REDACTED_PATH",
    "REDACTED_PII",
    "apply_caller_filter",
    "is_useful_body",
    "redact",
    "redact_hostnames",
    "redact_paths",
    "redact_pii",
    "redact_secrets",
]
