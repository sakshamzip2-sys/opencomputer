"""Tests for /footer on|off|status persistence — Wave 5 deferral T4 closure.

Verifies the slash handler reads/writes ``display.runtime_footer.enabled``
to ``~/.opencomputer/<profile>/config.yaml`` (or the profile-specific path
returned by ``opencomputer.agent.config._home``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from rich.console import Console

from opencomputer.cli_ui.slash_handlers import (
    SlashContext,
    _handle_footer,
)


@dataclass
class _StubSessionConfig:
    db_path: Path


@dataclass
class _StubConfig:
    session: _StubSessionConfig


@dataclass
class _StubRuntime:
    custom: dict[str, Any]


def _make_ctx(home: Path) -> SlashContext:
    db = home / "sessions.db"
    cfg = _StubConfig(session=_StubSessionConfig(db_path=db))
    import io as _io
    ctx = SlashContext(
        console=Console(file=_io.StringIO()),
        session_id="t",
        config=cfg,
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=lambda: [],
    )
    # The handler reads/sets ctx.runtime.custom — give it a stub.
    ctx.runtime = _StubRuntime(custom={})  # type: ignore[attr-defined]
    return ctx


def _patched_home(monkeypatch, home: Path) -> None:
    """Redirect ``opencomputer.agent.config._home()`` to ``home``."""
    monkeypatch.setattr(
        "opencomputer.agent.config._home",
        lambda: home,
    )


def test_footer_status_default_off(monkeypatch, tmp_path):
    _patched_home(monkeypatch, tmp_path)
    ctx = _make_ctx(tmp_path)
    r = _handle_footer(ctx, [])
    assert r.handled is True
    # No config.yaml exists yet → resolved as off; no write triggered.
    assert not (tmp_path / "config.yaml").exists()


def test_footer_on_writes_config(monkeypatch, tmp_path):
    _patched_home(monkeypatch, tmp_path)
    ctx = _make_ctx(tmp_path)
    _handle_footer(ctx, ["on"])
    cfg_path = tmp_path / "config.yaml"
    assert cfg_path.exists()
    cfg = yaml.safe_load(cfg_path.read_text())
    assert cfg["display"]["runtime_footer"]["enabled"] is True


def test_footer_off_writes_false(monkeypatch, tmp_path):
    _patched_home(monkeypatch, tmp_path)
    ctx = _make_ctx(tmp_path)
    _handle_footer(ctx, ["on"])  # set on first
    _handle_footer(ctx, ["off"])
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["display"]["runtime_footer"]["enabled"] is False


def test_footer_round_trip_status_reflects_write(monkeypatch, tmp_path):
    _patched_home(monkeypatch, tmp_path)
    ctx = _make_ctx(tmp_path)
    _handle_footer(ctx, ["on"])
    # Status read after write should reflect on (no exception).
    r = _handle_footer(ctx, ["status"])
    assert r.handled is True


def test_footer_on_updates_runtime_context(monkeypatch, tmp_path):
    """Toggling immediately reflects in runtime.custom so next turn sees it."""
    _patched_home(monkeypatch, tmp_path)
    ctx = _make_ctx(tmp_path)
    _handle_footer(ctx, ["on"])
    assert ctx.runtime.custom.get("show_footer") is True  # type: ignore[attr-defined]
    _handle_footer(ctx, ["off"])
    assert ctx.runtime.custom.get("show_footer") is False  # type: ignore[attr-defined]


def test_footer_preserves_existing_config_keys(monkeypatch, tmp_path):
    """A prior config with unrelated keys must survive the write."""
    _patched_home(monkeypatch, tmp_path)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({"model": {"name": "anthropic:claude-opus-4-7"}}),
    )
    ctx = _make_ctx(tmp_path)
    _handle_footer(ctx, ["on"])
    cfg = yaml.safe_load(cfg_path.read_text())
    assert cfg["model"]["name"] == "anthropic:claude-opus-4-7"
    assert cfg["display"]["runtime_footer"]["enabled"] is True


def test_footer_unknown_subcommand_treated_as_status(monkeypatch, tmp_path):
    _patched_home(monkeypatch, tmp_path)
    ctx = _make_ctx(tmp_path)
    r = _handle_footer(ctx, ["bogus"])
    assert r.handled is True
    # No write triggered for unknown subcommand
    assert not (tmp_path / "config.yaml").exists()
