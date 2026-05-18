"""Named plugin marketplaces (best-of-three Recipe 5).

Before this, ``remote_install.resolve_catalog_url`` resolved exactly one
catalog URL (env var or ``config.yaml``). A community ecosystem needs
*plural, named* sources — Claude Code's ``plugin marketplace add``.

This module owns the registry only: a ``marketplaces.yaml`` of named
``{name, url, added_at, trust_key}`` entries. It deliberately does NOT
introduce a new signing/trust stack — catalog signature verification
keeps using the existing global keyring
(``trusted_catalog_keys.json`` + ``catalog_signing``); ``trust_key`` is
an advisory per-marketplace fingerprint shown in ``list``. Fetching and
installing stay in :mod:`opencomputer.plugins.remote_install`.

File lives under the active profile home (next to the existing
``plugin_catalog_cache.json`` / ``trusted_catalog_keys.json``), so
``oc -p <name>`` gives each profile its own marketplace set.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

#: Marketplace names are slugs — typed on the command line as the
#: ``<marketplace>/<plugin>`` install prefix, so they must be simple.
_VALID_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class MarketplaceError(ValueError):
    """Invalid marketplace name / URL / duplicate."""


@dataclass(frozen=True, slots=True)
class Marketplace:
    """One named plugin catalog source."""

    name: str
    url: str
    added_at: int
    trust_key: str = ""


def marketplaces_path() -> Path:
    """Registry file for the active profile."""
    from opencomputer.agent.config import _home

    return _home() / "marketplaces.yaml"


def _validate_name(name: str) -> str:
    cleaned = (name or "").strip().lower()
    if not _VALID_NAME.match(cleaned):
        raise MarketplaceError(
            f"invalid marketplace name {name!r} — use lowercase letters, "
            f"digits, '-' and '_', starting alphanumeric"
        )
    return cleaned


def _validate_url(url: str) -> str:
    cleaned = (url or "").strip()
    if not (cleaned.startswith("https://") or cleaned.startswith("http://")):
        raise MarketplaceError(
            f"invalid marketplace URL {url!r} — must be an http(s):// "
            f"catalog endpoint"
        )
    return cleaned


def load_marketplaces(path: Path | None = None) -> list[Marketplace]:
    """Read the registry. Never raises — a malformed file logs and
    yields an empty list so a broken registry can't wedge plugin ops."""
    p = path or marketplaces_path()
    if not p.is_file():
        return []
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("marketplaces.yaml unreadable (%s) — treating as empty", exc)
        return []
    entries = raw.get("marketplaces") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    out: list[Marketplace] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip().lower()
        url = str(entry.get("url", "")).strip()
        if not _VALID_NAME.match(name) or not url:
            log.warning("marketplaces.yaml: skipping malformed entry %r", entry)
            continue
        out.append(
            Marketplace(
                name=name,
                url=url,
                added_at=int(entry.get("added_at", 0) or 0),
                trust_key=str(entry.get("trust_key", "") or ""),
            )
        )
    return out


def _write_marketplaces(items: list[Marketplace], path: Path) -> None:
    """Atomically persist the registry."""
    payload: dict[str, Any] = {
        "marketplaces": [
            {
                "name": m.name,
                "url": m.url,
                "added_at": m.added_at,
                **({"trust_key": m.trust_key} if m.trust_key else {}),
            }
            for m in items
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    tmp.replace(path)


def get_marketplace(name: str, path: Path | None = None) -> Marketplace | None:
    """Look up one marketplace by name (case-insensitive)."""
    target = (name or "").strip().lower()
    for m in load_marketplaces(path):
        if m.name == target:
            return m
    return None


def add_marketplace(
    name: str,
    url: str,
    *,
    trust_key: str = "",
    path: Path | None = None,
) -> Marketplace:
    """Register a marketplace. Raises :class:`MarketplaceError` on an
    invalid name/URL or a duplicate name."""
    p = path or marketplaces_path()
    clean_name = _validate_name(name)
    clean_url = _validate_url(url)
    items = load_marketplaces(p)
    if any(m.name == clean_name for m in items):
        raise MarketplaceError(
            f"marketplace {clean_name!r} already exists — remove it first"
        )
    entry = Marketplace(
        name=clean_name,
        url=clean_url,
        added_at=int(time.time()),
        trust_key=(trust_key or "").strip(),
    )
    items.append(entry)
    _write_marketplaces(items, p)
    return entry


def remove_marketplace(name: str, path: Path | None = None) -> bool:
    """Drop a marketplace by name. Returns ``True`` if one was removed.

    Already-installed plugins are untouched — the registry only governs
    where *new* plugins are fetched from."""
    p = path or marketplaces_path()
    target = (name or "").strip().lower()
    items = load_marketplaces(p)
    kept = [m for m in items if m.name != target]
    if len(kept) == len(items):
        return False
    _write_marketplaces(kept, p)
    return True


def marketplace_cache_path(name: str) -> Path:
    """Per-marketplace catalog cache file.

    Each marketplace caches separately so a multi-source ``search``
    doesn't have them overwrite one shared cache file."""
    from opencomputer.agent.config import _home

    safe = _validate_name(name)
    return _home() / f"plugin_catalog_cache__{safe}.json"


__all__ = [
    "Marketplace",
    "MarketplaceError",
    "add_marketplace",
    "get_marketplace",
    "load_marketplaces",
    "marketplace_cache_path",
    "marketplaces_path",
    "remove_marketplace",
]
