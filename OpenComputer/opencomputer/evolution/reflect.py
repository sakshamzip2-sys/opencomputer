"""Reflection engine and Insight dataclass for OpenComputer Evolution.

``Insight`` is the output of the reflection engine — one observation about
the agent's behaviour with a proposed action.  The dataclass shape is locked
at B1 so that storage, CLI, and the synthesizer can be wired against a stable
contract.  The ``ReflectionEngine`` logic itself lands in B2.

Design reference: OpenComputer/docs/evolution/design.md §4.3 and §7.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jinja2 import Environment, FileSystemLoader

from opencomputer.evolution.trajectory import TrajectoryRecord

if TYPE_CHECKING:
    from plugin_sdk.provider_contract import BaseProvider

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "prompts"

# ---------------------------------------------------------------------------
# Valid action_type values (checked at runtime — Literal isn't enforced by Python)
# ---------------------------------------------------------------------------

_VALID_ACTION_TYPES: frozenset[str] = frozenset({"create_skill", "edit_prompt", "noop"})


# ---------------------------------------------------------------------------
# Insight dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Insight:
    """Output of reflection — one observation about the agent's behavior with a proposed action.

    Implementation logic lands in B2.  This dataclass shape is locked at B1 so consumers
    (storage / CLI / synthesis) can be wired against a stable contract.
    """

    observation: str
    """Human-readable summary of the observed pattern."""

    evidence_refs: tuple[int, ...]
    """Trajectory record ids supporting the observation.  Must be a tuple, not a list."""

    action_type: Literal["create_skill", "edit_prompt", "noop"]
    """Proposed action category."""

    payload: Mapping[str, Any]
    """Action-specific detail (slug, draft text, diff, etc.)."""

    confidence: float
    """Confidence score in [0.0, 1.0]."""

    def __post_init__(self) -> None:
        # Validate confidence is in the unit interval.
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"Insight.confidence must be in [0.0, 1.0], got {self.confidence!r}"
            )

        # Validate evidence_refs is a tuple (not a list).
        if not isinstance(self.evidence_refs, tuple):
            raise TypeError(
                f"Insight.evidence_refs must be a tuple, got {type(self.evidence_refs).__name__}"
            )

        # Validate action_type (Literal is not enforced at runtime by Python).
        if self.action_type not in _VALID_ACTION_TYPES:
            raise ValueError(
                f"Insight.action_type must be one of {sorted(_VALID_ACTION_TYPES)!r}, "
                f"got {self.action_type!r}"
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_env() -> Environment:
    """Build a Jinja2 Environment pointing at the prompts directory."""
    return Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _run_async(coro: Any) -> Any:
    """Run an async coroutine from sync code, handling existing-loop case."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to use asyncio.run
        return asyncio.run(coro)
    # We're inside a running loop (e.g., called from an async test) — refuse.
    raise RuntimeError(
        "ReflectionEngine.reflect() must not be called from inside a running event loop. "
        "Run it from sync code (CLI / background thread)."
    )


def reflect_for_eval(events: list[dict]) -> str:
    """Eval-only entry point.

    Builds a synthetic TrajectoryRecord from the structured event list,
    runs ReflectionEngine.reflect(), and returns the joined Insight texts
    for the LLM-rubric grader to score.

    The input shape mirrors the TrajectoryEvent contract — adapter
    callers MUST pass structured events, not free text. session_id is
    fixed to "_eval_synthetic" so eval-only records cannot accidentally
    pollute the production evolution store.
    """
    import time

    from opencomputer.evolution.trajectory import (
        SCHEMA_VERSION_CURRENT,
        TrajectoryEvent,
    )

    if not isinstance(events, list):
        raise ValueError(f"events must be a list, got {type(events).__name__}")

    session_id = "_eval_synthetic"
    started_at = time.time()
    traj_events: list[TrajectoryEvent] = []
    for i, ev in enumerate(events):
        traj_events.append(
            TrajectoryEvent(
                session_id=session_id,
                message_id=i,
                action_type=ev["action_type"],
                tool_name=ev.get("tool_name"),
                outcome=ev["outcome"],
                timestamp=started_at + i,
                metadata=ev.get("metadata", {}),
            )
        )

    record = TrajectoryRecord(
        id=None,
        session_id=session_id,
        schema_version=SCHEMA_VERSION_CURRENT,
        started_at=started_at,
        ended_at=started_at + len(events),
        events=tuple(traj_events),
        completion_flag=True,
    )

    engine = ReflectionEngine()
    insights = engine.reflect([record])
    return "\n".join(getattr(i, "text", str(i)) for i in insights)


def _cache_key(records: list[TrajectoryRecord]) -> str:
    """Compute a cache key from the record ids (None ids excluded)."""
    ids = ",".join(str(r.id) for r in records if r.id is not None)
    return hashlib.sha256(ids.encode()).hexdigest()


def _parse_insights(raw: str, record_ids: set[int]) -> list[Insight]:
    """Parse LLM output into Insight list.

    Returns [] on top-level parse failure (logs warning). Skips individual
    entries that fail validation (logs per-entry debug). Filters out
    evidence_refs not in record_ids (LLM hallucinated id).
    """
    raw = raw.strip()
    # Strip markdown fences if the LLM ignored the instruction
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("ReflectionEngine: top-level JSON parse failed: %s", exc)
        return []
    if not isinstance(parsed, list):
        logger.warning(
            "ReflectionEngine: expected JSON list, got %s", type(parsed).__name__
        )
        return []
    out: list[Insight] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            logger.debug("ReflectionEngine: skipping non-dict entry %r", entry)
            continue
        try:
            evidence = tuple(int(x) for x in entry.get("evidence_refs", []))
            evidence = tuple(e for e in evidence if e in record_ids)
            insight = Insight(
                observation=str(entry["observation"]),
                evidence_refs=evidence,
                action_type=entry["action_type"],
                payload=dict(entry.get("payload", {})),
                confidence=float(entry["confidence"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug(
                "ReflectionEngine: skipping malformed entry %s — %s", entry, exc
            )
            continue
        out.append(insight)
    return out


# ---------------------------------------------------------------------------
# ReflectionEngine
# ---------------------------------------------------------------------------


class ReflectionEngine:
    """GEPA-style reflection engine — analyses trajectory batches, proposes Insights.

    B2: real implementation — renders the reflect.j2 Jinja2 template, calls the
    provider, and parses Insights from the JSON response.
    """

    def __init__(
        self,
        *,
        provider: BaseProvider,
        model: str = "claude-opus-4-7",
        max_tokens: int = 4096,
        window: int = 30,
    ) -> None:
        """Initialise the engine.

        Args:
            provider: LLM provider instance (BaseProvider from plugin_sdk).
            model: Model identifier to pass to the provider.
            max_tokens: Token budget for the reflection completion.
            window: Maximum number of trajectory records to pass to a single
                ``reflect()`` call.  Must be >= 1.

        Raises:
            ValueError: if *window* < 1.
        """
        if window < 1:
            raise ValueError("window must be >= 1")
        self._provider = provider
        self._model = model
        self._max_tokens = max_tokens
        self._window = window
        self._cache: dict[str, list[Insight]] = {}

    @property
    def window(self) -> int:
        """Maximum batch size for ``reflect()`` calls."""
        return self._window

    def reflect(self, records: list[TrajectoryRecord]) -> list[Insight]:
        """Run a reflection pass on a batch of trajectories.

        Steps:
          1. Trim records to the most recent ``self._window`` (oldest dropped).
          2. Compute a cache key from the record ids (in order).
          3. Cache hit → return cached list.
          4. Render the Jinja2 reflect.j2 template with the records.
          5. Call provider.complete(...) (sync wrapper around async).
          6. Parse the response text as JSON.
          7. Validate each entry → Insight (skip + log on per-entry failures).
          8. Cache + return.

        Raises:
            RuntimeError: if called from inside a running event loop.
        """
        from plugin_sdk.core import Message  # runtime import — provider needs Message anyway

        # 1. Trim to window
        records = records[-self._window :]

        # 2. Compute cache key
        key = _cache_key(records)

        # 3. Cache hit
        if key in self._cache:
            return self._cache[key]

        # 4. Render template
        env = _build_env()
        template = env.get_template("reflect.j2")
        prompt_text = template.render(
            records=records,
            model_hint=self._model,
            now=time.time(),
        )

        # 5. Call provider (sync bridge)
        messages = [Message(role="user", content=prompt_text)]
        response = _run_async(
            self._provider.complete(
                model=self._model,
                messages=messages,
                max_tokens=self._max_tokens,
                temperature=0.3,
            )
        )
        raw_text = response.message.content

        # 6 & 7. Parse + validate
        record_ids = {r.id for r in records if r.id is not None}
        insights = _parse_insights(raw_text, record_ids)

        # 8. Cache + return
        self._cache[key] = insights
        return insights
