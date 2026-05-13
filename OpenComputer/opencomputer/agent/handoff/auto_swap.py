"""AutoSwapTrigger — classifier-driven profile-swap state machine.

Algorithm (chosen on merit after Phase 1 brainstorm + Phase 2 audit):

  * On every user turn, the agent loop runs the existing
    :func:`opencomputer.awareness.personas.classifier.classify` over the
    last 3 user messages.
  * The trigger maintains a per-session **rolling window** of the last
    3 classifications.
  * When ALL THREE window entries point to the SAME non-current persona
    at confidence >= 0.8, AND that persona resolves to an available
    profile via :func:`profile_analysis._persona_matches_profile`, the
    trigger returns a :class:`SwapDecision(target=<profile>)`.
  * A 5-turn **cooldown** after any swap (manual or auto) suppresses new
    auto-swaps. Cooldown is per-session.

Why this shape:
  - 3-of-3 sustained avoids single-question yo-yo
  - 0.8 confidence avoids low-signal swaps (classifier's literal
    fallback persona never has confidence >= 0.8 in practice)
  - persona→profile resolution uses the existing fuzzy heuristic in
    profile_analysis, so manually-named profiles like "coder" still work
  - cooldown bounds worst-case classifier error to 1 bad swap

State is held in the runtime context's ``custom`` dict (the same
dictionary used by every other turn-scoped pending-state in OC).
Multi-session isolation: keys are scoped under
``custom["_handoff_auto_swap"]`` and indexed by session_id so concurrent
sessions in the same runtime never share state.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from opencomputer.awareness.personas.classifier import ClassificationResult

_log = logging.getLogger("opencomputer.agent.handoff.auto_swap")

# Tunables — single source of truth. Override via DI in tests, not by
# patching module globals at runtime.
DEFAULT_STREAK_LENGTH: int = 3
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.8
DEFAULT_COOLDOWN_TURNS: int = 5
DEFAULT_WINDOW_SIZE: int = 3

_STATE_KEY = "_handoff_auto_swap"


class SwapDecisionReason(Enum):
    """Why a SwapDecision turned out the way it did. Surfaced in logs +
    audit; never user-facing."""
    FIRED = "fired"
    BELOW_THRESHOLD = "below_threshold"
    STREAK_INCOMPLETE = "streak_incomplete"
    MIXED_PERSONAS = "mixed_personas"
    PERSONA_IS_CURRENT = "persona_is_current"
    PERSONA_UNMAPPED = "persona_unmapped"
    NO_AVAILABLE_TARGET = "no_available_target"
    COOLDOWN_ACTIVE = "cooldown_active"
    PLAN_MODE = "plan_mode"
    GATEWAY_DISABLED = "gateway_disabled"
    AUTO_OFF = "auto_off"


@dataclass(frozen=True, slots=True)
class SwapDecision:
    """The trigger's verdict for one turn."""
    target_profile: str | None
    reason: SwapDecisionReason
    confidence: float = 0.0
    classifier_reason: str = ""
    persona: str = ""

    @property
    def should_swap(self) -> bool:
        return (
            self.target_profile is not None
            and self.reason == SwapDecisionReason.FIRED
        )


@dataclass(slots=True)
class _SessionState:
    """Mutable per-session state. Stored in runtime.custom — survives
    across turns within one session, garbage-collected on session close
    by the runtime's normal lifecycle."""
    window: list[tuple[str, float]] = field(default_factory=list)
    cooldown_remaining: int = 0


class AutoSwapTrigger:
    """Stateless evaluator over per-session state held in runtime.custom.

    The class itself holds NO mutable state — every method accepts a
    runtime context and reads/writes the session-scoped slot. This means
    a single instance can serve every session in the process and tests
    can construct it once.
    """

    def __init__(
        self,
        *,
        persona_to_profile: callable[[str, tuple[str, ...]], str | None] = None,  # noqa: UP007
        streak_length: int = DEFAULT_STREAK_LENGTH,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        cooldown_turns: int = DEFAULT_COOLDOWN_TURNS,
        window_size: int = DEFAULT_WINDOW_SIZE,
    ) -> None:
        if streak_length < 1:
            raise ValueError(f"streak_length must be >= 1 (got {streak_length})")
        if not 0 < confidence_threshold <= 1.0:
            raise ValueError(
                f"confidence_threshold must be in (0, 1] "
                f"(got {confidence_threshold})"
            )
        if cooldown_turns < 0:
            raise ValueError(f"cooldown_turns must be >= 0 (got {cooldown_turns})")
        if window_size < streak_length:
            raise ValueError(
                f"window_size ({window_size}) must be >= "
                f"streak_length ({streak_length})"
            )
        self._streak_length = streak_length
        self._confidence_threshold = confidence_threshold
        self._cooldown_turns = cooldown_turns
        self._window_size = window_size
        self._persona_to_profile = persona_to_profile or _default_persona_to_profile

    # ─── public API ──────────────────────────────────────────────────

    def evaluate(
        self,
        *,
        runtime: Any,
        session_id: str,
        classification: ClassificationResult,
        current_profile: str,
        available_profiles: tuple[str, ...],
        plan_mode: bool,
        auto_off: bool,
        is_gateway_session: bool,
        gateway_optin: bool,
    ) -> SwapDecision:
        """Compute the swap decision for THIS turn.

        Side-effects: appends to the rolling window, decrements the
        cooldown counter. The caller is responsible for calling
        :meth:`mark_swapped` after a successful swap (manual or auto) so
        the cooldown resets.
        """
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(current_profile, str) or not current_profile:
            raise ValueError("current_profile must be a non-empty string")
        if not isinstance(classification, ClassificationResult):
            raise TypeError(
                f"classification must be ClassificationResult, "
                f"got {type(classification).__name__}"
            )

        state = _get_or_create_state(runtime, session_id)

        # Always advance the rolling window before any gate check so we
        # don't lose signal during cooldown / plan-mode. This makes
        # cooldown a "fire suppressor" rather than a "memory wipe".
        self._advance_window(state, classification)

        if auto_off:
            return SwapDecision(
                None, SwapDecisionReason.AUTO_OFF,
                confidence=classification.confidence,
                persona=classification.persona_id,
            )
        if plan_mode:
            return SwapDecision(
                None, SwapDecisionReason.PLAN_MODE,
                confidence=classification.confidence,
                persona=classification.persona_id,
            )
        if is_gateway_session and not gateway_optin:
            return SwapDecision(
                None, SwapDecisionReason.GATEWAY_DISABLED,
                confidence=classification.confidence,
                persona=classification.persona_id,
            )
        if state.cooldown_remaining > 0:
            state.cooldown_remaining -= 1
            return SwapDecision(
                None, SwapDecisionReason.COOLDOWN_ACTIVE,
                confidence=classification.confidence,
                persona=classification.persona_id,
            )
        if classification.confidence < self._confidence_threshold:
            return SwapDecision(
                None, SwapDecisionReason.BELOW_THRESHOLD,
                confidence=classification.confidence,
                persona=classification.persona_id,
            )

        # Streak check — look at the tail of the rolling window for N
        # entries all >= threshold and pointing to the same persona.
        tail = state.window[-self._streak_length:]
        if len(tail) < self._streak_length:
            return SwapDecision(
                None, SwapDecisionReason.STREAK_INCOMPLETE,
                confidence=classification.confidence,
                persona=classification.persona_id,
            )

        personas = {p for p, _ in tail}
        confs = [c for _, c in tail]
        if len(personas) > 1:
            return SwapDecision(
                None, SwapDecisionReason.MIXED_PERSONAS,
                confidence=min(confs),
                persona=classification.persona_id,
            )
        if min(confs) < self._confidence_threshold:
            return SwapDecision(
                None, SwapDecisionReason.BELOW_THRESHOLD,
                confidence=min(confs),
                persona=classification.persona_id,
            )

        target_persona = next(iter(personas))
        if target_persona == "default":
            # The classifier-fallback bucket. Never a swap target —
            # "default" is the unspecified profile every user starts in.
            return SwapDecision(
                None, SwapDecisionReason.PERSONA_UNMAPPED,
                confidence=min(confs),
                persona=target_persona,
                classifier_reason=classification.reason,
            )

        target_profile = self._persona_to_profile(target_persona, available_profiles)
        if target_profile is None:
            return SwapDecision(
                None, SwapDecisionReason.NO_AVAILABLE_TARGET,
                confidence=min(confs),
                persona=target_persona,
                classifier_reason=classification.reason,
            )
        if target_profile == current_profile:
            return SwapDecision(
                None, SwapDecisionReason.PERSONA_IS_CURRENT,
                confidence=min(confs),
                persona=target_persona,
                classifier_reason=classification.reason,
            )

        return SwapDecision(
            target_profile=target_profile,
            reason=SwapDecisionReason.FIRED,
            confidence=min(confs),
            classifier_reason=classification.reason,
            persona=target_persona,
        )

    def mark_swapped(self, *, runtime: Any, session_id: str) -> None:
        """Reset cooldown after a successful swap (manual or auto).

        The window is intentionally NOT cleared — we WANT future eval to
        know the conversation has been about persona X, so swapping back
        requires the same 3-of-3 sustained signal.
        """
        state = _get_or_create_state(runtime, session_id)
        state.cooldown_remaining = self._cooldown_turns

    def cooldown_remaining(
        self, *, runtime: Any, session_id: str,
    ) -> int:
        """Observability helper — used by status-bar / audit log."""
        state = _peek_state(runtime, session_id)
        return 0 if state is None else state.cooldown_remaining

    # ─── internal ────────────────────────────────────────────────────

    def _advance_window(
        self, state: _SessionState, classification: ClassificationResult,
    ) -> None:
        state.window.append(
            (classification.persona_id, float(classification.confidence))
        )
        if len(state.window) > self._window_size:
            state.window = state.window[-self._window_size:]


# ─── helpers ──────────────────────────────────────────────────────────


def _default_persona_to_profile(
    persona: str, available_profiles: tuple[str, ...],
) -> str | None:
    """Use the existing profile_analysis fuzzy-match.

    Imported lazily to keep auto_swap importable without pulling
    SessionDB. Returns the first matching profile name or None.
    """
    from opencomputer.profile_analysis import _persona_matches_profile

    for p in available_profiles:
        if _persona_matches_profile(persona, p):
            return p
    return None


def _get_or_create_state(runtime: Any, session_id: str) -> _SessionState:
    """Fetch (or create) the session-scoped state slot in runtime.custom."""
    container = runtime.custom.setdefault(_STATE_KEY, {})
    state = container.get(session_id)
    if state is None:
        state = _SessionState()
        container[session_id] = state
    return state


def _peek_state(runtime: Any, session_id: str) -> _SessionState | None:
    container = runtime.custom.get(_STATE_KEY)
    if container is None:
        return None
    return container.get(session_id)


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_COOLDOWN_TURNS",
    "DEFAULT_STREAK_LENGTH",
    "DEFAULT_WINDOW_SIZE",
    "AutoSwapTrigger",
    "SwapDecision",
    "SwapDecisionReason",
]
