"""TraceCard distiller — Phase 5 stub, Phase 7 lands the real LLM flow.

Given a session that should emit (decision tree from §10 Phase 5):

* No trace was used (``trace_used is None``) AND the session ended
  cleanly — emit unconditionally.
* A trace was used AND the novelty judge returned ``is_novel=True`` —
  emit the improved/edge-case version.

…this module's :func:`distill_session` reads the session messages,
applies privacy redaction, and produces a TraceCard ready to submit.

Phase 5 contract: returns ``None`` unconditionally so the subscriber's
submission path is exercised (without actually submitting). The
subscriber treats ``None`` as "nothing worth emitting" and returns
silently — exactly the behavior we want until Phase 7 swaps the body.

Phase 7 implementation will mirror :mod:`extensions.skill_evolution.skill_extractor`
— three short Haiku calls (intent / steps / insight), each cost-guarded
and PII-redacted, then assembled into a frozen :class:`plugin_sdk.TraceCard`.
"""

from __future__ import annotations

import logging

from plugin_sdk.traces import TraceCard

_log = logging.getLogger("opencomputer.social_traces.distiller")


async def distill_session(
    *,
    session_id: str,
    profile_home,  # Path — typed loosely to avoid circular Path imports
    submitter_hash: str,
) -> TraceCard | None:
    """Distill one finished session into a TraceCard.

    Phase 5: STUB. Always returns ``None`` so the submission path is
    wired but no real submissions land in the outbox until Phase 7
    implements the LLM extractor.

    Phase 7 reads ``SessionDB.get_messages(session_id)``, redacts the
    transcript, runs the three Haiku calls, validates the resulting
    TraceCard against the schema, and returns it.
    """
    _log.debug(
        "social-traces: distiller stub called for session=%s — returning None "
        "(Phase 7 will swap the implementation)",
        session_id,
    )
    return None


__all__ = ["distill_session"]
