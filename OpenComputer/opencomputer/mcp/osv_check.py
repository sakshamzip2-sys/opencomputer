"""
OSV malware / vulnerability pre-flight scan for MCP server packages
(Round 2B P-7).

Before spawning a stdio MCP server via ``npx``/``uvx``, the launcher
calls :func:`check_package` to look up the package in the public
`OSV.dev <https://osv.dev>`_ vulnerability database. Hits are surfaced
to the bus via the ``mcp_security.osv_hit`` event so audit /
trajectory subscribers can record what was caught; depending on the
``MCPConfig.osv_check_fail_closed`` flag the launcher then either
refuses the spawn (fail-closed) or logs a warning and proceeds
(fail-open, default).

Cache
-----

OSV results are cached in ``~/.opencomputer/cache/osv.json`` for 24 h.
Two concerns drive the cache:

* **Rate limits.** OSV's public endpoint is generous but not unlimited;
  a noisy fleet of agents shouldn't hammer it on every server start.
* **Offline use.** Once cached, subsequent agent starts can clear
  this check without network connectivity.

The cache directory is created with mode ``0o700`` so the JSON file —
which contains nothing secret today but could in a future revision —
isn't world-readable.

Failure mode
------------

Network failures (timeout, connection refused, non-2xx response) are
treated as **fail-open by default**: an empty ``vulns`` list is
returned, a warning is logged, and the caller proceeds. This is the
correct posture for a pre-flight enrichment check — an OSV outage
should not break MCP startup. Operators who want strict behaviour set
``MCPConfig.osv_check_fail_closed = True``; the launcher then refuses
to spawn whenever a HIGH-severity hit is found, but a network error
itself still returns empty (the enrichment failed, not the package).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

_log = logging.getLogger("opencomputer.mcp.osv_check")

#: Default cache file path. Lazily resolved per-call so tests can override
#: ``HOME`` / ``OPENCOMPUTER_HOME`` cleanly via ``monkeypatch.setenv``.
_CACHE_TTL_S: int = 24 * 3600
_OSV_QUERY_URL: str = "https://api.osv.dev/v1/query"
_REQUEST_TIMEOUT_S: float = 5.0


def _cache_path() -> Path:
    """Resolve the cache file path lazily so test isolation works."""
    return Path.home() / ".opencomputer" / "cache" / "osv.json"


def _load_cache() -> dict[str, Any]:
    """Return the persisted cache dict, or an empty dict on any failure.

    Returning an empty dict on parse errors is intentional — a corrupted
    cache should never break MCP startup. The next successful check
    will overwrite the file.
    """
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("osv cache unreadable (%s); ignoring", exc)
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    """Persist ``cache`` to disk, creating the parent dir at mode 0700.

    Permission errors are logged but never raised — callers must be
    able to proceed even when the cache write fails.
    """
    path = _cache_path()
    try:
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        # Tighten the dir mode after creation; mkdir(mode=) is masked
        # by umask on most systems so we can't rely on it alone.
        try:
            os.chmod(parent, 0o700)
        except OSError:
            # Best-effort — filesystems without POSIX modes (e.g. some
            # network mounts) will silently no-op. Not worth raising.
            pass
        with path.open("w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    except OSError as exc:
        _log.warning("osv cache write failed (%s); continuing", exc)


def _query_osv(name: str, ecosystem: str) -> list[dict[str, Any]]:
    """Hit OSV's ``/v1/query`` endpoint. Returns the raw vulns list.

    Network / HTTP errors propagate to the caller — wrapped in
    :func:`check_package` to translate into the fail-open behaviour
    documented in the module docstring.
    """
    payload = {"package": {"name": name, "ecosystem": ecosystem}}
    response = httpx.post(_OSV_QUERY_URL, json=payload, timeout=_REQUEST_TIMEOUT_S)
    response.raise_for_status()
    body = response.json()
    vulns = body.get("vulns", []) if isinstance(body, dict) else []
    return list(vulns) if isinstance(vulns, list) else []


def check_package(name: str, ecosystem: str = "npm") -> dict[str, Any]:
    """Look up ``name`` in OSV; return ``{vulns, cached, cached_at}``.

    Parameters
    ----------
    name:
        Package name as published on the registry (e.g.
        ``"@modelcontextprotocol/server-filesystem"`` for npm or
        ``"mcp-server-fetch"`` for PyPI).
    ecosystem:
        OSV ecosystem identifier. Defaults to ``"npm"``; use
        ``"PyPI"`` for ``uvx``-launched servers.

    Returns
    -------
    dict
        ``{"vulns": [...], "cached": bool, "cached_at": float}``. The
        ``vulns`` list is the raw OSV ``vulns`` array — empty when the
        package is clean OR when the network lookup failed (fail-open).
        ``cached`` is ``True`` when a fresh-enough cache entry was
        used; ``cached_at`` is the unix epoch when the entry was
        recorded (or the current time on a fresh fetch).

    The function never raises — network failures degrade to a clean
    result + warning log. Callers that want strict behaviour read the
    ``vulns`` list themselves and decide.
    """
    key = f"{ecosystem}:{name}"
    cache = _load_cache()
    now = time.time()

    entry = cache.get(key)
    if isinstance(entry, dict):
        cached_at = entry.get("cached_at")
        vulns = entry.get("vulns")
        if (
            isinstance(cached_at, int | float)
            and isinstance(vulns, list)
            and (now - float(cached_at)) < _CACHE_TTL_S
        ):
            return {
                "vulns": list(vulns),
                "cached": True,
                "cached_at": float(cached_at),
            }

    # Cache miss / stale → re-query OSV. Wrap the network call so any
    # transport-level failure degrades to fail-open instead of breaking
    # the spawning agent.
    try:
        vulns = _query_osv(name, ecosystem)
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as exc:
        _log.warning(
            "osv lookup failed for %s/%s (%s); fail-open",
            ecosystem,
            name,
            exc,
        )
        return {"vulns": [], "cached": False, "cached_at": now}

    cache[key] = {"vulns": vulns, "cached_at": now}
    _save_cache(cache)

    return {"vulns": list(vulns), "cached": False, "cached_at": now}


def has_high_severity(vulns: list[dict[str, Any]]) -> bool:
    """Return ``True`` if any entry in ``vulns`` is HIGH/CRITICAL severity.

    OSV reports severity in two places: the top-level ``severity``
    list (CVSS vectors) and the ``database_specific.severity`` string
    used by some ecosystems (npm advisories use ``"high"``,
    ``"critical"``, etc.). We check both.
    """
    high_levels = {"HIGH", "CRITICAL"}
    for vuln in vulns:
        if not isinstance(vuln, dict):
            continue
        # Top-level severity[] (CVSS scores) — score >= 7.0 is High
        # per the CVSS v3 spec; OSV often surfaces this as a vector
        # string we can't reason about cheaply, so we fall back on
        # the database-specific label when present.
        db_specific = vuln.get("database_specific")
        if isinstance(db_specific, dict):
            label = db_specific.get("severity")
            if isinstance(label, str) and label.upper() in high_levels:
                return True
        # Some advisories nest severity inside "affected[].database_specific"
        for affected in vuln.get("affected", []) or []:
            if not isinstance(affected, dict):
                continue
            db_aff = affected.get("database_specific")
            if isinstance(db_aff, dict):
                label = db_aff.get("severity")
                if isinstance(label, str) and label.upper() in high_levels:
                    return True
    return False


__all__ = ["check_package", "has_high_severity"]
