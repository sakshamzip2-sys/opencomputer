"""M3 #7 fix — display.persona_override suppresses the casual register.

The gateway threads ``display.persona_override`` onto the runtime as
``persona_disabled`` (value none/off) or ``persona_id_override`` (a
pinned persona id). ``_build_persona_overlay`` honours both.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from opencomputer.agent.config import (
    Config,
    LoopConfig,
    MemoryConfig,
    ModelConfig,
    SessionConfig,
)
from opencomputer.agent.loop import AgentLoop
from plugin_sdk.runtime_context import RuntimeContext


def _loop(tmp: Path) -> AgentLoop:
    cfg = Config(
        model=ModelConfig(provider="mock", model="mock-model"),
        loop=LoopConfig(max_iterations=2),
        session=SessionConfig(db_path=tmp / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp / "MEMORY.md", skills_path=tmp / "skills"
        ),
    )
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    return AgentLoop(provider=MagicMock(), config=cfg, compaction_disabled=True)


def test_persona_disabled_returns_empty_overlay(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    loop._runtime = RuntimeContext(custom={"persona_disabled": True})
    overlay = loop._build_persona_overlay("s1", "hello")
    assert overlay == ""
    assert loop._active_persona_id == ""


def test_persona_disabled_takes_precedence_over_id_override(
    tmp_path: Path,
) -> None:
    """persona_disabled wins even if a persona id is also pinned."""
    loop = _loop(tmp_path)
    loop._runtime = RuntimeContext(
        custom={"persona_disabled": True, "persona_id_override": "trading"}
    )
    assert loop._build_persona_overlay("s1", "hi") == ""


def test_no_override_runs_the_classifier(tmp_path: Path) -> None:
    """Without an override the classifier still runs (returns a str,
    possibly empty — we only assert it does not crash and is a str)."""
    loop = _loop(tmp_path)
    loop._runtime = RuntimeContext(custom={})
    overlay = loop._build_persona_overlay("s1", "hi")
    assert isinstance(overlay, str)
