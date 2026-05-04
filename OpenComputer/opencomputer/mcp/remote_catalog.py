"""Remote MCP catalog fetch + cache (Phase 12m partial — T2 of mcp-deferrals-v2).

OC's bundled :mod:`opencomputer.mcp.presets` ships a hardcoded list of
servers. This module fetches a community-maintained catalog from a
known URL with a 24h local cache. Falls back to the cached copy on
network failure (warn + serve stale rather than fail closed).

Install-from-remote (mapping a fetched entry into ``MCPServerConfig``
without bundled :class:`~opencomputer.mcp.presets.Preset` plumbing) is
intentionally out-of-scope for v1 — needs version pinning + checksum
validation. This module ships the FETCH + CACHE + DISPLAY surface.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: URL of the canonical machine-parseable catalog. Replaceable for tests.
_CATALOG_URL = (
    "https://raw.githubusercontent.com/sakshamzip2-sys/opencomputer/"
    "main/OpenComputer/data/mcp_catalog.json"
)

#: Local cache path. Default lives under the user's profile home;
#: tests override via ``monkeypatch.setattr(remote_catalog, "_CACHE_PATH", ...)``.
_CACHE_PATH: Path = Path.home() / ".opencomputer" / "mcp_catalog_cache.json"

#: Cache TTL in seconds. 24h.
_CACHE_TTL_SECONDS = 24 * 60 * 60


class CatalogFetchError(RuntimeError):
    """Raised when the catalog can't be fetched and no cache exists."""


def _cache_is_fresh(path: Path) -> bool:
    """True if the cache file exists and was modified within TTL."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return False
    return (time.time() - mtime) < _CACHE_TTL_SECONDS


def _read_cache(path: Path) -> dict[str, Any] | None:
    """Return parsed cache data, or None if missing/corrupted."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    """Atomically write the catalog data to the cache path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def fetch_catalog(*, refresh: bool = False, url: str | None = None) -> dict[str, Any]:
    """Return the catalog JSON. Hits cache first when fresh; fetches otherwise.

    Args:
        refresh: When True, bypass the cache and force a network fetch
            (still falls back to cache on network failure).
        url: Override the fetch URL. Default :data:`_CATALOG_URL`.

    Returns:
        Parsed catalog dict.

    Raises:
        CatalogFetchError: When network fetch fails AND no cache is
            available (corrupted or missing).
    """
    target_url = url or _CATALOG_URL

    if not refresh and _cache_is_fresh(_CACHE_PATH):
        cached = _read_cache(_CACHE_PATH)
        if cached is not None:
            logger.debug("MCP catalog: using fresh cache (%s)", _CACHE_PATH)
            return cached
        # Cache is corrupted; fall through to network fetch.

    try:
        response = httpx.get(target_url, timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        _write_cache(_CACHE_PATH, data)
        logger.info("MCP catalog: refreshed from %s", target_url)
        return data
    except Exception as fetch_exc:  # noqa: BLE001 — network or parse
        # Fall back to whatever cache exists, even if stale.
        cached = _read_cache(_CACHE_PATH)
        if cached is not None:
            logger.warning(
                "MCP catalog: fetch failed (%s); serving stale cache",
                fetch_exc,
            )
            return cached
        raise CatalogFetchError(
            f"failed to fetch MCP catalog from {target_url} "
            f"and no cache available: {fetch_exc}"
        ) from fetch_exc


def format_catalog_for_display(data: dict[str, Any]) -> str:
    """Render the catalog as a human-readable string for the CLI.

    Each server entry takes 2 lines: slug + description, then optionally
    a third line for required_env. Empty catalogs return a placeholder.
    """
    servers = data.get("servers", []) or []
    if not servers:
        return "(catalog is empty — no servers listed)"

    lines: list[str] = []
    for srv in servers:
        slug = srv.get("slug", "?")
        description = srv.get("description", "")
        lines.append(f"  {slug}")
        if description:
            lines.append(f"    {description}")
        env = srv.get("required_env") or []
        if env:
            lines.append(f"    requires: {', '.join(env)}")
        lines.append("")  # blank separator
    return "\n".join(lines).rstrip()


__all__ = [
    "CatalogFetchError",
    "fetch_catalog",
    "format_catalog_for_display",
]
