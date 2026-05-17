"""Per-plugin update checking (best-of-three Recipe 10).

``cli_update_check`` tells the user when *OpenComputer itself* has an
update; this does the same for *installed plugins*. It compares each
catalog-installed plugin's version (from ``.installed_index.json``)
against the version currently advertised in the catalogs, and caches
the result for 6h so a session start never pays repeated network cost.

The comparison core — :func:`compute_updates` — is a pure function
(installed records + a slug→version map in, update list out), so it is
trivially testable without any network. The CLI builds the version map
by fetching catalogs; this module never reaches the network itself.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from opencomputer.plugins.installed_index import InstalledRecord

log = logging.getLogger(__name__)

#: Same 6h TTL as the host-update checker — a plugin release cadence
#: never needs finer-grained polling.
CACHE_TTL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True, slots=True)
class PluginUpdate:
    """One installed plugin that has a newer version available."""

    plugin_id: str
    installed_version: str
    available_version: str
    source_url: str


def _is_newer(installed: str, available: str) -> bool:
    """True iff ``available`` is a strictly higher version.

    Falls back to a string inequality when either value is not a valid
    PEP 440 version — better to surface a maybe-update than to silently
    miss one because a plugin used an odd version string.
    """
    from packaging.version import InvalidVersion, Version

    inst = (installed or "").strip()
    avail = (available or "").strip()
    if not avail:
        return False
    try:
        return Version(avail) > Version(inst)
    except InvalidVersion:
        return bool(inst) and avail != inst


def compute_updates(
    records: list[InstalledRecord],
    catalog_versions: dict[str, str],
) -> list[PluginUpdate]:
    """Pure update diff.

    ``catalog_versions`` maps a catalog slug / plugin id to the version
    the catalog currently advertises. Only ``source == "catalog"``
    records are checked — git / url / pypi installs carry no comparable
    version metadata in the index and are reported as "unsupported" by
    the CLI rather than guessed at here.
    """
    updates: list[PluginUpdate] = []
    for rec in records:
        if rec.source != "catalog":
            continue
        available = catalog_versions.get(rec.plugin_id) or catalog_versions.get(
            rec.source_url
        )
        if available and _is_newer(rec.version, available):
            updates.append(
                PluginUpdate(
                    plugin_id=rec.plugin_id,
                    installed_version=rec.version,
                    available_version=available,
                    source_url=rec.source_url,
                )
            )
    return updates


def cache_path() -> Path:
    """Where the plugin-update cache lives (per profile)."""
    from opencomputer.agent.config import _home

    return _home() / "plugin_update_cache.json"


def read_cache(
    path: Path | None = None, *, now: float | None = None
) -> list[PluginUpdate] | None:
    """Return the cached update list if it is still fresh, else ``None``.

    A missing, malformed, or stale cache all return ``None`` — the
    caller then does a network refresh.
    """
    p = path or cache_path()
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ts = raw.get("checked_at", 0) if isinstance(raw, dict) else 0
    now_ts = now if now is not None else time.time()
    if not isinstance(ts, (int, float)) or now_ts - ts > CACHE_TTL_SECONDS:
        return None
    entries = raw.get("updates", [])
    if not isinstance(entries, list):
        return None
    out: list[PluginUpdate] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        out.append(
            PluginUpdate(
                plugin_id=str(e.get("plugin_id", "")),
                installed_version=str(e.get("installed_version", "")),
                available_version=str(e.get("available_version", "")),
                source_url=str(e.get("source_url", "")),
            )
        )
    return out


def write_cache(
    updates: list[PluginUpdate],
    path: Path | None = None,
    *,
    now: float | None = None,
) -> None:
    """Atomically persist the update list with a check timestamp."""
    p = path or cache_path()
    payload = {
        "checked_at": int(now if now is not None else time.time()),
        "updates": [
            {
                "plugin_id": u.plugin_id,
                "installed_version": u.installed_version,
                "available_version": u.available_version,
                "source_url": u.source_url,
            }
            for u in updates
        ],
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log.warning("plugin update cache write failed: %s", exc)


__all__ = [
    "CACHE_TTL_SECONDS",
    "PluginUpdate",
    "cache_path",
    "compute_updates",
    "read_cache",
    "write_cache",
]
