"""Best-of-three review-followup hardening — the 6 MEDIUM fixes.

Each test pins one silent-swallow / observability fix surfaced by the
Phase-7 multi-agent review of the best-of-three port (PR #640). The
fixes turn `except: pass` / mis-scoped guards into surfaced WARN logs
or real behaviour, per project gotcha 13 (no silent error-swallow).
"""
from __future__ import annotations

import logging

from typer.testing import CliRunner


# ── M1 — _current_skin warns on a broken profile config ──────────────


def test_m1_current_skin_warns_on_broken_config(monkeypatch, caplog) -> None:  # noqa: ANN001
    from opencomputer import cli_skin
    from opencomputer.agent import profile_yaml

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("synthetic broken profile config")

    monkeypatch.setattr(profile_yaml, "get_display_skin", _boom)
    with caplog.at_level(logging.WARNING):
        result = cli_skin._current_skin()
    assert result == "default"
    assert any(
        "could not read the display skin" in r.message
        for r in caplog.records
    ), "a broken config must surface a WARN, not fall through silently"


# ── M2 — read_cache: corrupt cache is distinct from missing ──────────


def test_m2_read_cache_warns_on_corrupt_but_not_on_missing(  # noqa: ANN001
    tmp_path, caplog
) -> None:
    from opencomputer.plugins.update_check import read_cache

    # Missing → silent None (normal).
    with caplog.at_level(logging.WARNING):
        assert read_cache(tmp_path / "nope.json") is None
    assert not [
        r for r in caplog.records if "update cache" in r.message
    ], "a missing cache must be silent"

    # Corrupt → None AND a WARN (truncated write / permission flip).
    corrupt = tmp_path / "update_cache.json"
    corrupt.write_text("{not valid json", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert read_cache(corrupt) is None
    assert any(
        "unreadable" in r.message for r in caplog.records
    ), "a corrupt cache must surface a WARN"


# ── M3 — sync_builtin_commands warns on System-A import drift ────────


def test_m3_sync_builtin_commands_warns_on_import_drift(  # noqa: ANN001
    monkeypatch, caplog
) -> None:
    import opencomputer.agent.slash_commands as sc
    from opencomputer.cli_ui.slash_handlers import sync_builtin_commands

    # Remove the symbol so the in-function `from ... import
    # get_registered_commands` raises ImportError.
    monkeypatch.delattr(sc, "get_registered_commands", raising=False)
    with caplog.at_level(logging.WARNING):
        result = sync_builtin_commands()
    assert result == []
    assert any(
        "System-A command sync skipped" in r.message
        for r in caplog.records
    ), "an import-drift failure must surface a WARN, not return [] silently"


# ── M4 — indicator-override warning logs once per process ────────────


def test_m4_indicator_override_warning_logs_once(caplog) -> None:  # noqa: ANN001
    from opencomputer.cli_ui import streaming

    streaming._INDICATOR_OVERRIDE_WARNED = False
    try:
        with caplog.at_level(logging.WARNING):
            streaming._warn_indicator_override_once(RuntimeError("boom-1"))
            streaming._warn_indicator_override_once(RuntimeError("boom-2"))
        hits = [
            r for r in caplog.records
            if "busy-indicator override failed" in r.message
        ]
        assert len(hits) == 1, (
            "the spinner render hot path must warn ONCE, not per frame; "
            f"got {len(hits)}"
        )
        assert "boom-1" in hits[0].message
    finally:
        streaming._INDICATOR_OVERRIDE_WARNED = False


# ── M5 — install_markdown_commands is truly idempotent ───────────────


def test_m5_deleted_markdown_command_disappears_on_reinstall(  # noqa: ANN001
    tmp_path
) -> None:
    """A ``.md`` file deleted between two ``install_markdown_commands``
    calls must make its ``/command`` disappear — not ghost-survive."""
    from opencomputer.cli_ui import slash, slash_handlers
    from opencomputer.cli_ui.slash import resolve_command
    from opencomputer.cli_ui.slash_handlers import install_markdown_commands

    reg = list(slash.SLASH_REGISTRY)
    lookup = dict(slash._LOOKUP)
    handlers = dict(slash_handlers._HANDLERS)
    installed = list(slash_handlers._INSTALLED_MARKDOWN_NAMES)
    try:
        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "alpha.md").write_text("Alpha body.", encoding="utf-8")
        (cmd_dir / "beta.md").write_text("Beta body.", encoding="utf-8")
        install_markdown_commands(tmp_path)
        assert resolve_command("alpha") is not None
        assert resolve_command("beta") is not None

        # Delete beta.md and re-install — /beta must be GONE.
        (cmd_dir / "beta.md").unlink()
        install_markdown_commands(tmp_path)
        assert resolve_command("alpha") is not None
        assert resolve_command("beta") is None, (
            "a deleted .md file's command must disappear on re-install"
        )
        assert "beta" not in slash_handlers._HANDLERS, (
            "a deleted .md file's handler must be dropped too"
        )
    finally:
        slash.SLASH_REGISTRY[:] = reg
        slash._LOOKUP.clear()
        slash._LOOKUP.update(lookup)
        slash_handlers._HANDLERS.clear()
        slash_handlers._HANDLERS.update(handlers)
        slash_handlers._INSTALLED_MARKDOWN_NAMES[:] = installed


# ── M6 — preview_skin survives a malformed / hostile hex ─────────────


def test_m6_preview_skin_survives_malformed_hex(monkeypatch) -> None:  # noqa: ANN001
    """A malformed hex (or a hostile user-skin value like ``red [bold]``)
    must render as ``?`` — never crash the whole ``oc skin preview``."""
    from opencomputer import cli_skin
    from opencomputer.cli_ui import skin as skin_mod

    class _FakeSpec:
        name = "evil"
        description = ""
        colors = {"good": "#ff0000", "bad": "red [bold]"}

    monkeypatch.setattr(skin_mod, "load_skin", lambda _n: _FakeSpec())
    result = CliRunner().invoke(cli_skin.skin_app, ["preview", "evil"])
    assert result.exit_code == 0, (
        f"preview must not crash on a malformed hex; output={result.output!r}"
    )
    assert "good" in result.output and "bad" in result.output
