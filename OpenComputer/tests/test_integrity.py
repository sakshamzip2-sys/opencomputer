"""Tests for integrity.py — drift detection for installed plugins."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from opencomputer.plugins.installed_index import (
    InstalledRecord,
    record_install,
)
from opencomputer.plugins.integrity import (
    DriftReport,
    NotInstalledError,
    SourceUnreachableError,
    verify_plugin,
)
from tests._helpers.install_fixtures import make_tarball as _make_tarball


def test_verify_unknown_plugin_raises(tmp_path: Path):
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()
    with pytest.raises(NotInstalledError):
        verify_plugin("ghost", dest_root=dest_root)


def test_verify_catalog_install_clean(tmp_path: Path):
    """Round-trip: install (mock), verify, expect no drift."""
    import io
    import tarfile

    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    raw = _make_tarball("clean-verify")
    sha = hashlib.sha256(raw).hexdigest()

    plugin_dir = dest_root / "clean-verify"
    plugin_dir.mkdir()
    # Extract the tarball to the plugin dir so on-disk bytes match the source.
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(path=plugin_dir, filter="data")

    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id="clean-verify",
            version="0.1.0",
            source="catalog",
            source_url="clean-verify",
            source_ref=None,
            tarball_sha256=sha,
            installed_at=0,
        ),
    )

    # Re-fetch returns the same bytes — no drift.
    report = verify_plugin(
        "clean-verify",
        dest_root=dest_root,
        refetch_fn=lambda rec: raw,
    )
    assert isinstance(report, DriftReport)
    assert report.has_drift is False


def test_verify_drift_detected_on_mutated_file(tmp_path: Path):
    import io
    import tarfile

    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    raw_original = _make_tarball("drift-test")
    sha = hashlib.sha256(raw_original).hexdigest()

    plugin_dir = dest_root / "drift-test"
    plugin_dir.mkdir()
    # Extract the original tarball, then mutate plugin.py so drift is detected.
    with tarfile.open(fileobj=io.BytesIO(raw_original), mode="r:gz") as tar:
        tar.extractall(path=plugin_dir, filter="data")
    (plugin_dir / "plugin.py").write_text(
        "def register(api):\n    print('mutated')\n"
    )

    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id="drift-test",
            version="0.1.0",
            source="catalog",
            source_url="drift-test",
            source_ref=None,
            tarball_sha256=sha,
            installed_at=0,
        ),
    )

    report = verify_plugin(
        "drift-test",
        dest_root=dest_root,
        refetch_fn=lambda rec: raw_original,
    )
    assert report.has_drift is True
    assert any("plugin.py" in d.path for d in report.differences)


def test_verify_source_unreachable_does_not_crash(tmp_path: Path):
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    plugin_dir = dest_root / "offline"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"id":"offline","name":"offline","version":"0.1.0","entry":"plugin.py"}'
    )

    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id="offline",
            version="0.1.0",
            source="url",
            source_url="https://gone.example/x.tgz",
            source_ref=None,
            tarball_sha256="0" * 64,
            installed_at=0,
        ),
    )

    def boom(rec):
        raise OSError("connection refused")

    with pytest.raises(SourceUnreachableError):
        verify_plugin("offline", dest_root=dest_root, refetch_fn=boom)
