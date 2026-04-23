"""Phase 12b.2 — Sub-project B, Task B1.

Tests for the plugin template tree + Jinja2 renderer (the foundation of
`opencomputer plugin new`). The renderer is in
``opencomputer/cli_plugin_scaffold.py``; templates live under
``opencomputer/templates/plugin/{channel,provider,toolkit,mixed}/``.

B1 ships only the renderer + templates — CLI wiring lands in B2, smoke
in B3. These tests exercise the renderer directly.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

# ─── one test per kind: the expected tree is present ─────────────────


def test_render_channel_template_creates_expected_files(tmp_path: Path) -> None:
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    written = render_plugin_template(
        plugin_id="demo-chan",
        kind="channel",
        output_path=tmp_path,
    )
    root = tmp_path / "demo-chan"
    assert (root / "plugin.json").exists()
    assert (root / "plugin.py").exists()
    assert (root / "adapter.py").exists()
    assert (root / "README.md").exists()
    assert (root / "tests" / "test_demo_chan.py").exists()
    assert any(str(p).endswith("adapter.py") for p in written)


def test_render_provider_template_creates_expected_files(tmp_path: Path) -> None:
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    written = render_plugin_template(
        plugin_id="demo-prov",
        kind="provider",
        output_path=tmp_path,
    )
    root = tmp_path / "demo-prov"
    assert (root / "plugin.json").exists()
    assert (root / "plugin.py").exists()
    assert (root / "provider.py").exists()
    assert (root / "README.md").exists()
    assert (root / "tests" / "test_demo_prov.py").exists()
    assert any(str(p).endswith("provider.py") for p in written)


def test_render_toolkit_template_creates_expected_files(tmp_path: Path) -> None:
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    written = render_plugin_template(
        plugin_id="demo-tool",
        kind="toolkit",
        output_path=tmp_path,
    )
    root = tmp_path / "demo-tool"
    assert (root / "plugin.json").exists()
    assert (root / "plugin.py").exists()
    assert (root / "tests" / "test_demo_tool.py").exists()
    assert (root / "README.md").exists()
    assert (root / "tools" / "__init__.py").exists()
    assert any(
        "tools/my_tool.py" in str(p) or "tools\\my_tool.py" in str(p) for p in written
    )


def test_render_mixed_template_creates_expected_files(tmp_path: Path) -> None:
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    written = render_plugin_template(
        plugin_id="demo-mixed",
        kind="mixed",
        output_path=tmp_path,
    )
    root = tmp_path / "demo-mixed"
    assert (root / "plugin.json").exists()
    assert (root / "plugin.py").exists()
    assert (root / "adapter.py").exists()
    assert (root / "provider.py").exists()
    assert (root / "tools" / "__init__.py").exists()
    assert (root / "tools" / "my_tool.py").exists()
    assert (root / "README.md").exists()
    assert (root / "tests" / "test_demo_mixed.py").exists()
    # written is a non-empty list of absolute paths
    assert len(written) >= 7
    assert all(isinstance(p, Path) for p in written)


# ─── content correctness ─────────────────────────────────────────────


def test_rendered_plugin_json_parses(tmp_path: Path) -> None:
    """Every rendered plugin.json must pass validate_manifest()."""
    from opencomputer.cli_plugin_scaffold import render_plugin_template
    from opencomputer.plugins.manifest_validator import validate_manifest

    render_plugin_template(
        plugin_id="good-provider",
        kind="provider",
        output_path=tmp_path,
        description="a test provider",
        author="Tester",
    )
    manifest_data = json.loads((tmp_path / "good-provider" / "plugin.json").read_text())
    schema, err = validate_manifest(manifest_data)
    assert err == "", f"validate_manifest rejected rendered manifest: {err}"
    assert schema is not None
    assert schema.id == "good-provider"
    assert schema.kind == "provider"


def test_rendered_plugin_py_syntax_ok(tmp_path: Path) -> None:
    """Every rendered .py file must parse cleanly via ast.parse()."""
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    for kind in ("channel", "provider", "toolkit", "mixed"):
        out = tmp_path / kind
        out.mkdir()
        render_plugin_template(
            plugin_id=f"syntax-{kind}",
            kind=kind,
            output_path=out,
        )
        root = out / f"syntax-{kind}"
        py_files = list(root.rglob("*.py"))
        assert py_files, f"no .py files rendered for kind={kind}"
        for py in py_files:
            try:
                ast.parse(py.read_text())
            except SyntaxError as e:  # pragma: no cover — failure path
                pytest.fail(f"{py} has SyntaxError: {e}")


def test_module_name_and_class_name_derivation(tmp_path: Path) -> None:
    """plugin_id='foo-bar-baz' → module_name=foo_bar_baz, class_name=FooBarBaz."""
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    render_plugin_template(
        plugin_id="foo-bar-baz",
        kind="provider",
        output_path=tmp_path,
    )
    root = tmp_path / "foo-bar-baz"
    # File-name templating: tests/test_{{ module_name }}.py.j2 →
    # tests/test_foo_bar_baz.py
    assert (root / "tests" / "test_foo_bar_baz.py").exists()
    # Class-name shows up in provider.py
    provider_src = (root / "provider.py").read_text()
    assert "FooBarBaz" in provider_src, f"missing PascalCase class_name: {provider_src}"


# ─── overwrite semantics ─────────────────────────────────────────────


def test_render_refuses_overwrite_without_flag(tmp_path: Path) -> None:
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    render_plugin_template(
        plugin_id="dup",
        kind="toolkit",
        output_path=tmp_path,
    )
    with pytest.raises(FileExistsError):
        render_plugin_template(
            plugin_id="dup",
            kind="toolkit",
            output_path=tmp_path,
        )


def test_render_with_overwrite_true_succeeds_second_time(tmp_path: Path) -> None:
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    render_plugin_template(
        plugin_id="over",
        kind="provider",
        output_path=tmp_path,
        description="first",
    )
    first_json = json.loads((tmp_path / "over" / "plugin.json").read_text())
    assert first_json["description"] == "first"

    render_plugin_template(
        plugin_id="over",
        kind="provider",
        output_path=tmp_path,
        description="second",
        overwrite=True,
    )
    second_json = json.loads((tmp_path / "over" / "plugin.json").read_text())
    assert second_json["description"] == "second"


# ─── id validation ───────────────────────────────────────────────────


def test_render_rejects_invalid_plugin_id(tmp_path: Path) -> None:
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    with pytest.raises(ValueError, match="id"):
        render_plugin_template(
            plugin_id="Bad ID",
            kind="toolkit",
            output_path=tmp_path,
        )


def test_render_rejects_empty_plugin_id(tmp_path: Path) -> None:
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    with pytest.raises(ValueError):
        render_plugin_template(
            plugin_id="",
            kind="toolkit",
            output_path=tmp_path,
        )


# ─── CLI kind → manifest kind mapping ─────────────────────────────────


def test_toolkit_kind_maps_to_tool_in_manifest(tmp_path: Path) -> None:
    """CLI uses 'toolkit' for clarity; manifest stores SDK value 'tool'."""
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    render_plugin_template(
        plugin_id="map-test",
        kind="toolkit",
        output_path=tmp_path,
    )
    data = json.loads((tmp_path / "map-test" / "plugin.json").read_text())
    assert data["kind"] == "tool"


def test_mixed_channel_provider_kinds_pass_through(tmp_path: Path) -> None:
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    for kind in ("channel", "provider", "mixed"):
        out = tmp_path / kind
        out.mkdir()
        render_plugin_template(
            plugin_id=f"kind-{kind}",
            kind=kind,
            output_path=out,
        )
        data = json.loads((out / f"kind-{kind}" / "plugin.json").read_text())
        assert data["kind"] == kind


# ─── Task B2: CLI wiring ───────────────────────────────────────────────


def _get_plugin_app():
    """Import the plugin_app fresh so tests pick up any module state."""
    import importlib

    from opencomputer import cli_plugin

    importlib.reload(cli_plugin)
    return cli_plugin.plugin_app


def test_plugin_new_creates_toolkit_scaffold_in_custom_path(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    app = _get_plugin_app()
    result = CliRunner().invoke(
        app,
        ["new", "demo", "--kind", "toolkit", "--path", str(tmp_path)],
    )
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "demo" / "plugin.json").exists()
    assert (tmp_path / "demo" / "plugin.py").exists()


def test_plugin_new_respects_profile_default_path_when_no_path_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    app = _get_plugin_app()
    result = CliRunner().invoke(app, ["new", "demo", "--kind", "toolkit"])
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "plugins" / "demo" / "plugin.json").exists()


def test_plugin_new_refuses_duplicate_without_force(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    (tmp_path / "demo").mkdir()
    app = _get_plugin_app()
    result = CliRunner().invoke(
        app,
        ["new", "demo", "--kind", "toolkit", "--path", str(tmp_path)],
    )
    assert result.exit_code == 1
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "already exists" in combined
    assert "--force" in combined


def test_plugin_new_overwrites_with_force(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "leftover.txt").write_text("old")
    app = _get_plugin_app()
    result = CliRunner().invoke(
        app,
        ["new", "demo", "--kind", "toolkit", "--path", str(tmp_path), "--force"],
    )
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "demo" / "plugin.json").exists()
    assert not (tmp_path / "demo" / "leftover.txt").exists()


def test_plugin_new_rejects_invalid_id(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    app = _get_plugin_app()
    result = CliRunner().invoke(
        app,
        ["new", "Bad Name", "--kind", "toolkit", "--path", str(tmp_path)],
    )
    assert result.exit_code == 1
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "id" in combined or "lowercase" in combined or "format" in combined


def test_plugin_new_interactive_prompt_for_kind_when_tty(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    app = _get_plugin_app()
    result = CliRunner().invoke(
        app,
        ["new", "demo", "--path", str(tmp_path)],
        input="provider\n",
    )
    assert result.exit_code == 0, result.stdout
    data = json.loads((tmp_path / "demo" / "plugin.json").read_text())
    assert data["kind"] == "provider"


def test_plugin_new_errors_when_kind_omitted_in_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    app = _get_plugin_app()
    import opencomputer.cli_plugin as cli_plugin_mod

    monkeypatch.setattr(cli_plugin_mod.sys.stdin, "isatty", lambda: False)
    result = CliRunner().invoke(
        app,
        ["new", "demo", "--path", str(tmp_path)],
        input="",
    )
    assert result.exit_code == 1
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "non-interactive" in combined or "required" in combined


def test_plugin_new_prints_next_steps(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    app = _get_plugin_app()
    result = CliRunner().invoke(
        app,
        ["new", "demo", "--kind", "provider", "--path", str(tmp_path)],
    )
    assert result.exit_code == 0, result.stdout
    assert "Next steps" in result.stdout
    # Numbered list items — at least three specific markers
    assert "cd " in result.stdout
    assert "pytest" in result.stdout
    assert "opencomputer plugins" in result.stdout


# ─── Task B3: post-scaffold smoke check ───────────────────────────────


def test_plugin_new_smoke_passes_for_toolkit(tmp_path: Path) -> None:
    """After rendering a toolkit, the CLI should load the plugin and print OK."""
    from typer.testing import CliRunner

    app = _get_plugin_app()
    result = CliRunner().invoke(
        app,
        ["new", "demo", "--kind", "toolkit", "--path", str(tmp_path)],
    )
    assert result.exit_code == 0, result.stdout
    assert "Smoke check passed" in result.stdout


def test_plugin_new_smoke_passes_for_each_kind(tmp_path: Path) -> None:
    """Every one of the 4 template kinds should scaffold cleanly + smoke-pass."""
    from typer.testing import CliRunner

    for kind in ("channel", "provider", "toolkit", "mixed"):
        out = tmp_path / kind
        out.mkdir()
        app = _get_plugin_app()
        result = CliRunner().invoke(
            app,
            ["new", f"demo-{kind}", "--kind", kind, "--path", str(out)],
        )
        assert result.exit_code == 0, f"kind={kind}: {result.stdout}"
        assert "Smoke check passed" in result.stdout, (
            f"kind={kind} stdout: {result.stdout}"
        )


def test_plugin_new_smoke_failure_prints_red_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If load_plugin raises, CLI exits 1 but leaves files on disk."""
    from typer.testing import CliRunner

    import opencomputer.cli_plugin as cli_plugin_mod

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    # Patch the symbol cli_plugin uses to perform the smoke load.
    monkeypatch.setattr(cli_plugin_mod, "_smoke_load_plugin", _boom, raising=False)

    # If the attribute doesn't exist yet (before B3 impl), fall back to
    # patching the underlying loader.load_plugin — either path exercises
    # the failure branch in the CLI.
    import opencomputer.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "load_plugin", _boom)

    app = _get_plugin_app()
    result = CliRunner().invoke(
        app,
        ["new", "demo", "--kind", "toolkit", "--path", str(tmp_path)],
    )
    assert result.exit_code == 1, result.stdout
    combined = result.stdout + (result.stderr or "")
    assert "Smoke check failed" in combined
    assert "boom" in combined
    # Files are still on disk — user can fix + retry
    assert (tmp_path / "demo" / "plugin.json").exists()
    assert (tmp_path / "demo" / "plugin.py").exists()


def test_plugin_new_no_smoke_flag_skips_check(tmp_path: Path) -> None:
    """--no-smoke suppresses the smoke check entirely."""
    from typer.testing import CliRunner

    app = _get_plugin_app()
    result = CliRunner().invoke(
        app,
        [
            "new",
            "demo",
            "--kind",
            "toolkit",
            "--path",
            str(tmp_path),
            "--no-smoke",
        ],
    )
    assert result.exit_code == 0, result.stdout
    # Neither success nor failure marker should appear
    assert "Smoke check passed" not in result.stdout
    assert "Smoke check failed" not in result.stdout


def test_plugin_new_smoke_uses_isolated_registry(tmp_path: Path) -> None:
    """Scaffolding must NOT pollute the process-global plugin registry."""
    from typer.testing import CliRunner

    from opencomputer.plugins.registry import registry as global_registry

    # Snapshot of provider/channel keys + loaded plugins before scaffolding.
    providers_before = set(global_registry.providers.keys())
    channels_before = set(global_registry.channels.keys())
    loaded_ids_before = {lp.candidate.manifest.id for lp in global_registry.loaded}

    app = _get_plugin_app()
    # Use mixed — registers channel + provider + tool, maximum chance of pollution.
    result = CliRunner().invoke(
        app,
        ["new", "isolated-demo", "--kind", "mixed", "--path", str(tmp_path)],
    )
    assert result.exit_code == 0, result.stdout
    assert "Smoke check passed" in result.stdout

    providers_after = set(global_registry.providers.keys())
    channels_after = set(global_registry.channels.keys())
    loaded_ids_after = {lp.candidate.manifest.id for lp in global_registry.loaded}

    assert providers_after == providers_before, (
        f"smoke check leaked providers: {providers_after - providers_before}"
    )
    assert channels_after == channels_before, (
        f"smoke check leaked channels: {channels_after - channels_before}"
    )
    assert loaded_ids_after == loaded_ids_before, (
        f"smoke check leaked loaded plugins: {loaded_ids_after - loaded_ids_before}"
    )
