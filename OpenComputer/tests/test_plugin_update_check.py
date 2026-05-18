"""Per-plugin update notifier (best-of-three Recipe 10).

Covers the pure ``compute_updates`` diff, version comparison, and the
6h cache round-trip. Catalog fetching is network and not exercised here.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.plugins.installed_index import InstalledRecord
from opencomputer.plugins.update_check import (
    CACHE_TTL_SECONDS,
    PluginUpdate,
    _is_newer,
    compute_updates,
    read_cache,
    write_cache,
)


def _rec(pid: str, version: str, source: str = "catalog") -> InstalledRecord:
    return InstalledRecord(
        plugin_id=pid,
        version=version,
        source=source,
        source_url=pid,
        source_ref=None,
        tarball_sha256="x" * 64 if source == "catalog" else None,
        installed_at=0,
    )


# ── version comparison ───────────────────────────────────────────────


def test_is_newer_semver() -> None:
    assert _is_newer("1.0.0", "1.1.0") is True
    assert _is_newer("1.1.0", "1.0.0") is False
    assert _is_newer("1.0.0", "1.0.0") is False


def test_is_newer_empty_available_is_false() -> None:
    assert _is_newer("1.0.0", "") is False


def test_is_newer_non_pep440_falls_back_to_inequality() -> None:
    assert _is_newer("2026-01-01", "2026-02-01") is True
    assert _is_newer("abc", "abc") is False


# ── compute_updates ──────────────────────────────────────────────────


def test_detects_available_update() -> None:
    records = [_rec("alpha", "1.0.0")]
    updates = compute_updates(records, {"alpha": "1.2.0"})
    assert len(updates) == 1
    assert updates[0].plugin_id == "alpha"
    assert updates[0].available_version == "1.2.0"


def test_no_update_when_current_is_latest() -> None:
    assert compute_updates([_rec("alpha", "2.0.0")], {"alpha": "2.0.0"}) == []


def test_non_catalog_sources_are_skipped() -> None:
    records = [_rec("gitplug", "1.0.0", source="git")]
    # even with a newer version advertised, git installs are not checked
    assert compute_updates(records, {"gitplug": "9.9.9"}) == []


def test_plugin_absent_from_catalog_is_not_an_update() -> None:
    assert compute_updates([_rec("alpha", "1.0.0")], {}) == []


# ── cache ────────────────────────────────────────────────────────────


def test_cache_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "update_cache.json"
    updates = [PluginUpdate("alpha", "1.0.0", "1.2.0", "alpha")]
    write_cache(updates, path, now=1000.0)
    loaded = read_cache(path, now=1000.0)
    assert loaded == updates


def test_stale_cache_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "update_cache.json"
    write_cache([], path, now=1000.0)
    # read well past the TTL
    assert read_cache(path, now=1000.0 + CACHE_TTL_SECONDS + 1) is None


def test_missing_cache_returns_none(tmp_path: Path) -> None:
    assert read_cache(tmp_path / "nope.json") is None


def test_fresh_empty_cache_returns_empty_list_not_none(tmp_path: Path) -> None:
    path = tmp_path / "update_cache.json"
    write_cache([], path, now=1000.0)
    assert read_cache(path, now=1000.0 + 60) == []


# ── _build_catalog_versions (cli_plugin.py) ──────────────────────────


def test_default_catalog_failure_is_surfaced(monkeypatch) -> None:  # noqa: ANN001
    """F5 (review followup) — when the default catalog fetch fails,
    ``_build_catalog_versions`` must SURFACE the failure (yellow log
    line). The previous ``except CatalogError: pass`` swallowed every
    subclass under a "no default catalog configured" comment that was
    true for only one subclass; a real network 5xx, signature failure,
    or parse error would silently leave ``oc plugin update-check``
    reporting "all up to date" — the worst class of bug in a notifier.
    """
    from opencomputer import cli_plugin
    from opencomputer.plugins import remote_install

    captured: list[str] = []

    class _SpyConsole:
        def print(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            captured.append(" ".join(str(a) for a in args))

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise remote_install.CatalogError(
            "synthetic 5xx from default catalog"
        )

    from opencomputer.plugins import marketplaces as mp_mod

    monkeypatch.setattr(remote_install, "fetch_catalog", _boom)
    monkeypatch.setattr(
        remote_install,
        "resolve_catalog_url",
        lambda: "https://example.test/c.json",
    )
    # ``load_marketplaces`` is imported lazily inside
    # ``_build_catalog_versions`` — patch at the source module, not at
    # the cli_plugin reference (which would never resolve until import).
    monkeypatch.setattr(mp_mod, "load_marketplaces", lambda: [])
    monkeypatch.setattr(cli_plugin, "_console", _SpyConsole())

    versions = cli_plugin._build_catalog_versions()

    assert versions == {}, "nothing should be recovered when default catalog fails"
    surfaced = [c for c in captured if "default catalog" in c]
    assert surfaced, (
        f"default-catalog failure must surface a log line; got {captured!r}"
    )
    assert "synthetic 5xx" in surfaced[0]
