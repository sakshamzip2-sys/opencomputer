"""Novelty judge — Phase 6 implementation.

Decides whether a session that USED a trace did something genuinely
beyond what the trace showed. Drives the rule (d) emit/silent gate
on the trace-was-used branch.

Mirrors :mod:`extensions.skill_evolution.pattern_detector`'s judge
shape: one short Haiku call, structured JSON output, cost-guarded
pre-flight, fail-open on parse / provider / budget errors.

Three signals the prompt looks for (in priority order):

1. **Improvement** — agent solved it faster, with fewer steps, or
   with a more reliable approach than the trace prescribed.
2. **Edge case** — agent hit a failure mode the trace didn't cover
   and worked around it.
3. **Genuinely different route** — agent took a different path that
   ended in a similar outcome (lateral knowledge worth contributing).

If none of those apply, ``is_novel=False`` and the session emits
nothing. That's the conservative default — the network already has
a trace for this intent; redundant copies add noise without value.

Production wiring
-----------------
The provider + cost_guard come into the subscriber via constructor
factories. The :class:`opencomputer.gateway.server.Gateway` wires
them by resolving the user's configured provider against the live
plugin registry — same pattern as skill-evolution. The CLI single-
shot path (``opencomputer chat``) doesn't auto-start the subscriber
so the judge never fires there; that's intended (one-shot CLI runs
don't fit the long-lived "agent watches its own behaviour" model).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("opencomputer.social_traces.novelty_judge")


#: Cost we pre-flight against the cost guard. Mirrors skill-evolution's
#: Haiku-budget heuristic; tuned for the same model family.
_PROJECTED_COST_USD = 0.005

#: Hard cap on tokens the judge can emit. The structured JSON response
#: fits in well under 200; we leave headroom for verbose ``reason``
#: fields without paying for runaway prose.
_MAX_TOKENS = 256

#: Default model. Configurable per-call so tests can swap to a
#: cheaper / different model without monkey-patching.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

_JUDGE_SYSTEM_PROMPT = """\
You are a careful classifier deciding whether a coding-agent session
that USED a previously-shared workflow trace did something genuinely
beyond what the trace prescribed.

Return ONLY a single JSON object — no prose, no markdown fences.
Schema:

  {"novel": <bool>, "confidence": <int 0..100>, "reason": "<one sentence>"}

`novel` is True if the agent did ANY of:
  - Solved it meaningfully faster or with fewer steps than the trace.
  - Hit a failure mode the trace didn't cover and worked around it.
  - Found a meaningfully different route to a similar outcome.

`novel` is False if the agent followed the trace closely or did
nothing the trace didn't already cover. Default to False when
uncertain — silent emit is the safer error.

`confidence` reflects how strongly you believe the verdict (0 =
coin-flip, 100 = definitive).

Calibration examples:
- POSITIVE: trace says "use rsync --update"; agent ran it and saw
  clock skew failures, switched to "rsync --checksum", succeeded.
  → {"novel": true, "confidence": 80, "reason": "agent discovered --checksum works around clock-skew failure mode trace didn't mention"}
- NEGATIVE: trace says "use rsync --checksum"; agent ran it as
  prescribed, succeeded.
  → {"novel": false, "confidence": 90, "reason": "agent followed the trace verbatim"}
- NEGATIVE: agent re-tried the same step a few times due to a
  flaky network. Final outcome matches the trace.
  → {"novel": false, "confidence": 75, "reason": "retries don't constitute a novel pattern"}
"""


@dataclass(frozen=True, slots=True)
class NoveltyVerdict:
    """Result of the LLM judgement.

    ``is_novel``: True when the agent improved on the used trace OR
    discovered an edge case. Drives the emit-vs-silent decision.

    ``confidence``: 0-100. The subscriber currently doesn't gate on
    this (any True is treated as "emit"), but the value is logged
    for tuning the prompt downstream.

    ``reason``: short human-readable explanation. Logged for
    debuggability — never sent to the network.
    """

    is_novel: bool
    confidence: int = 0
    reason: str = ""


# ─── helpers ─────────────────────────────────────────────────────────


def _budget_allows(decision: Any) -> bool:
    """Normalise a ``cost_guard.check_budget`` return into a bool.

    The real API returns a ``BudgetDecision`` dataclass with an
    ``.allowed`` attribute. Tests mock the call as a bare bool. We
    accept either shape.
    """
    if isinstance(decision, bool):
        return decision
    return bool(getattr(decision, "allowed", False))


def _extract_response_text(response: Any) -> str:
    """Pull the assistant text out of a ProviderResponse-like object.

    Mirrors :func:`extensions.skill_evolution.pattern_detector._extract_response_text`
    — accepts either ``response.message.content`` (real ProviderResponse)
    or ``response.content`` (test mock).
    """
    raw = getattr(response, "content", None)
    if isinstance(raw, str):
        return raw
    msg = getattr(response, "message", None)
    raw = getattr(msg, "content", None) if msg is not None else None
    if isinstance(raw, str):
        return raw
    return ""


def _parse_judge_response(text: str) -> NoveltyVerdict | None:
    """Parse the judge's JSON. Returns None on any failure.

    Defensive against models that wrap output in markdown fences or
    add prose — locate the first ``{`` / last ``}`` pair and parse
    that slice.
    """
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = text[start : end + 1]
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        confidence = int(parsed.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))
    return NoveltyVerdict(
        is_novel=bool(parsed.get("novel", False)),
        confidence=confidence,
        reason=str(parsed.get("reason", "")).strip(),
    )


def _build_user_prompt(
    *,
    user_message: str,
    transcript: str,
    used_trace_intent: str,
    used_trace_insight: str,
) -> str:
    """Assemble the judge's user prompt.

    The prompt is small on purpose — Haiku gets paid by the token,
    and the system prompt already carries the rules + calibration.
    Long transcripts get truncated client-side; the subscriber
    is responsible for the truncation budget.
    """
    return (
        f"Trace that was injected at task start:\n"
        f"  Intent: {used_trace_intent or '(none)'}\n"
        f"  Insight: {used_trace_insight or '(none)'}\n\n"
        f"User's request:\n  {user_message or '(empty)'}\n\n"
        f"Agent transcript (truncated):\n{transcript or '(empty)'}\n\n"
        "Respond with the JSON object only."
    )


def _truncate_transcript(text: str, max_chars: int = 4000) -> str:
    """Cap transcript length to keep judge input bounded.

    Hard cap on input bytes so a 100K-token session can't blow up
    the Haiku context window or the cost. We truncate from the END
    (keeping the beginning) since the early-turn behaviour is the
    most diagnostic for "did the agent follow the trace?".
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…[truncated]"


# ─── public API ──────────────────────────────────────────────────────


async def judge_session_novelty(
    *,
    user_message: str,
    transcript: str,
    used_trace_intent: str,
    used_trace_insight: str,
    provider: Any | None = None,
    cost_guard: Any | None = None,
    model: str = DEFAULT_MODEL,
) -> NoveltyVerdict:
    """Decide whether the session improved on the trace it used.

    ``provider`` is a :class:`plugin_sdk.BaseProvider`-shaped object —
    any class with an ``async complete(...)`` method. ``cost_guard`` is
    duck-typed against ``check_budget`` + ``record_usage``.

    Behaviour matrix:

    * ``provider is None`` → return ``is_novel=False`` (degraded mode;
      production wiring lands when the gateway resolves a provider).
    * ``cost_guard is None`` → run without budget gating; record_usage
      no-op. Test-friendly default.
    * Budget denied → return ``is_novel=False, reason="budget-denied"``.
    * Provider raises → return ``is_novel=False, reason="provider-error"``.
    * Parse failure → return ``is_novel=False, reason="parse-failure"``.

    All failure paths fall to ``is_novel=False`` because that's the
    conservative default: when in doubt, don't add a redundant trace
    to the network.
    """
    if provider is None:
        _log.debug(
            "social-traces: novelty_judge has no provider — degrading to "
            "is_novel=False (production gateway wiring pending)"
        )
        return NoveltyVerdict(
            is_novel=False, confidence=0, reason="no-provider",
        )

    if cost_guard is not None:
        try:
            decision = cost_guard.check_budget(
                "anthropic", projected_cost_usd=_PROJECTED_COST_USD,
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "social-traces: cost_guard.check_budget raised — "
                "treating as denied",
                exc_info=True,
            )
            return NoveltyVerdict(is_novel=False, reason="cost-guard-raised")
        if not _budget_allows(decision):
            _log.info(
                "social-traces: novelty_judge skipped — cost_guard denied"
            )
            return NoveltyVerdict(is_novel=False, reason="budget-denied")

    user_prompt = _build_user_prompt(
        user_message=user_message,
        transcript=_truncate_transcript(transcript),
        used_trace_intent=used_trace_intent,
        used_trace_insight=used_trace_insight,
    )

    try:
        from plugin_sdk.core import Message  # local import — keep this
        # module importable in degraded environments

        messages = [Message(role="user", content=user_prompt)]
    except Exception:  # noqa: BLE001 — fall back to dict-shaped messages
        messages = [{"role": "user", "content": user_prompt}]

    try:
        response = await provider.complete(
            model=model,
            messages=messages,
            system=_JUDGE_SYSTEM_PROMPT,
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001 — provider failure surfaces as not-novel
        _log.warning(
            "social-traces: novelty_judge provider.complete raised — "
            "treating as not-novel",
            exc_info=True,
        )
        return NoveltyVerdict(is_novel=False, reason="provider-error")

    # Best-effort cost recording — only after a real response was
    # received. Mirrors skill-evolution's tolerant signature handling
    # (production = positional kwargs; some test mocks accept anything).
    if cost_guard is not None:
        try:
            cost_guard.record_usage(
                "anthropic",
                cost_usd=_PROJECTED_COST_USD,
                operation="social_traces_novelty_judge",
            )
        except TypeError:
            try:
                cost_guard.record_usage(
                    provider="anthropic", cost_usd=_PROJECTED_COST_USD,
                )
            except Exception:  # noqa: BLE001
                _log.debug(
                    "social-traces: cost_guard.record_usage failed",
                    exc_info=True,
                )
        except Exception:  # noqa: BLE001
            _log.debug(
                "social-traces: cost_guard.record_usage failed",
                exc_info=True,
            )

    text = _extract_response_text(response)
    parsed = _parse_judge_response(text)
    if parsed is None:
        _log.info(
            "social-traces: novelty_judge parse failed — treating as not-novel"
        )
        return NoveltyVerdict(is_novel=False, reason="parse-failure")

    _log.info(
        "social-traces: novelty_judge verdict — is_novel=%s confidence=%d "
        "reason=%r",
        parsed.is_novel, parsed.confidence, parsed.reason,
    )
    return parsed


__all__ = [
    "DEFAULT_MODEL",
    "NoveltyVerdict",
    "judge_session_novelty",
]
