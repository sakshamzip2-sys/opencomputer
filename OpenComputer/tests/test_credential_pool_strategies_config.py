"""T6 — `credential_pool_strategies` config knob (per-provider rotation)."""

from __future__ import annotations

import logging

import yaml

from opencomputer.agent.config import default_config
from opencomputer.agent.config_store import _apply_overrides
from opencomputer.agent.credential_sources import resolve_pool_strategy


def test_default_config_has_empty_strategies():
    cfg = default_config()
    assert cfg.credential_pool_strategies == {}


def test_yaml_loads_strategies():
    cfg = default_config()
    raw = yaml.safe_load(
        """
credential_pool_strategies:
  openrouter: round_robin
  anthropic: least_used
"""
    )
    out = _apply_overrides(cfg, raw)
    assert out.credential_pool_strategies["openrouter"] == "round_robin"
    assert out.credential_pool_strategies["anthropic"] == "least_used"


def test_resolve_strategy_falls_back_to_least_used():
    cfg = default_config()
    assert resolve_pool_strategy(cfg, "openrouter") == "least_used"


def test_resolve_strategy_valid_returns_configured():
    cfg = default_config()
    raw = yaml.safe_load(
        """
credential_pool_strategies:
  openrouter: round_robin
"""
    )
    cfg = _apply_overrides(cfg, raw)
    assert resolve_pool_strategy(cfg, "openrouter") == "round_robin"


def test_resolve_strategy_unknown_value_warns_and_falls_back(caplog):
    cfg = default_config()
    raw = yaml.safe_load(
        """
credential_pool_strategies:
  openrouter: made_up_strategy
"""
    )
    cfg = _apply_overrides(cfg, raw)
    with caplog.at_level(logging.WARNING, logger="opencomputer.agent.credential_sources"):
        result = resolve_pool_strategy(cfg, "openrouter")
    assert result == "least_used"
    assert any("made_up_strategy" in rec.message for rec in caplog.records)


def test_round_trip_via_save_config(tmp_path, monkeypatch):
    """credential_pool_strategies survives save_config + load round-trip."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent.config_store import save_config

    cfg = default_config()
    cls = type(cfg)
    cfg = cls(
        **{
            **{
                f.name: getattr(cfg, f.name)
                for f in cfg.__dataclass_fields__.values()
            },
            "credential_pool_strategies": {"openrouter": "random"},
        }
    )
    save_config(cfg, tmp_path / "config.yaml")
    written = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert written["credential_pool_strategies"] == {"openrouter": "random"}
