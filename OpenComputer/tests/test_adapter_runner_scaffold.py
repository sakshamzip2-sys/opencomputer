"""Tests for ``opencomputer plugin new --kind adapter-pack`` scaffolding."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_registry():
    from extensions.adapter_runner import clear_registry_for_tests

    clear_registry_for_tests()
    yield
    clear_registry_for_tests()


def test_adapter_pack_kind_in_valid_list():
    from opencomputer.cli_plugin_scaffold import _VALID_KINDS

    assert "adapter-pack" in _VALID_KINDS


def test_render_adapter_pack_template(tmp_path: Path) -> None:
    """Rendering the template tree creates the expected files."""
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    written = render_plugin_template(
        plugin_id="my-pack",
        kind="adapter-pack",
        output_path=tmp_path,
        description="Test pack",
        author="Tester",
    )
    target = tmp_path / "my-pack"
    assert target.is_dir()

    expected = {
        "plugin.json",
        "plugin.py",
        "README.md",
        "pyproject.toml",
        "adapters/example/command.py",
        "adapters/example/verify/command.json",
    }
    rel_written = {p.relative_to(target).as_posix() for p in written}
    assert expected.issubset(rel_written)

    # plugin.json mentions adapter-pack name + tool kind
    manifest = (target / "plugin.json").read_text()
    assert "my-pack" in manifest
    assert '"kind": "tool"' in manifest

    # plugin.py wires register_adapter_pack
    plugin_py = (target / "plugin.py").read_text()
    assert "register_adapter_pack" in plugin_py
    assert "adapters_dir" in plugin_py


def test_render_adapter_pack_idempotent(tmp_path: Path) -> None:
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    render_plugin_template(
        plugin_id="p1",
        kind="adapter-pack",
        output_path=tmp_path,
        description="d",
        author="a",
    )
    # Second call without overwrite must raise
    with pytest.raises(FileExistsError):
        render_plugin_template(
            plugin_id="p1",
            kind="adapter-pack",
            output_path=tmp_path,
            description="d",
            author="a",
        )

    # With overwrite=True, succeeds.
    render_plugin_template(
        plugin_id="p1",
        kind="adapter-pack",
        output_path=tmp_path,
        description="d2",
        author="a",
        overwrite=True,
    )
