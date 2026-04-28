"""Public SkillSource ABC + dataclasses for the Skills Hub.

Plugins and OC's bundled hub adapters both implement SkillSource. Skill metadata
flowing through the hub system is a SkillMeta; full installable content is a
SkillBundle.

This module is part of the public plugin SDK. It MUST NOT import from
opencomputer/* — the SDK boundary test enforces this.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

TrustLevel = Literal["builtin", "trusted", "community", "untrusted"]
_VALID_TRUST: tuple[str, ...] = ("builtin", "trusted", "community", "untrusted")


@dataclass(frozen=True, slots=True)
class SkillMeta:
    """Lightweight skill descriptor returned by SkillSource.search/inspect.

    Identifier MUST be ``<source>/<name>`` form so a router can route fetch()
    calls back to the right source.
    """
    identifier: str
    name: str
    description: str
    source: str
    version: str | None = None
    author: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    trust_level: TrustLevel = "community"
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.trust_level not in _VALID_TRUST:
            raise ValueError(
                f"trust_level must be one of {_VALID_TRUST}, got {self.trust_level!r}"
            )


@dataclass(frozen=True, slots=True)
class SkillBundle:
    """Full installable content of a skill — SKILL.md plus any auxiliary files."""
    identifier: str
    skill_md: str
    files: dict[str, str]


class SkillSource(ABC):
    """Abstract base class for skill registry adapters.

    Implementations:
    - Return a stable ``name`` (e.g. "well-known", "github", "agentskills_io").
    - Implement ``search()``, ``fetch()``, and ``inspect()``.
    - Are stateless or carry only their own auth/config.
    - Raise nothing on partial failure — return [] from search, None from
      fetch/inspect when the identifier is unknown. Network errors should be
      logged but not raised so the router can fall through to other sources.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique source name used as the identifier prefix."""

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        """Return up to ``limit`` skills matching the query string."""

    @abstractmethod
    def fetch(self, identifier: str) -> SkillBundle | None:
        """Return the full bundle for an identifier, or None if unknown."""

    @abstractmethod
    def inspect(self, identifier: str) -> SkillMeta | None:
        """Return rich metadata for an identifier, or None if unknown."""


__all__ = ["SkillSource", "SkillMeta", "SkillBundle", "TrustLevel"]
