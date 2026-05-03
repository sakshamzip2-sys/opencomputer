"""plugin.json size is capped at 256KB to defend discovery.

Sub-project G (openclaw-parity) Task 5. Defends against a malicious
plugin shipping a 100MB plugin.json that DOSes the scan loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.plugins.discovery import MAX_MANIFEST_BYTES, _parse_manifest


class TestManifestSizeCap:
    def test_normal_size_parses(self, tmp_path: Path) -> None:
        path = tmp_path / "plugin.json"
        path.write_text(
            '{"id":"x","name":"X","version":"0.1.0","entry":"plugin","kind":"tool"}',
            encoding="utf-8",
        )
        assert _parse_manifest(path) is not None

    def test_oversized_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        path = tmp_path / "plugin.json"
        padding = "x" * (MAX_MANIFEST_BYTES + 1)
        path.write_text(
            '{"id":"x","name":"X","version":"0.1.0","entry":"plugin",'
            f'"kind":"tool","description":"{padding}"}}',
            encoding="utf-8",
        )
        with caplog.at_level("WARNING", logger="opencomputer.plugins.discovery"):
            result = _parse_manifest(path)
        assert result is None
        assert any("exceeds" in rec.message for rec in caplog.records)

    def test_exact_boundary_passes_size_gate(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The size cap test only cares that a 256KB-exact file is NOT
        # skipped at the size gate. Padding a top-level field that has
        # its own length cap (e.g. `description` ≤ 512) would be
        # rejected by the validator — that's a separate concern.
        # Filling `legacy_plugin_ids` with many unique short ids keeps
        # us inside the validator while still hitting MAX_MANIFEST_BYTES.
        path = tmp_path / "plugin.json"
        prefix = (
            '{"id":"x","name":"X","version":"0.1.0",'
            '"entry":"plugin","kind":"tool","legacy_plugin_ids":['
        )
        suffix = '"a"]}'
        # Each id "lid12345" + comma = ~12 bytes; fill until we approach
        # the cap, then trim.
        ids: list[str] = []
        approx = len(prefix) + len(suffix)
        i = 0
        while approx < MAX_MANIFEST_BYTES - 60:
            entry = f'"lid-{i:09d}",'
            ids.append(entry)
            approx += len(entry)
            i += 1
        body = prefix + "".join(ids) + suffix
        # Trim or pad with whitespace to hit the cap exactly.
        if len(body.encode("utf-8")) < MAX_MANIFEST_BYTES:
            pad = " " * (MAX_MANIFEST_BYTES - len(body.encode("utf-8")))
            body = prefix + "".join(ids) + suffix + pad
        path.write_text(body, encoding="utf-8")
        assert path.stat().st_size == MAX_MANIFEST_BYTES
        with caplog.at_level("WARNING", logger="opencomputer.plugins.discovery"):
            _parse_manifest(path)
        # The size gate emits a warning containing "exceeds" only when
        # rejected. We assert NO such warning fired.
        assert not any("exceeds" in rec.message for rec in caplog.records)
