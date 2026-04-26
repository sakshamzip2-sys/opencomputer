"""End-to-end orchestrator for the pattern-detection procedural-memory loop.

Phase 5.B-3 of catch-up plan. Composes the per-piece work that landed
in earlier phases:

- :class:`PatternDetector` (5.1) observes tool calls
- :class:`DraftRateLimiter` (5.3) enforces 1/day + 10/lifetime caps
- :class:`PatternSynthesizer` (5.2 + 5.B-1) drafts SKILL.md
- F1 :class:`ConsentGate` enforces ``procedural_memory.write_skill``

Caller wiring (a hook subscriber):

    loop = ProceduralMemoryLoop(home=home, provider=cheap_provider,
                                consent_gate=gate)
    api.register_hook("PostToolUse", lambda ctx: loop.observe(
        ctx.tool_name, ctx.tool_arguments, error=ctx.tool_error,
    ))
    api.register_hook("Stop", lambda ctx: loop.maybe_propose_drafts())

The loop is *defensive*: synthesis or rate-limit failures are caught
inside, never propagate to the agent loop. The cost is silent skips —
worth it because the alternative (crashing the model's turn for a
self-improvement glitch) is much worse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencomputer.evolution.pattern_detector import PatternDetector
from opencomputer.evolution.pattern_synthesizer import (
    PatternSynthesizer,
    SynthesisError,
)
from opencomputer.evolution.rate_limit import DraftRateLimiter, RateLimitExceeded
from opencomputer.evolution.store import (
    archive_dir,
    ensure_dirs,
    evolution_root,
    is_archived,
)
from plugin_sdk.consent import CapabilityClaim, ConsentTier

if TYPE_CHECKING:
    from opencomputer.agent.consent.gate import ConsentGate

_log = logging.getLogger("opencomputer.evolution.loop")

_CAPABILITY_ID = "procedural_memory.write_skill"


@dataclass
class ProceduralMemoryLoop:
    """Wires pattern detection → consent → rate-limit → synthesis → write.

    Parameters
    ----------
    home:
        Active profile home (used for store paths + rate limiter DB).
    provider:
        Anything with awaitable ``complete(prompt) -> str``. The
        cheap-route provider is the right choice — drafts don't need
        the smartest model.
    consent_gate:
        F1 ``ConsentGate`` instance. ``None`` disables the gate (test
        usage only — production must always pass one).
    threshold:
        Pattern-occurrence threshold before a proposal fires (default 3).
    """

    home: Path
    provider: Any
    consent_gate: ConsentGate | None = None
    threshold: int = 3
    detector: PatternDetector = field(init=False)
    synthesizer: PatternSynthesizer = field(init=False)
    rate_limiter: DraftRateLimiter = field(init=False)

    def __post_init__(self) -> None:
        ensure_dirs(self.home)
        self.detector = PatternDetector(threshold=self.threshold)
        self.synthesizer = PatternSynthesizer(home=self.home, provider=self.provider)
        rate_db = evolution_root(self.home) / "rate.db"
        self.rate_limiter = DraftRateLimiter(db_path=rate_db)

    # ─── PostToolUse hook entry ─────────────────────────────────────────

    def observe(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        error: bool = False,
    ) -> None:
        """Forward an observation to the detector. Cheap, no IO."""
        self.detector.observe(tool_name, arguments or {}, error=error)

    # ─── Stop / end-of-turn hook entry ──────────────────────────────────

    async def maybe_propose_drafts(self) -> list[Path]:
        """Drain pending proposals; synthesize approved ones.

        Returns the list of new SKILL.md paths written to quarantine.
        Any single proposal can fail (consent denied, rate-limited,
        bad LLM output, slug collision); failures are logged and
        skipped, never propagated.
        """
        proposals = self.detector.drain_proposals()
        if not proposals:
            return []

        written: list[Path] = []
        for proposal in proposals:
            # 1. Was this pattern previously discarded by the user?
            #    If so, don't re-propose. (Prevents nagging.)
            if self._archived_for_pattern(proposal.pattern_key):
                _log.debug("loop: pattern %s previously archived, skipping",
                           proposal.pattern_key)
                continue

            # 2. Consent gate. Default-deny if no grant.
            if self.consent_gate is not None and not self._consent_ok():
                _log.debug("loop: consent not granted for %s, skipping",
                           _CAPABILITY_ID)
                # Drain everything but write nothing — user can grant later
                # and the next round will fire again. We intentionally
                # consume proposals here so we don't pile up on every turn.
                continue

            # 3. Rate limit. Per-day + lifetime checks.
            try:
                self.rate_limiter.allow()
            except RateLimitExceeded as e:
                _log.info("loop: rate-limited (%s); deferring", e)
                # Re-mark as not-proposed so the next turn after the
                # window rolls off can fire again. Otherwise we'd lose
                # the proposal forever after a single rate-limit hit.
                self.detector.reset_proposed()
                break

            # 4. Synthesize via LLM
            try:
                path = await self.synthesizer.synthesize(proposal)
            except SynthesisError as e:
                _log.warning("loop: synthesis failed for %s: %s",
                             proposal.pattern_key, e)
                continue
            except Exception as e:  # noqa: BLE001 — never crash the agent loop
                _log.exception("loop: unexpected synthesis failure for %s: %s",
                               proposal.pattern_key, e)
                continue

            self.rate_limiter.record_draft()
            written.append(path)

        return written

    # ─── Helpers ────────────────────────────────────────────────────────

    def _consent_ok(self) -> bool:
        """Run the consent gate check. Returns ``True`` if allowed."""
        if self.consent_gate is None:
            return True
        claim = CapabilityClaim(
            capability_id=_CAPABILITY_ID,
            tier_required=ConsentTier.EXPLICIT,
            human_description=(
                "Draft a new skill based on a repeated pattern observed in "
                "this session. Drafts go to quarantine; require explicit "
                "user approval before they activate."
            ),
        )
        try:
            decision = self.consent_gate.check(claim, scope=None, session_id=None)
        except Exception as e:  # noqa: BLE001
            _log.exception("loop: consent gate raised: %s", e)
            return False
        return bool(getattr(decision, "allow", False))

    def _archived_for_pattern(self, pattern_key: str) -> bool:
        """Did the user previously discard a draft for this pattern?

        We keep a soft index: every archived dir has a marker file
        containing the pattern key. If found, this pattern is
        suppressed.
        """
        # Cheap check — list archive entries' marker files. Archive
        # is bounded (drafts are throttled by rate limiter), so the
        # full scan is fine.
        arch = archive_dir(self.home)
        if not arch.exists():
            return False
        for entry in arch.iterdir():
            marker = entry / ".pattern_key"
            if marker.exists() and marker.read_text().strip() == pattern_key:
                return True
        return False

    def mark_archived_pattern(self, slug: str, pattern_key: str) -> None:
        """Record the pattern key on a discarded draft so we don't re-propose.

        Called by the discard CLI after :func:`store.discard_draft`.
        """
        arch = archive_dir(self.home) / slug
        if not arch.exists():
            return
        (arch / ".pattern_key").write_text(pattern_key + "\n")

    @staticmethod
    def is_archived(home: Path, slug: str) -> bool:
        return is_archived(home, slug)
