"""Website blocklist — config-driven domain refusal for URL-capable tools.

Hermes calls this the "Website Blocklist" feature. It complements (does
NOT replace) :mod:`opencomputer.security.url_safety`:

- ``url_safety.is_safe_url`` is a SECURITY check (RFC 1918, loopback,
  cloud metadata, link-local).
- This module is a POLICY check (org-defined: e.g., "agents must not
  fetch admin.company.com").

Tools call ``is_safe_url`` first (security), then ``is_blocked``
(policy). Order matters: a private-network URL is refused even if no
policy rule matches; a policy-blocked public URL is refused even if it
passes SSRF checks.

Rule grammar (one rule per line in shared files; same in config list):

* ``admin.example.com``      — exact host match
* ``*.internal.company.com`` — subdomain wildcard (matches the bare
  domain too: ``internal.company.com``, ``api.internal.company.com``)
* ``*.local``                — TLD wildcard (matches any host ending
  in ``.local``)
* ``# foo``                  — comment line (skipped)
* blank line                 — skipped

Shared-file behaviour mirrors Hermes spec: missing or unreadable files
log a warning and are skipped; explicit ``domains`` still apply.

Caching: ``load_policy_cached`` exposes a 30-second TTL. Tools call it
on the hot path; ops can edit the rule files without a restart.

Config schema independence: ``policy_from_active_config`` reads the
profile's ``config.yaml`` directly via PyYAML rather than going through
the ``SecurityConfig`` dataclass. This keeps the module independent of
unrelated schema changes (e.g., concurrent PRs adding sibling
``security.*`` keys).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("opencomputer.security.website_blocklist")

POLICY_CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class WebsiteBlocklistPolicy:
    """Resolved policy — what tools call ``is_blocked`` against.

    Attributes:
        enabled: master switch. ``False`` makes ``is_blocked`` always
            return ``False`` regardless of ``domains`` / ``shared_files``.
        domains: tuple of rule strings (exact / ``*.<sub>`` / ``*.<tld>``).
            Mirrors ``security.website_blocklist.domains`` config.
        shared_files: extra files containing rules, one per line.
            Mirrors ``security.website_blocklist.shared_files`` config.
            Read each call to ``is_blocked`` (the 30s cache wraps the
            full policy load).
    """

    enabled: bool
    domains: tuple[str, ...]
    shared_files: tuple[Path, ...]


def parse_rules(text: str) -> tuple[str, ...]:
    """Parse a rule file's text into a tuple of rule strings.

    Strips ``#``-prefixed comments and blank lines.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return tuple(out)


def _load_shared_file_rules(path: Path) -> tuple[str, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(
            "website_blocklist: shared file %s unreadable: %s — skipping",
            path,
            e,
        )
        return ()
    return parse_rules(text)


def _matches_rule(host: str, rule: str) -> bool:
    """Return True if ``host`` matches ``rule``.

    ``host`` is the lowercase hostname extracted from the URL. ``rule``
    is one of: exact host, ``*.<suffix>``.
    """
    if rule.startswith("*."):
        suffix = rule[2:]
        return host == suffix or host.endswith("." + suffix)
    return host == rule


def is_blocked(url: str, policy: WebsiteBlocklistPolicy) -> bool:
    """Return True if ``url`` matches any rule in ``policy``.

    Hot path; called per URL fetch. ``policy`` is a snapshot — caller
    is expected to use ``load_policy_cached`` to amortise file reads.
    An invalid URL or missing host returns ``False`` (no rule can
    match).
    """
    if not policy.enabled:
        return False
    host = ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, TypeError):
        host = ""
    if not host:
        return False
    for rule in policy.domains:
        if _matches_rule(host, rule):
            return True
    # Re-read shared files each call; the 30 s policy cache amortises this.
    for f in policy.shared_files:
        for rule in _load_shared_file_rules(f):
            if _matches_rule(host, rule):
                return True
    return False


# ── 30-second cache for the resolved policy ───────────────────────────


@dataclass(slots=True)
class _CacheEntry:
    policy: WebsiteBlocklistPolicy
    expires_at: float


_cache_lock = threading.Lock()
_cache: dict[int, _CacheEntry] = {}


def load_policy_cached(
    *,
    enabled: bool,
    domains: tuple[str, ...],
    shared_files: tuple[Path, ...],
    now: float | None = None,
) -> WebsiteBlocklistPolicy:
    """Return a cached :class:`WebsiteBlocklistPolicy` for the given inputs.

    Cache key is the tuple of inputs — different config produces a
    different entry. TTL is 30 seconds (mirrors Hermes spec).
    Thread-safe.

    ``now`` is for testing — production callers omit it.
    """
    now = now if now is not None else time.monotonic()
    key = hash((enabled, domains, tuple(map(str, shared_files))))
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and entry.expires_at > now:
            return entry.policy
        policy = WebsiteBlocklistPolicy(
            enabled=enabled, domains=domains, shared_files=shared_files,
        )
        _cache[key] = _CacheEntry(
            policy=policy, expires_at=now + POLICY_CACHE_TTL_SECONDS,
        )
        return policy


def clear_cache_for_tests() -> None:
    """Test helper — drop all cached policies."""
    with _cache_lock:
        _cache.clear()


def policy_from_active_config() -> WebsiteBlocklistPolicy:
    """Read the policy from the active profile's ``config.yaml``.

    Goes through PyYAML directly rather than the central
    ``SecurityConfig`` dataclass — this keeps the module independent of
    unrelated config-schema changes (e.g., concurrent PRs adding
    sibling ``security.*`` keys). On any error (missing file, bad
    YAML, missing section), returns a disabled policy — fail-open is
    correct for the policy layer because the security layer
    (``is_safe_url``) still applies.
    """
    try:
        import yaml

        from opencomputer.profiles import (
            profile_home_dir,
            read_active_profile,
        )

        prof = read_active_profile()
        if prof is None:
            return load_policy_cached(
                enabled=False, domains=(), shared_files=(),
            )
        config_path = profile_home_dir(prof) / "config.yaml"
        if not config_path.exists():
            return load_policy_cached(
                enabled=False, domains=(), shared_files=(),
            )
        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        wbl = (data.get("security") or {}).get("website_blocklist") or {}
        return load_policy_cached(
            enabled=bool(wbl.get("enabled", False)),
            domains=tuple(wbl.get("domains") or ()),
            shared_files=tuple(Path(s) for s in (wbl.get("shared_files") or ())),
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "website_blocklist: config load failed (%s) — disabled", e,
        )
        return load_policy_cached(
            enabled=False, domains=(), shared_files=(),
        )


__all__ = [
    "POLICY_CACHE_TTL_SECONDS",
    "WebsiteBlocklistPolicy",
    "clear_cache_for_tests",
    "is_blocked",
    "load_policy_cached",
    "parse_rules",
    "policy_from_active_config",
]
