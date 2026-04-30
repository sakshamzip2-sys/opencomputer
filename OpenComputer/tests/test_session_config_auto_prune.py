"""SessionConfig auto-prune fields + YAML loader integration."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from opencomputer.agent.config import SessionConfig
from opencomputer.agent.config_store import load_config


def test_session_config_existing_fields_preserved() -> None:
    cfg = SessionConfig()
    assert cfg.session_id is None
    assert str(cfg.db_path).endswith("sessions.db")


def test_session_config_auto_prune_defaults() -> None:
    cfg = SessionConfig()
    assert cfg.auto_prune_days == 0  # disabled by default
    assert cfg.auto_prune_untitled_days == 0  # default OFF (opt-in)
    assert cfg.auto_prune_min_messages == 3


def test_load_config_reads_auto_prune_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "session": {
                    "auto_prune_days": 90,
                    "auto_prune_untitled_days": 14,
                }
            }
        )
    )
    cfg = load_config(cfg_path)
    assert cfg.session.auto_prune_days == 90
    assert cfg.session.auto_prune_untitled_days == 14
    assert cfg.session.auto_prune_min_messages == 3  # default kept


def test_load_config_missing_session_block_uses_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": {"name": "claude-sonnet-4"}}))
    cfg = load_config(cfg_path)
    assert cfg.session.auto_prune_days == 0
