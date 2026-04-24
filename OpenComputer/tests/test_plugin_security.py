"""Tests for filesystem security checks on plugin discovery (I.1).

Covers ``opencomputer.plugins.security.validate_plugin_root`` plus the
``discover()`` integration that rejects candidates failing the check
before ever parsing their manifest. Matches OpenClaw's pattern at
sources/openclaw/src/plugins/discovery.ts:152-307.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from opencomputer.plugins import discovery
from opencomputer.plugins.discovery import discover
from opencomputer.plugins.security import (
    SecurityCheckResult,
    validate_plugin_root,
)

_IS_POSIX = hasattr(os, "geteuid")


def _write_manifest(plugin_dir: Path, plugin_id: str, entry: str = "plugin") -> Path:
    """Write a minimal valid plugin.json under ``plugin_dir``."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": plugin_id,
        "name": plugin_id.replace("-", " ").title(),
        "version": "0.0.1",
        "kind": "tool",
        "entry": entry,
    }
    manifest_path = plugin_dir / "plugin.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_dir / f"{entry}.py").write_text("", encoding="utf-8")
    return manifest_path


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    """Each test starts with an empty discovery cache."""
    discovery._discovery_cache.clear()
    yield
    discovery._discovery_cache.clear()


# ---------------------------------------------------------------------
# validate_plugin_root — direct unit tests
# ---------------------------------------------------------------------


def test_normal_plugin_accepted(tmp_path: Path) -> None:
    """A plain plugin dir inside the search root with default perms passes."""
    root = tmp_path / "plugins"
    root.mkdir()
    plugin_dir = root / "good-plugin"
    _write_manifest(plugin_dir, "good-plugin")

    result = validate_plugin_root(plugin_dir, root)

    assert result.ok, result.reason


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    """A plugin dir symlinked to a location outside the root is rejected."""
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_manifest(outside, "evil")

    root = tmp_path / "plugins"
    root.mkdir()
    link = root / "evil"
    link.symlink_to(outside, target_is_directory=True)

    result = validate_plugin_root(link, root)

    assert not result.ok
    assert "escapes search root" in (result.reason or "")


def test_symlink_inside_root_accepted(tmp_path: Path) -> None:
    """A symlink whose target is ALSO inside the root must pass."""
    root = tmp_path / "plugins"
    root.mkdir()
    real = root / "real"
    _write_manifest(real, "real")
    link = root / "link"
    link.symlink_to(real, target_is_directory=True)

    result = validate_plugin_root(link, root)

    assert result.ok, result.reason


@pytest.mark.skipif(not _IS_POSIX, reason="POSIX-only permission semantics")
def test_world_writable_user_plugin_rejected(tmp_path: Path) -> None:
    """User-installed plugin with world-writable bit → fail closed."""
    root = tmp_path / "plugins"
    root.mkdir()
    plugin_dir = root / "loose"
    _write_manifest(plugin_dir, "loose")
    # 0o777 includes the ``others write`` bit the check guards against.
    os.chmod(plugin_dir, 0o777)

    result = validate_plugin_root(plugin_dir, root, is_bundled=False)

    assert not result.ok
    assert "world-writable" in (result.reason or "")


@pytest.mark.skipif(not _IS_POSIX, reason="POSIX-only permission semantics")
def test_world_writable_bundled_plugin_accepted_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Bundled plugin with same loose perms loads but logs a warning."""
    root = tmp_path / "plugins"
    root.mkdir()
    plugin_dir = root / "bundled-loose"
    _write_manifest(plugin_dir, "bundled-loose")
    os.chmod(plugin_dir, 0o777)

    with caplog.at_level(logging.WARNING, logger="opencomputer.plugins.security"):
        result = validate_plugin_root(plugin_dir, root, is_bundled=True)

    assert result.ok, result.reason
    assert any("world-writable" in rec.message for rec in caplog.records)


@pytest.mark.skipif(not _IS_POSIX, reason="POSIX-only uid semantics")
def test_owner_uid_mismatch_user_plugin_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directory owned by another non-root user → reject for user plugins."""
    root = tmp_path / "plugins"
    root.mkdir()
    plugin_dir = root / "stolen"
    _write_manifest(plugin_dir, "stolen")

    # Fake a different effective uid than the filesystem's owner. We
    # don't control st_uid (root-only), so flip geteuid instead — the
    # check compares the two and can't tell which side moved.
    real_uid = plugin_dir.stat().st_uid
    fake_uid = real_uid + 12345
    monkeypatch.setattr(
        "opencomputer.plugins.security.os.geteuid", lambda: fake_uid
    )

    result = validate_plugin_root(plugin_dir, root, is_bundled=False)

    assert not result.ok
    assert "suspicious ownership" in (result.reason or "")


@pytest.mark.skipif(not _IS_POSIX, reason="POSIX-only uid semantics")
def test_owner_uid_mismatch_bundled_plugin_accepted_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same UID mismatch is only a warning for bundled plugins."""
    root = tmp_path / "plugins"
    root.mkdir()
    plugin_dir = root / "bundled-stolen"
    _write_manifest(plugin_dir, "bundled-stolen")

    real_uid = plugin_dir.stat().st_uid
    monkeypatch.setattr(
        "opencomputer.plugins.security.os.geteuid", lambda: real_uid + 777
    )

    with caplog.at_level(logging.WARNING, logger="opencomputer.plugins.security"):
        result = validate_plugin_root(plugin_dir, root, is_bundled=True)

    assert result.ok, result.reason
    assert any("suspicious ownership" in rec.message for rec in caplog.records)


@pytest.mark.skipif(not _IS_POSIX, reason="POSIX-only uid semantics")
def test_root_owned_path_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root-owned (uid=0) directory is always accepted — install scripts."""
    root = tmp_path / "plugins"
    root.mkdir()
    plugin_dir = root / "root-owned"
    _write_manifest(plugin_dir, "root-owned")

    # Pretend the directory is owned by root by mocking its stat result.
    real_stat = plugin_dir.stat

    class _FakeStat:
        def __init__(self, orig):
            self._orig = orig

        def __getattr__(self, name):
            if name == "st_uid":
                return 0
            return getattr(self._orig, name)

    def _fake_stat(*args, **kwargs):
        return _FakeStat(real_stat())

    monkeypatch.setattr(Path, "stat", _fake_stat)
    # Ensure we're running as a non-root uid so the check actually runs.
    monkeypatch.setattr(
        "opencomputer.plugins.security.os.geteuid", lambda: 1000
    )

    result = validate_plugin_root(plugin_dir, root, is_bundled=False)

    assert result.ok, result.reason


def test_windows_skips_posix_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-POSIX platforms return ok=True with a skipped-check note."""
    root = tmp_path / "plugins"
    root.mkdir()
    plugin_dir = root / "winplugin"
    _write_manifest(plugin_dir, "winplugin")

    # Simulate a platform without ``os.geteuid`` by removing the attr.
    # ``raising=False`` keeps the delattr safe when the attr was never
    # defined (real Windows).
    monkeypatch.delattr(
        "opencomputer.plugins.security.os.geteuid", raising=False
    )

    result = validate_plugin_root(plugin_dir, root, is_bundled=False)

    assert result.ok
    assert "posix-only" in (result.reason or "").lower()


def test_security_check_result_is_frozen_dataclass() -> None:
    """Regression guard — SecurityCheckResult must stay frozen + slotted."""
    r = SecurityCheckResult(ok=True)
    with pytest.raises((AttributeError, Exception)):
        r.ok = False  # type: ignore[misc]


# ---------------------------------------------------------------------
# discover() integration — the gate rejects candidates before manifest parse
# ---------------------------------------------------------------------


def test_discover_skips_symlink_escape(tmp_path: Path) -> None:
    """Rejected plugin should NOT appear in discover()'s candidate list."""
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_manifest(outside, "evil")

    root = tmp_path / "plugins"
    root.mkdir()
    # One legit plugin plus one symlink-escape attempt.
    _write_manifest(root / "legit", "legit")
    (root / "evil").symlink_to(outside, target_is_directory=True)

    candidates = discover([root], force_rescan=True)

    ids = {c.manifest.id for c in candidates}
    assert ids == {"legit"}


@pytest.mark.skipif(not _IS_POSIX, reason="POSIX-only permission semantics")
def test_discover_skips_world_writable_user_plugin(tmp_path: Path) -> None:
    """User-installed plugin with 0o777 perms is filtered out."""
    root = tmp_path / "plugins"
    root.mkdir()
    _write_manifest(root / "tight", "tight")
    loose = root / "loose"
    _write_manifest(loose, "loose")
    os.chmod(loose, 0o777)

    candidates = discover([root], force_rescan=True)

    ids = {c.manifest.id for c in candidates}
    assert ids == {"tight"}


@pytest.mark.skipif(not _IS_POSIX, reason="POSIX-only permission semantics")
def test_discover_accepts_world_writable_bundled_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under extensions/ (bundled), same loose perms still load."""
    # Point ``_bundled_extensions_root`` at our tmp tree so that any
    # plugin under it is treated as bundled.
    bundled_root = tmp_path / "extensions"
    bundled_root.mkdir()
    monkeypatch.setattr(
        discovery, "_bundled_extensions_root", lambda: bundled_root.resolve()
    )

    _write_manifest(bundled_root / "tight", "tight")
    loose = bundled_root / "loose"
    _write_manifest(loose, "loose")
    os.chmod(loose, 0o777)

    candidates = discover([bundled_root], force_rescan=True)

    ids = {c.manifest.id for c in candidates}
    assert ids == {"tight", "loose"}


def test_discover_accepts_normal_plugins(tmp_path: Path) -> None:
    """Plain plugins with default perms pass both direct + integration checks."""
    root = tmp_path / "plugins"
    root.mkdir()
    _write_manifest(root / "alpha", "alpha")
    _write_manifest(root / "beta", "beta")

    candidates = discover([root], force_rescan=True)

    assert {c.manifest.id for c in candidates} == {"alpha", "beta"}


# ---------------------------------------------------------------------
# Regression guard — bundled extensions/ must still all pass the gate
# ---------------------------------------------------------------------


def test_repo_bundled_extensions_all_pass_security_gate() -> None:
    """Every shipped ``extensions/<id>`` dir must pass validate_plugin_root.

    This is the zero-regression gate for I.1 — if a refactor tightens
    the check in a way that would reject a bundled plugin, this fails
    loudly here rather than in production.
    """
    # Locate the repo's extensions/ via the same derivation discovery
    # uses. Pathed so the test is resilient to test-runner cwd.
    repo_root = Path(__file__).resolve().parent.parent
    ext_dir = repo_root / "extensions"
    if not ext_dir.exists():
        pytest.skip("repo extensions/ not present in this checkout")

    failures: list[str] = []
    for entry in sorted(ext_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / "plugin.json").exists():
            continue
        result = validate_plugin_root(entry, ext_dir, is_bundled=True)
        if not result.ok:
            failures.append(f"{entry.name}: {result.reason}")

    assert not failures, "bundled plugins failing security gate:\n" + "\n".join(
        failures
    )
