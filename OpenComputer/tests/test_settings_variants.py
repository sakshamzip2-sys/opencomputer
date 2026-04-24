"""III.3 — bundled settings variants (lax / strict / sandbox).

Covers the three starter ``config.yaml`` templates shipped under
``opencomputer/settings_variants/`` plus the ``opencomputer config init``
and ``opencomputer config variants`` CLI surfaces.

Each variant must:
  - Round-trip through :func:`load_config` without raising.
  - Produce the expected hook shape (none / strict audit / sandbox Bash).
  - Copy cleanly into a profile via the init CLI, with overwrite gating.

Reference: ``sources/claude-code/examples/settings/README.md`` (the three
Claude Code settings examples these variants mirror).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.config_store import load_config
from opencomputer.cli import _variants_dir, app

# ─── helpers ───────────────────────────────────────────────────────────

VARIANTS = ("lax", "strict", "sandbox")


def _variant_path(name: str) -> Path:
    return _variants_dir() / f"{name}.yaml"


# ─── variant files parse cleanly ───────────────────────────────────────


@pytest.mark.parametrize("variant", VARIANTS)
def test_all_variants_parse_cleanly(variant: str) -> None:
    """Each bundled YAML must parse into a valid :class:`Config`.

    This guards against typos / hook-syntax errors sneaking into a variant.
    Malformed hooks would be silently skipped (warn + drop) by
    ``_parse_hooks_block``, so we additionally assert below that strict /
    sandbox retain their expected hooks.
    """
    path = _variant_path(variant)
    assert path.is_file(), f"variant file missing: {path}"
    cfg = load_config(path)
    # Model / loop / memory fields should all be intact.
    assert cfg.model.provider == "anthropic"
    assert cfg.model.model == "claude-opus-4-7"
    assert cfg.memory.provider == "memory-honcho"


def test_lax_variant_has_no_hooks() -> None:
    cfg = load_config(_variant_path("lax"))
    assert cfg.hooks == ()
    # lax keeps the default (permissive) loop settings.
    assert cfg.loop.max_iterations == 50
    assert cfg.loop.parallel_tools is True


def test_strict_variant_has_pretooluse_hook() -> None:
    cfg = load_config(_variant_path("strict"))
    assert len(cfg.hooks) >= 1
    pre = [h for h in cfg.hooks if h.event == "PreToolUse"]
    assert len(pre) >= 1, "strict must declare at least one PreToolUse hook"
    # The audit hook must target destructive / mutating tools.
    assert any("Edit" in (h.matcher or "") for h in pre)
    assert any("Write" in (h.matcher or "") for h in pre)
    # Strict tightens the loop budget and serialises tool dispatch.
    assert cfg.loop.max_iterations == 30
    assert cfg.loop.parallel_tools is False
    assert cfg.loop.delegation_max_iterations == 20


def test_sandbox_variant_has_bash_matcher() -> None:
    cfg = load_config(_variant_path("sandbox"))
    bash_hooks = [
        h for h in cfg.hooks if h.event == "PreToolUse" and h.matcher == "Bash"
    ]
    assert bash_hooks, "sandbox variant must wire a Bash PreToolUse hook"
    # Sandbox inherits strict's tightened loop posture.
    assert cfg.loop.max_iterations == 30
    assert cfg.loop.parallel_tools is False


# ─── CLI — opencomputer config init ────────────────────────────────────


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the active profile home at ``tmp_path`` and return it."""
    home = tmp_path / ".opencomputer"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(home))
    return home


def test_config_init_copies_variant_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate_home(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(app, ["config", "init", "--variant", "lax"])

    assert result.exit_code == 0, result.stdout
    dst = home / "config.yaml"
    assert dst.is_file()
    # Variant content must land verbatim (comments preserved).
    src_text = _variant_path("lax").read_text(encoding="utf-8")
    assert dst.read_text(encoding="utf-8") == src_text
    # Confirmation mentions the variant name.
    assert "lax" in result.stdout


def test_config_init_refuses_overwrite_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate_home(tmp_path, monkeypatch)
    existing = home / "config.yaml"
    existing.write_text("# pre-existing\nmodel:\n  provider: openai\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["config", "init", "--variant", "strict"])

    assert result.exit_code != 0
    assert "already exists" in result.stdout
    assert "--force" in result.stdout
    # File on disk must be untouched.
    assert "pre-existing" in existing.read_text(encoding="utf-8")


def test_config_init_overwrites_with_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolate_home(tmp_path, monkeypatch)
    existing = home / "config.yaml"
    existing.write_text("# original\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["config", "init", "--variant", "strict", "--force"])

    assert result.exit_code == 0, result.stdout
    new_text = existing.read_text(encoding="utf-8")
    assert "original" not in new_text
    assert "STRICT variant" in new_text


def test_config_init_unknown_variant_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_home(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(app, ["config", "init", "--variant", "foobar"])

    assert result.exit_code != 0
    lowered = result.stdout.lower()
    assert "unknown variant" in lowered
    # Error message should surface the available names so the user can retry.
    assert "lax" in lowered
    assert "strict" in lowered
    assert "sandbox" in lowered


def test_config_variants_lists_all_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_home(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(app, ["config", "variants"])

    assert result.exit_code == 0, result.stdout
    for name in VARIANTS:
        assert name in result.stdout, f"{name} not listed in variants output"
