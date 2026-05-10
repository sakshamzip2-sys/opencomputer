"""Goal config slots — goals.max_turns + auxiliary.goal_judge.{provider,model}.

Spec: docs/superpowers/specs/2026-05-08-kanban-goals-v2-design.md §3 Gap C.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

from opencomputer.agent.config import (
    AuxiliaryConfig,
    Config,
    GoalJudgeConfig,
    GoalsConfig,
    default_config,
)
from opencomputer.agent.config_store import load_config


def test_default_goals_max_turns_is_20():
    cfg = default_config()
    assert cfg.goals.max_turns == 20


def test_default_goal_judge_is_unset():
    cfg = default_config()
    assert cfg.auxiliary.goal_judge.provider is None
    assert cfg.auxiliary.goal_judge.model is None


def test_yaml_overrides_max_turns(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"goals": {"max_turns": 50}}))
    cfg = load_config(p)
    assert cfg.goals.max_turns == 50


def test_yaml_sets_goal_judge_provider_model(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "auxiliary": {
                    "goal_judge": {
                        "provider": "openrouter",
                        "model": "google/gemini-3-flash-preview",
                    }
                }
            }
        )
    )
    cfg = load_config(p)
    assert cfg.auxiliary.goal_judge.provider == "openrouter"
    assert cfg.auxiliary.goal_judge.model == "google/gemini-3-flash-preview"


def test_set_session_goal_uses_config_max_turns(tmp_path: Path, monkeypatch):
    """Omitted budget falls back to GoalsConfig.max_turns."""
    from opencomputer.agent import config as config_mod
    from opencomputer.agent.state import SessionDB

    fake_cfg = dataclasses.replace(
        default_config(),
        goals=GoalsConfig(max_turns=42),
        auxiliary=AuxiliaryConfig(),
    )
    monkeypatch.setattr(config_mod, "default_config", lambda: fake_cfg)

    db = SessionDB(tmp_path / "sessions.db")
    sid = "s_x"
    db.ensure_session(sid, platform="cli", model="x", cwd=None)
    db.set_session_goal(sid, text="t")  # no budget kwarg
    g = db.get_session_goal(sid)
    assert g is not None
    assert g.budget == 42


def test_goal_judge_dataclass_immutability():
    """frozen + slots — instances refuse mutation."""
    cfg = GoalJudgeConfig(provider="x", model="y")
    import pytest

    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        cfg.provider = "z"  # type: ignore[misc]
