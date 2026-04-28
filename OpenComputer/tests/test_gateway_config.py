"""Tests for :class:`GatewayConfig` (PR #221 follow-up).

The Gateway used to hard-code ``photo_burst_window=0.8`` because the
config dict it threaded into Dispatch was never read from a user-facing
config surface. These tests pin the new wiring:

* ``Config.gateway.photo_burst_window`` round-trips through
  ``save_config`` / ``load_config``.
* :class:`Gateway` propagates the configured value into
  :attr:`Dispatch._burst_window_seconds`.
* Default behaviour (no override) preserves the historical 0.8s window.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from opencomputer.agent.config import Config, GatewayConfig
from opencomputer.agent.config_store import load_config, save_config
from opencomputer.gateway.server import Gateway


def _fake_loop() -> MagicMock:
    """Stand-in AgentLoop. Gateway.__init__ only stores ``self.loop`` and
    Dispatch reads ``loop._consent_gate`` (returns None for missing attr
    when the mock is configured with ``spec=None``)."""
    loop = MagicMock()
    loop._consent_gate = None  # explicit so getattr returns None, not a Mock
    return loop


# ── default behaviour ────────────────────────────────────────────


def test_photo_burst_window_default_unchanged() -> None:
    """No GatewayConfig argument → legacy 0.8s preserved."""
    gw = Gateway(_fake_loop())
    assert gw.dispatch._burst_window_seconds == 0.8


def test_photo_burst_window_explicit_default_is_0_8() -> None:
    """Constructing GatewayConfig() yields the documented default."""
    cfg = GatewayConfig()
    assert cfg.photo_burst_window == 0.8
    gw = Gateway(_fake_loop(), config=cfg)
    assert gw.dispatch._burst_window_seconds == 0.8


# ── override propagation ─────────────────────────────────────────


def test_photo_burst_window_from_config_propagates() -> None:
    """User-facing override reaches Dispatch."""
    cfg = GatewayConfig(photo_burst_window=0.5)
    gw = Gateway(_fake_loop(), config=cfg)
    assert gw.dispatch._burst_window_seconds == 0.5


def test_photo_burst_window_zero_disables_burst() -> None:
    """``0.0`` is a legitimate "fire immediately" override."""
    cfg = GatewayConfig(photo_burst_window=0.0)
    gw = Gateway(_fake_loop(), config=cfg)
    assert gw.dispatch._burst_window_seconds == 0.0


def test_photo_burst_window_high_value_propagates() -> None:
    """Sanity: arbitrary positive floats round-trip."""
    cfg = GatewayConfig(photo_burst_window=2.5)
    gw = Gateway(_fake_loop(), config=cfg)
    assert gw.dispatch._burst_window_seconds == 2.5


# ── YAML round-trip via Config / config_store ────────────────────


def test_gateway_config_round_trips_through_yaml(tmp_path: Path) -> None:
    """``Config.gateway`` survives ``save_config`` → ``load_config``."""
    cfg_path = tmp_path / "config.yaml"
    cfg = Config(gateway=GatewayConfig(photo_burst_window=1.25))
    save_config(cfg, cfg_path)
    reloaded = load_config(cfg_path)
    assert reloaded.gateway.photo_burst_window == 1.25


def test_default_config_includes_gateway_block() -> None:
    """``default_config().gateway`` exists and matches the documented
    default — guards against accidentally dropping the new field."""
    from opencomputer.agent.config import default_config

    cfg = default_config()
    assert hasattr(cfg, "gateway")
    assert isinstance(cfg.gateway, GatewayConfig)
    assert cfg.gateway.photo_burst_window == 0.8
