"""``oc plugin doctor`` — read-only plugin diagnostics (best-of-three R8).

Runs per-plugin checks (manifest, entry module syntax, min_host_version,
profile scope, enabled status) without ever importing plugin code.
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from opencomputer.cli_plugin import _diagnose_plugin, plugin_app

runner = CliRunner()


class _Manifest:
    def __init__(self, **kw) -> None:  # noqa: ANN003
        self.id = kw.get("id", "demo")
        self.version = kw.get("version", "1.0.0")
        self.kind = kw.get("kind", "tool")
        self.entry = kw.get("entry", "plugin")
        self.min_host_version = kw.get("min_host_version", "")
        self.profiles = kw.get("profiles")
        self.tool_names = kw.get("tool_names", ())
        self.mcp_servers = kw.get("mcp_servers", ())
        self.cli_commands = kw.get("cli_commands", ())


class _Candidate:
    def __init__(self, manifest, root_dir) -> None:  # noqa: ANN001
        self.manifest = manifest
        self.root_dir = root_dir


def _make_plugin(tmp_path, *, body="def register(api):\n    pass\n", **kw):  # noqa: ANN001, ANN003
    root = tmp_path / kw.get("id", "demo")
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.py").write_text(body, encoding="utf-8")
    return _Candidate(_Manifest(**kw), root)


# ── _diagnose_plugin ─────────────────────────────────────────────────


def test_healthy_plugin_passes_core_checks(tmp_path) -> None:  # noqa: ANN001
    rows = _diagnose_plugin(_make_plugin(tmp_path))
    by_check = {c: (s, d) for c, s, d in rows}
    assert by_check["manifest"][0] == "PASS"
    assert by_check["entry module"][0] == "PASS"
    assert by_check["min_host_version"][0] == "SKIP"  # not declared


def test_missing_entry_file_fails(tmp_path) -> None:  # noqa: ANN001
    cand = _make_plugin(tmp_path)
    (cand.root_dir / "plugin.py").unlink()
    rows = _diagnose_plugin(cand)
    by_check = {c: s for c, s, _ in rows}
    assert by_check["entry module"] == "FAIL"


def test_syntax_error_in_entry_fails(tmp_path) -> None:  # noqa: ANN001
    cand = _make_plugin(tmp_path, body="def register(api)\n    pass\n")
    rows = _diagnose_plugin(cand)
    by_check = {c: s for c, s, _ in rows}
    assert by_check["entry module"] == "FAIL"


def test_bad_min_host_version_fails(tmp_path) -> None:  # noqa: ANN001
    cand = _make_plugin(tmp_path, min_host_version="99999.0.0")
    rows = _diagnose_plugin(cand)
    by_check = {c: s for c, s, _ in rows}
    assert by_check["min_host_version"] == "FAIL"


def test_declared_surface_is_informational(tmp_path) -> None:  # noqa: ANN001
    cand = _make_plugin(tmp_path, tool_names=("A", "B", "C"))
    rows = _diagnose_plugin(cand)
    surface = next(d for c, _, d in rows if c == "declared surface")
    assert "3 tools" in surface


def test_resolve_filter_error_surfaces_as_fail_row(  # noqa: ANN001
    tmp_path, monkeypatch
) -> None:
    """F4 (review followup) — a broken ``_resolve_plugin_filter`` used
    to be silently swallowed and the doctor lied with an "enabled SKIP:
    no explicit filter — all enabled" row. The diagnostic tool itself
    catching-and-discarding the diagnostic is the worst class of defect.
    On exception, the enabled row must be FAIL so doctor exits non-zero
    on real config breakage."""
    def _boom() -> None:
        raise RuntimeError("synthetic profile load error")

    monkeypatch.setattr("opencomputer.cli._resolve_plugin_filter", _boom)
    rows = _diagnose_plugin(_make_plugin(tmp_path))
    by_check = {c: (s, d) for c, s, d in rows}
    assert by_check["enabled"][0] == "FAIL"
    # The error message must surface verbatim so the user can debug.
    assert "synthetic profile load error" in by_check["enabled"][1]


# ── CLI ──────────────────────────────────────────────────────────────


def test_doctor_no_args_shows_usage() -> None:
    result = runner.invoke(plugin_app, ["doctor"])
    assert result.exit_code == 2
    assert "usage" in result.stdout.lower()


def test_doctor_unknown_id_exits_nonzero() -> None:
    result = runner.invoke(plugin_app, ["doctor", "no-such-plugin-xyz"])
    assert result.exit_code == 1
    assert "no plugin" in result.stdout.lower()


def test_doctor_real_bundled_plugin() -> None:
    """Run doctor against a real bundled plugin — it must render a
    table and not crash."""
    result = runner.invoke(plugin_app, ["doctor", "anthropic-provider"])
    assert result.exit_code in (0, 1)  # 1 only if a check legitimately fails
    assert "plugin doctor" in result.stdout
    assert "entry module" in result.stdout
