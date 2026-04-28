"""Runtime PII / secrets redaction — single-call sweep.

Hermes Tier 3.D port. Promotes the trajectory-export-only redaction in
:mod:`opencomputer.evolution.redaction` to a runtime-callable security
primitive accessible everywhere in the codebase.

Use it at any **boundary** where assistant text or tool output may
cross into a system that retains it (logs, cross-provider session
export, context compression input, ACP transport).

Two public functions::

    >>> from opencomputer.security.redact import redact_runtime_text
    >>> redact_runtime_text("contact me at saksham@example.com")
    'contact me at <EMAIL_REDACTED>'
    >>> from opencomputer.security.redact import redact_runtime_text_with_counts
    >>> redact_runtime_text_with_counts("Bearer abc123 + sk-ant-secret")
    ('Bearer <REDACTED> + <ANTHROPIC_KEY_REDACTED>', {'bearer': 1, 'anthropic': 1, ...})

By default redaction is **enabled**. Disable with::

    OC_REDACT_RUNTIME=false

The env var is **snapshotted at import** so a runtime LLM-generated
``os.environ["OC_REDACT_RUNTIME"]="false"`` cannot disable it
mid-session. To turn redaction off, set the env var **before** the
process starts.

Patterns covered (see ``_PATTERNS`` below):

- API keys (multi-vendor): Anthropic, OpenAI, AWS, GitHub PAT, Slack
  bot/personal, Telegram bot, Google AI, Perplexity, Firecrawl,
  Hugging Face, Replicate, npm, PyPI, DigitalOcean, Groq, Tavily,
  Exa, Synapse, Mem0, SendGrid, Bigcommerce, generic ``sk-`` /
  ``Bearer`` shapes
- JSON / env sensitive field assignments: ``"apiKey": "..."``,
  ``API_KEY=...``, ``password=...``, etc.
- PEM private keys (``-----BEGIN ... PRIVATE KEY-----`` blocks)
- DB connection strings (``postgres://user:pass@host``,
  ``mysql://``, ``mongodb://``, ``redis://``, ``amqp://``)
- JSON Web Tokens (``eyJ...``)
- URL query params: ``?access_token=``, ``?api_key=``,
  ``?client_secret=``, etc.
- URL userinfo for non-DB schemes (``https://user:pass@host``)
- File paths under ``/Users/<name>/``
- Email addresses
- IPv4 addresses (loopback skipped)
- Discord mentions (``<@123456789012345678>``)
- E.164 phone numbers (``+<7-15 digits>``)

Order matters. More-specific patterns precede generic ones so we
don't double-replace.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

# ---------------------------------------------------------------------------
# Import-time kill switch — snapshot, NOT runtime-checked
# ---------------------------------------------------------------------------
# CRITICAL: snapshot at import. A runtime mutation of os.environ cannot
# disable redaction mid-session. To turn off, set the env var before the
# process starts.
_REDACT_ENABLED: bool = os.environ.get("OC_REDACT_RUNTIME", "").lower() not in (
    "0",
    "false",
    "no",
    "off",
)


# ---------------------------------------------------------------------------
# Pattern catalog
# ---------------------------------------------------------------------------
#
# Tuples of (regex, replacement, count_key). Order matters — most-specific
# vendor prefixes first so labels are accurate (e.g. anthropic before
# generic sk-).

_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    # ---- Vendor-specific API keys (specific labels) ----
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "<ANTHROPIC_KEY_REDACTED>", "anthropic"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<AWS_KEY_REDACTED>", "aws"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{35,}"), "<GOOGLE_AI_KEY_REDACTED>", "google_ai"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "<GITHUB_PAT_REDACTED>", "github_pat"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "<GITHUB_PAT_REDACTED>", "github_pat"),
    (re.compile(r"xox[bpaprsu]-[A-Za-z0-9\-]{20,}"), "<SLACK_TOKEN_REDACTED>", "slack"),
    (re.compile(r"\b\d{8,}:[A-Za-z0-9_\-]{20,}\b"), "<TELEGRAM_TOKEN_REDACTED>", "telegram"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{40,}\b"), "<GROQ_KEY_REDACTED>", "groq"),
    (re.compile(r"\btvly-[A-Za-z0-9_\-]{20,}\b"), "<TAVILY_KEY_REDACTED>", "tavily"),
    (re.compile(r"\bexa_[A-Za-z0-9_\-]{20,}\b"), "<EXA_KEY_REDACTED>", "exa"),
    (re.compile(r"\bpplx-[A-Za-z0-9]{40,}\b"), "<PERPLEXITY_KEY_REDACTED>", "perplexity"),
    (re.compile(r"\bfc-[A-Za-z0-9]{20,}\b"), "<FIRECRAWL_KEY_REDACTED>", "firecrawl"),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"), "<HF_TOKEN_REDACTED>", "huggingface"),
    (re.compile(r"\br8_[A-Za-z0-9]{30,}\b"), "<REPLICATE_TOKEN_REDACTED>", "replicate"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"), "<NPM_TOKEN_REDACTED>", "npm"),
    (re.compile(r"\bpypi-[A-Za-z0-9_\-]{30,}\b"), "<PYPI_TOKEN_REDACTED>", "pypi"),
    (re.compile(r"\bdop_v1_[A-Za-z0-9]{40,}\b"), "<DIGITALOCEAN_TOKEN_REDACTED>", "digitalocean"),
    (re.compile(r"\bSG\.[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{30,}\b"), "<SENDGRID_KEY_REDACTED>", "sendgrid"),
    (re.compile(r"\bsk_live_[A-Za-z0-9]{20,}\b"), "<STRIPE_LIVE_KEY_REDACTED>", "stripe_live"),
    (re.compile(r"\bsk_test_[A-Za-z0-9]{20,}\b"), "<STRIPE_TEST_KEY_REDACTED>", "stripe_test"),
    (re.compile(r"\bbb_live_[A-Za-z0-9]{20,}\b"), "<BIGCOMMERCE_KEY_REDACTED>", "bigcommerce"),
    (re.compile(r"\bsyt_[A-Za-z0-9_\-]{20,}\b"), "<SYNAPSE_TOKEN_REDACTED>", "synapse"),
    (re.compile(r"\bmem0_[A-Za-z0-9_\-]{20,}\b"), "<MEM0_KEY_REDACTED>", "mem0"),
    # Generic OpenAI-style ``sk-...`` (≥20 chars after the prefix). Excludes
    # specific vendor sk- prefixes already matched above.
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "<OPENAI_KEY_REDACTED>", "openai"),
    # ---- PEM private keys (multi-line block) ----
    (
        re.compile(r"-----BEGIN[A-Z ]+PRIVATE KEY-----[\s\S]*?-----END[A-Z ]+PRIVATE KEY-----"),
        "<PEM_PRIVATE_KEY_REDACTED>",
        "pem",
    ),
    # ---- JWTs (eyJ...) ----
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
        "<JWT_REDACTED>",
        "jwt",
    ),
    # ---- DB connection strings with embedded credentials ----
    # Example: postgres://user:password@host:5432/db
    (
        re.compile(r"\b(postgres|postgresql|mysql|mongodb|redis|amqp)(\+[a-z]+)?://[^/\s:]+:[^@\s]+@[^\s/?#]+", re.IGNORECASE),
        r"\1://<DB_CREDENTIALS_REDACTED>",
        "db_connection",
    ),
    # ---- URL userinfo for non-DB schemes (https://user:pass@host) ----
    (
        re.compile(r"\b(https?|ftp|ws|wss)://[^/\s:]+:[^@\s]+@", re.IGNORECASE),
        r"\1://<URL_USERINFO_REDACTED>@",
        "url_userinfo",
    ),
    # ---- URL query params with sensitive names ----
    # Must precede env_assignment, JSON-field, and bearer patterns so it wins
    # against substring overlaps in URLs like `?access_token=...`.
    (
        re.compile(
            r"([?&](?:access_token|refresh_token|id_token|token|api_key|apikey|client_secret|password|auth|jwt|session|secret|key|code|signature|x-amz-signature)=)([^&\s\"']+)",
            re.IGNORECASE,
        ),
        r"\1<URL_PARAM_REDACTED>",
        "url_param",
    ),
    # ---- Bearer tokens (Authorization header style) ----
    (re.compile(r"Bearer\s+[A-Za-z0-9_\-\.~+/]+"), "Bearer <REDACTED>", "bearer"),
    # ---- JSON sensitive field assignments ----
    # Conservative — matches only when the *value* is a string. Won't match
    # `"password": null` or numeric values (those aren't credentials anyway).
    (
        re.compile(
            r'("(?:apiKey|api_key|token|secret|password|access_token|refresh_token|auth_token|bearer|secret_value|raw_secret|secret_input|key_material|client_secret|private_key)"\s*:\s*)"[^"]*"',
            re.IGNORECASE,
        ),
        r'\1"<JSON_FIELD_REDACTED>"',
        "json_field",
    ),
    # ---- Env-var-style assignments (KEY=value) ----
    # Anchored to whitespace/start-of-string at the left so URL query strings
    # (`?access_token=…`, `&password=…`) don't trigger this — they're caught
    # by the more-specific url_param pattern above.
    (
        re.compile(
            r"(?:^|[\s;])((?:[A-Z][A-Z0-9_]*_)?(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH))\s*=\s*([^\s\"'$]+)",
            re.IGNORECASE,
        ),
        r" \1=<ENV_VALUE_REDACTED>",
        "env_assignment",
    ),
    # ---- Discord mentions ----
    (re.compile(r"<@!?\d{15,20}>"), "<DISCORD_MENTION_REDACTED>", "discord_mention"),
    # ---- File paths under /Users/<name>/ (preserves trailing path) ----
    (re.compile(r"/Users/[^/\s]+/"), "/Users/REDACTED/", "file_path"),
    # ---- Email addresses ----
    (
        re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"),
        "<EMAIL_REDACTED>",
        "email",
    ),
    # ---- E.164 phone numbers (+<7-15 digits>, optional separators) ----
    (
        re.compile(r"\+\d{1,3}[\s\-]?\d{2,4}[\s\-]?\d{2,4}[\s\-]?\d{2,5}\b"),
        "<PHONE_REDACTED>",
        "phone",
    ),
    # ---- IPv4 addresses (loopback skipped via post-match check below) ----
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "<IP_REDACTED>", "ipv4"),
]

# IPs we don't redact (debug paths still meaningful)
_IP_SAFE: set[str] = {"127.0.0.1", "0.0.0.0", "::1"}


def _apply_pattern(text: str, pat: re.Pattern[str], repl: str, key: str) -> tuple[str, int]:
    """Apply one pattern; return (new_text, count). Skip safe IPs."""
    if key == "ipv4":
        count = 0

        def _ip_sub(m: re.Match[str]) -> str:
            nonlocal count
            if m.group(0) in _IP_SAFE:
                return m.group(0)
            count += 1
            return repl

        out = pat.sub(_ip_sub, text)
        return out, count

    # Standard substitution path. Capture group references (\1) work via re.sub.
    new_text, n = pat.subn(repl, text)
    return new_text, n


def redact_runtime_text(text: str) -> str:
    """Apply the full pattern sweep and return the redacted text.

    No-op if the kill-switch is disabled (``OC_REDACT_RUNTIME=false`` set
    BEFORE process start). Idempotent — running twice yields the same
    result as running once.
    """
    if not _REDACT_ENABLED or not text:
        return text
    out = text
    for pat, repl, key in _PATTERNS:
        out, _ = _apply_pattern(out, pat, repl, key)
    return out


def redact_runtime_text_with_counts(text: str) -> tuple[str, dict[str, int]]:
    """Same sweep as :func:`redact_runtime_text` but returns the per-category
    redaction counts. Useful for diagnostics + tests.

    Counts dict has every category key, even if zero. This makes the
    output predictable for diff-based test assertions.
    """
    counts: dict[str, int] = {}
    if not _REDACT_ENABLED or not text:
        # Still return all-zero counts dict so callers can rely on the keys.
        for _, _, key in _PATTERNS:
            counts.setdefault(key, 0)
        return text, counts

    out = text
    for pat, repl, key in _PATTERNS:
        out, n = _apply_pattern(out, pat, repl, key)
        counts[key] = counts.get(key, 0) + n
    return out, counts


def redact_runtime_mapping(data: Mapping[str, object]) -> dict[str, object]:
    """Recursively redact a JSON-serializable mapping. String values run
    through the pattern sweep; nested dicts/lists are walked.

    Useful when redacting structured logs / tool args before they're
    serialized to disk or transport.
    """
    return _walk(data)  # type: ignore[return-value]


def _walk(node: object) -> object:
    if isinstance(node, str):
        return redact_runtime_text(node)
    if isinstance(node, Mapping):
        return {k: _walk(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v) for v in node]
    if isinstance(node, tuple):
        return tuple(_walk(v) for v in node)
    return node


def is_enabled() -> bool:
    """Whether runtime redaction is enabled (snapshotted at import)."""
    return _REDACT_ENABLED


__all__ = [
    "redact_runtime_text",
    "redact_runtime_text_with_counts",
    "redact_runtime_mapping",
    "is_enabled",
]
