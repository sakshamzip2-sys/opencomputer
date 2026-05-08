"""Tests for top-level `timezone:` IANA config (Hermes config v2)."""
from __future__ import annotations

import zoneinfo
from datetime import datetime
from pathlib import Path

import pytest

from opencomputer.agent.config import (
    Config,
    default_config,
    now_in_tz,
    resolve_tzinfo,
)
from opencomputer.agent.config_store import load_config


def test_default_timezone_is_empty() -> None:
    cfg = default_config()
    assert cfg.timezone == ""


def test_load_config_accepts_valid_iana(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("timezone: \"America/New_York\"\n", encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.timezone == "America/New_York"
    # Confirm zoneinfo accepts it.
    zoneinfo.ZoneInfo(cfg.timezone)


def test_load_config_rejects_invalid_iana(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("timezone: \"Mars/Olympus_Mons\"\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="timezone"):
        load_config(cfg_path)


def test_load_config_accepts_empty_timezone(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("timezone: \"\"\n", encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.timezone == ""


def test_resolve_tzinfo_returns_zoneinfo_when_set() -> None:
    cfg = Config(timezone="America/New_York")
    tz = resolve_tzinfo(cfg)
    assert isinstance(tz, zoneinfo.ZoneInfo)
    assert str(tz) == "America/New_York"


def test_resolve_tzinfo_returns_none_when_empty() -> None:
    cfg = Config(timezone="")
    assert resolve_tzinfo(cfg) is None


def test_now_in_tz_uses_configured_zone() -> None:
    cfg = Config(timezone="UTC")
    now = now_in_tz(cfg)
    assert now.tzinfo is not None
    assert str(now.tzinfo) == "UTC"


def test_now_in_tz_naive_when_unset() -> None:
    cfg = Config(timezone="")
    now = now_in_tz(cfg)
    assert now.tzinfo is None
