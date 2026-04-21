"""
Typed configuration — replaces the 58-parameter __init__ nightmare.

All agent config lives in small, composable dataclasses. Load from
~/.opencomputer/config.yaml (or TOML — TBD). Environment variables
can override individual fields.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _home() -> Path:
    """Return ~/.opencomputer/, creating it if needed."""
    home = Path(os.environ.get("OPENCOMPUTER_HOME", Path.home() / ".opencomputer"))
    home.mkdir(parents=True, exist_ok=True)
    return home


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """Which LLM to use and how."""

    provider: str = "anthropic"  # maps to a provider plugin name
    model: str = "claude-opus-4-7"
    max_tokens: int = 4096
    temperature: float = 1.0
    api_key_env: str = "ANTHROPIC_API_KEY"


@dataclass(frozen=True, slots=True)
class LoopConfig:
    """Behavior of the main agent loop."""

    max_iterations: int = 50
    parallel_tools: bool = True
    iteration_timeout_s: int = 600


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Where sessions are stored and how."""

    db_path: Path = field(default_factory=lambda: _home() / "sessions.db")
    session_id: str | None = None  # None = create new session each run


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """The three-pillar memory configuration."""

    declarative_path: Path = field(default_factory=lambda: _home() / "MEMORY.md")
    skills_path: Path = field(default_factory=lambda: _home() / "skills")
    # episodic memory uses SessionConfig.db_path


@dataclass(frozen=True, slots=True)
class Config:
    """Root configuration — composed of small focused configs."""

    model: ModelConfig = field(default_factory=ModelConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    home: Path = field(default_factory=_home)


def default_config() -> Config:
    """Return the default configuration with filesystem-appropriate paths."""
    return Config()


__all__ = [
    "Config",
    "ModelConfig",
    "LoopConfig",
    "SessionConfig",
    "MemoryConfig",
    "default_config",
]
