"""
Behavioral inference primitives — the public ``Motif`` vocabulary (Phase 3.B, F2).

This module is the SDK contract for the behavioral-inference engine
shipped in :mod:`opencomputer.inference`. Plugins (and Phase 3.C's
user-model graph) read :class:`Motif` records persisted by the
engine without reaching into ``opencomputer/*`` internals.

A :class:`Motif` is a low-cost, structured "thing we noticed" derived
from a window of :class:`plugin_sdk.ingestion.SignalEvent` values. The
three concrete extractor kinds shipped at this phase are heuristic-only:

* ``"temporal"``    — recurring (hour-of-day, day-of-week) usage buckets
* ``"transition"``  — adjacent-event follow-on counts
* ``"implicit_goal"`` — top-N tool sequence summary per session

The :class:`MotifExtractor` Protocol stays the same when the
implementation is later swapped for an LLM-judged variant; downstream
consumers (Phase 3.C) shouldn't need to change.

Stability contract
------------------

This module is part of the public SDK. Once the :class:`Motif` field
set is shipped, renaming / removing / re-typing a field is a **breaking
change**. Adding optional fields with safe defaults is fine; new
``kind`` literal values require a version bump on consumer code that
matches on the discriminator.

Privacy posture
---------------

Mirrors the bus's "metadata only" stance: motif payloads carry counts,
labels, hours-of-day, and event ids — never raw user content. A
downstream consumer that wants the underlying message body retrieves
it from SessionDB by ``session_id`` + ``event_id``, identical to the
event-bus pattern.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from plugin_sdk.ingestion import SignalEvent

#: The three motif kinds shipped at Phase 3.B. New kinds require a
#: schema version bump on :class:`opencomputer.inference.storage.MotifStore`.
MotifKind = Literal["temporal", "transition", "implicit_goal"]


@dataclass(frozen=True, slots=True)
class Motif:
    """A structured pattern observed across a window of signal events.

    Attributes
    ----------
    motif_id:
        Stable UUID4 per motif, auto-generated on construction. Acts as
        the primary key in :class:`MotifStore` and the dedup key for
        downstream consumers.
    kind:
        Discriminator literal — one of :data:`MotifKind`. Determines
        the shape of ``payload``.
    confidence:
        Range ``[0.0, 1.0]``. Heuristic at this phase; later LLM-judge
        variants may calibrate. Consumers should treat low-confidence
        motifs as noise.
    support:
        Number of events that contributed to the motif. Useful for
        ranking and for the heuristic confidence formula.
    summary:
        One-line human-readable description. Safe for direct display
        in CLI / dashboard tables — does not embed raw user content.
    payload:
        Kind-specific JSON-serialisable structured data. Each extractor
        documents its payload shape. Keep entries small — this is
        persisted to SQLite as a JSON blob.
    evidence_event_ids:
        ``event_id`` values from the bus events that contributed.
        Lets a consumer pull richer context from SessionDB / scrapers
        without copying the underlying content into the motif itself.
    created_at:
        Unix epoch seconds at which the motif was observed.
    session_id:
        Optional session scope for motifs derived from a single
        session (today: ``"implicit_goal"``). ``None`` for motifs
        extracted across multiple sessions (today: ``"temporal"`` and
        ``"transition"``).
    """

    kind: MotifKind = "temporal"
    confidence: float = 0.0
    support: int = 0
    summary: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence_event_ids: tuple[str, ...] = ()
    motif_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    session_id: str | None = None


@runtime_checkable
class MotifExtractor(Protocol):
    """Contract every concrete extractor satisfies.

    Extractors are pure: ``extract(events)`` does not mutate the
    bus, the store, or any module-level state. The engine
    (:class:`opencomputer.inference.engine.BehavioralInferenceEngine`)
    is responsible for buffering events, calling extractors over a
    batch, and persisting the returned motifs.

    A ``runtime_checkable`` Protocol lets test fakes inherit from
    nothing — they just need ``name``, ``kind``, and ``extract``.
    Adding required methods to this Protocol is a breaking change.
    """

    name: ClassVar[str]
    """Stable identifier — used in logs, telemetry, and for the
    engine's per-extractor exception isolation."""

    kind: ClassVar[MotifKind]
    """Discriminator emitted on the resulting motifs. Must match
    one of the :data:`MotifKind` literals."""

    def extract(self, events: Sequence[SignalEvent]) -> list[Motif]:
        """Return zero or more motifs derived from ``events``. Pure."""
        ...


__all__ = [
    "Motif",
    "MotifExtractor",
    "MotifKind",
]
