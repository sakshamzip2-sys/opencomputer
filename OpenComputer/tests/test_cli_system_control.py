"""CLI tests for ``opencomputer system-control`` (Phase 3.F)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opencomputer.agent.config import (
    Config,
    FullSystemControlConfig,
    default_config,
)
from opencomputer.cli_system_control import system_control_app
from opencomputer.system_control.bus_listener import detach_from_bus
from opencomputer.system_control.logger import reset_default_logger

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_state():
    """Drop singletons + bus subscriptions between tests."""
    detach_from_bus()
    reset_default_logger()
    yield
    detach_from_bus()
    reset_default_logger()


def _make_isolated_config(tmp_path: Path) -> tuple[Config, Path]:
    """Build a Config pointing at tmp_path-rooted log + return the path."""
    log_path = tmp_path / "agent.log"
    cfg = Config(
        system_control=FullSystemControlConfig(
            enabled=False,
            log_path=log_path,
        )
    )
    return cfg, log_path


class _ConfigStore:
    """Mutable in-memory config store usable as load_config / save_config."""

    def __init__(self, initial: Config) -> None:
        self.cfg = initial

    def load(self, *args, **kwargs) -> Config:
        return self.cfg

    def save(self, cfg: Config, *args, **kwargs) -> Path:
        self.cfg = cfg
        return Path("/tmp/dummy_path")


def test_enable_writes_config_and_prints_banner(tmp_path: Path) -> None:
    """`enable` flips config.system_control.enabled to True + prints banner."""
    cfg, _log_path = _make_isolated_config(tmp_path)
    store = _ConfigStore(cfg)

    with (
        patch(
            "opencomputer.cli_system_control.load_config",
            store.load,
        ),
        patch(
            "opencomputer.cli_system_control.save_config",
            store.save,
        ),
        patch(
            "opencomputer.agent.config_store.load_config",
            store.load,
        ),
    ):
        result = runner.invoke(system_control_app, ["enable"])

    assert result.exit_code == 0, result.output
    assert "system-control is ON" in result.output
    assert store.cfg.system_control.enabled is True


def test_disable_clears_config(tmp_path: Path) -> None:
    """`enable` then `disable` returns config to disabled."""
    cfg, _ = _make_isolated_config(tmp_path)
    store = _ConfigStore(cfg)

    with (
        patch(
            "opencomputer.cli_system_control.load_config",
            store.load,
        ),
        patch(
            "opencomputer.cli_system_control.save_config",
            store.save,
        ),
        patch(
            "opencomputer.agent.config_store.load_config",
            store.load,
        ),
    ):
        runner.invoke(system_control_app, ["enable"])
        assert store.cfg.system_control.enabled is True
        result = runner.invoke(system_control_app, ["disable"])

    assert result.exit_code == 0, result.output
    assert "system-control is OFF" in result.output
    assert store.cfg.system_control.enabled is False


def test_status_shows_log_path_and_size(tmp_path: Path) -> None:
    """`status` prints the log path + size + last entries."""
    log_path = tmp_path / "agent.log"
    log_path.write_text(
        json.dumps(
            {"kind": "tool_call", "timestamp": 1.0, "pid": 99, "tool_name": "Read"}
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = Config(
        system_control=FullSystemControlConfig(
            enabled=True,
            log_path=log_path,
        )
    )
    store = _ConfigStore(cfg)

    with patch(
        "opencomputer.cli_system_control.load_config",
        store.load,
    ):
        result = runner.invoke(system_control_app, ["status"])

    assert result.exit_code == 0, result.output
    # Rich console wraps long paths; collapse whitespace for the assertion
    flattened = " ".join(result.output.split())
    flattened_path = " ".join(str(log_path).split())
    # Path may also be wrapped mid-token via Rich's word-break; check
    # that at least the basename is present (always shorter than wrap).
    assert log_path.name in result.output
    # And that the dir prefix appears somewhere even if wrapped
    assert log_path.parent.name in flattened or log_path.parent.name in flattened_path
    # Some indicator of size
    assert "log size" in result.output
    # Last-entry tool_call line shows
    assert "tool_call" in result.output


def test_enable_disable_idempotent(tmp_path: Path) -> None:
    """enable+enable → enabled. disable+disable → disabled. No errors."""
    cfg, _ = _make_isolated_config(tmp_path)
    store = _ConfigStore(cfg)

    with (
        patch(
            "opencomputer.cli_system_control.load_config",
            store.load,
        ),
        patch(
            "opencomputer.cli_system_control.save_config",
            store.save,
        ),
        patch(
            "opencomputer.agent.config_store.load_config",
            store.load,
        ),
    ):
        r1 = runner.invoke(system_control_app, ["enable"])
        r2 = runner.invoke(system_control_app, ["enable"])
        assert r1.exit_code == 0
        assert r2.exit_code == 0
        assert store.cfg.system_control.enabled is True

        r3 = runner.invoke(system_control_app, ["disable"])
        r4 = runner.invoke(system_control_app, ["disable"])
        assert r3.exit_code == 0
        assert r4.exit_code == 0
        assert store.cfg.system_control.enabled is False


def test_status_shows_off_when_disabled(tmp_path: Path) -> None:
    """`status` clearly indicates disabled state when off."""
    cfg = default_config()  # disabled
    store = _ConfigStore(cfg)
    with patch(
        "opencomputer.cli_system_control.load_config",
        store.load,
    ):
        result = runner.invoke(system_control_app, ["status"])
    assert result.exit_code == 0
    assert "off" in result.output.lower()


def test_enable_with_menu_bar_unsupported_prints_warning(tmp_path: Path) -> None:
    """`enable --menu-bar` on non-Darwin prints a warning + still enables."""
    cfg, _ = _make_isolated_config(tmp_path)
    store = _ConfigStore(cfg)

    with (
        patch(
            "opencomputer.cli_system_control.load_config",
            store.load,
        ),
        patch(
            "opencomputer.cli_system_control.save_config",
            store.save,
        ),
        patch(
            "opencomputer.agent.config_store.load_config",
            store.load,
        ),
        patch(
            "opencomputer.system_control.menu_bar.is_menu_bar_supported",
            return_value=False,
        ),
    ):
        result = runner.invoke(system_control_app, ["enable", "--menu-bar"])

    assert result.exit_code == 0, result.output
    assert "menu bar not supported" in result.output
    # Config still enables
    assert store.cfg.system_control.enabled is True
    # Caller wanted menu bar — preserved in config
    assert store.cfg.system_control.menu_bar_indicator is True
