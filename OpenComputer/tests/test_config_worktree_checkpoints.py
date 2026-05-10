"""Test the new WorktreeConfig and CheckpointsConfig dataclasses + Config wiring."""
from __future__ import annotations

import pytest

from opencomputer.agent.config import (
    CheckpointsConfig,
    Config,
    WorktreeConfig,
    default_config,
)


def test_worktree_config_defaults() -> None:
    cfg = WorktreeConfig()
    assert cfg.include_max_total_mb == 1000
    assert cfg.include_max_per_file_mb == 500
    assert cfg.include_global_fallback is True
    assert cfg.include_follow_symlinks is False


def test_checkpoints_config_defaults() -> None:
    cfg = CheckpointsConfig()
    assert cfg.enabled is True
    assert cfg.max_snapshots == 50
    assert cfg.max_total_size_mb == 1000
    assert cfg.max_file_size_mb == 50
    assert cfg.auto_prune is True
    assert cfg.retention_days == 30
    assert cfg.min_interval_hours == 24
    assert cfg.delete_orphans is True


def test_config_exposes_worktree_and_checkpoints() -> None:
    cfg = default_config()
    assert isinstance(cfg.worktree, WorktreeConfig)
    assert isinstance(cfg.checkpoints, CheckpointsConfig)


def test_worktree_config_frozen() -> None:
    cfg = WorktreeConfig()
    with pytest.raises((AttributeError, Exception)):
        cfg.include_max_total_mb = 9999  # type: ignore[misc]


def test_checkpoints_config_frozen() -> None:
    cfg = CheckpointsConfig()
    with pytest.raises((AttributeError, Exception)):
        cfg.enabled = False  # type: ignore[misc]
