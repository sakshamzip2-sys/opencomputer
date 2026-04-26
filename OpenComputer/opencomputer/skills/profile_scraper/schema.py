"""profile-scraper schema — frozen ProfileFact dataclass + Snapshot envelope."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ProfileFact:
    """One observed fact about the user."""

    field: str
    value: Any
    source: str
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A complete scrape's output — list of facts + provenance metadata."""

    facts: tuple[ProfileFact, ...]
    started_at: float
    ended_at: float
    sources_attempted: tuple[str, ...]
    sources_succeeded: tuple[str, ...]
