"""Typed records for the handoff subsystem.

All public dataclasses are frozen+slotted — handoff state flows one-way
(generated → written → read → consumed), and accidental mutation by a
downstream consumer must surface as TypeError, not as a silent corruption
of the inbox file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal


class HandoffWarranted(Enum):
    """Outcome of protocol Step 0 — 'is a handoff warranted?'."""
    YES = "yes"
    NO_TRIVIAL = "no_trivial"     # single Q&A, no substantive thread
    NO_EMPTY = "no_empty"          # no user messages yet
    NO_COMPLETED = "no_completed"  # task complete, nothing to carry


@dataclass(frozen=True, slots=True)
class HandoffMetadata:
    """YAML-frontmatter shape persisted to disk in handoff_*.md files.

    All fields are persisted and re-read; adding a field is a non-breaking
    change only when it has a default (forward-compatibility for inboxes
    written by a newer client into a profile still running an older one).
    """

    #: ``"handoff-v2"`` — drives reader dispatch. Bumping requires
    #: shipping a new reader BEFORE any writer emits the new version.
    protocol_version: Literal["handoff-v2"]
    #: Profile that wrote this handoff (e.g. ``"default"``).
    source_profile: str
    #: Profile this handoff was written for (e.g. ``"stocks"``).
    target_profile: str
    #: ISO-8601 UTC timestamp of generation (``...Z`` suffix).
    generated_at: str
    #: SessionDB id the handoff was generated from. Useful for audit
    #: cross-reference; not used to read content (handoffs are
    #: self-contained per protocol R10).
    source_session_id: str
    #: One of ``"auto"``, ``"manual"``, ``"cli"`` — populated by the
    #: caller (trigger or slash command) for downstream telemetry.
    trigger: Literal["auto", "manual", "cli"]
    #: Classifier confidence on the turn that triggered the swap, if
    #: ``trigger == "auto"``. ``None`` for manual/cli.
    classifier_confidence: float | None = None
    #: Brief classifier reason string — preserved verbatim for debugging.
    classifier_reason: str | None = None


@dataclass(frozen=True, slots=True)
class HandoffDocument:
    """A complete handoff: metadata + body.

    ``body`` is the markdown content the outgoing model produced. Per
    protocol R10 the body is portable plain markdown — no model-specific
    syntax, no tool-call shapes, no platform commands. The reader treats
    it as data (R12).
    """

    metadata: HandoffMetadata
    body: str
    #: Path on disk after a successful inbox write. Empty before write.
    path: Path = field(default_factory=lambda: Path())

    def with_path(self, path: Path) -> HandoffDocument:
        """Return a copy with ``path`` populated (frozen dataclass copy)."""
        return HandoffDocument(metadata=self.metadata, body=self.body, path=path)


__all__ = ["HandoffDocument", "HandoffMetadata", "HandoffWarranted"]
