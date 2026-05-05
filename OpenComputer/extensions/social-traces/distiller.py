"""TraceCard distiller — Phase 7 implementation.

Replaces the Phase 5 stub. Given a finished session, runs three
short Haiku calls (intent / steps / insight) over a redacted
transcript and assembles a :class:`plugin_sdk.TraceCard` ready to
submit to OpenHub.

Pipeline shape mirrors :mod:`extensions.skill_evolution.skill_extractor`:

* Each Haiku call cost-guarded against ``cost_guard.check_budget``;
  budget denial short-circuits the whole pipeline (don't half-build a
  trace and ship it).
* Two redaction sweeps per call: input prompt + output text.
* Schema validation at the end against the size caps from
  ``openhub-mvp.md`` §8.3 — server will reject anything malformed,
  but we'd rather fail fast on the agent side.
* Any stage returning empty / sentinel-only text → return ``None``
  (the subscriber treats ``None`` as "nothing worth submitting").

Privacy invariants enforced
---------------------------

1. Every string that enters an LLM prompt is redacted first.
2. Every LLM output is re-redacted before being placed into the
   TraceCard. We don't trust the model to preserve redaction —
   regenerating a `<redacted-path>` token in its output is fine, but
   missing one is not.
3. The submitter_hash is the only identifier on the card — no user
   identity, no profile name, no machine name.

Failure modes
-------------

* No provider (Phase 6 wiring deferred) → return ``None`` immediately.
* Cost guard denies → return ``None``.
* Provider raises → return ``None``.
* LLM emits empty / sentinel-only output → return ``None``.
* Final TraceCard fails schema validation → return ``None``.

All paths log at INFO/WARNING with the session_id; never raise into
the subscriber's bus handler.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from plugin_sdk.core import Message
from plugin_sdk.traces import TRACE_API_V1, TraceCard, TraceMeta, TraceStep

from . import redactor
from .tag_extractor import extract_tags_from_message

_log = logging.getLogger("opencomputer.social_traces.distiller")


# ─── tunables ────────────────────────────────────────────────────────


_PROJECTED_COST_USD = 0.005
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

#: Token caps per call. Intentionally tight — Haiku is cheap but
#: not free, and short outputs reduce the chance the model adds prose
#: outside the requested format.
_INTENT_MAX_TOKENS = 80
_STEPS_MAX_TOKENS = 600
_INSIGHT_MAX_TOKENS = 300

#: Field-length caps (mirror ``openhub-mvp.md`` §8.3 validation).
_INTENT_MIN_CHARS = 10
_INTENT_MAX_CHARS = 500
_INSIGHT_MIN_CHARS = 20
_INSIGHT_MAX_CHARS = 2000
_STEP_SUMMARY_MAX = 500
_TAG_MIN_CHARS = 2
_TAG_MAX_CHARS = 30
_MAX_TAGS = 10
_MIN_STEPS = 1
_MAX_STEPS = 50

#: Cap how much transcript text any single LLM call sees. Keeps
#: Haiku's context bounded and the cost predictable.
_TRANSCRIPT_CHAR_BUDGET = 4000


# ─── prompts ─────────────────────────────────────────────────────────


_INTENT_SYSTEM = (
    "You write one-sentence summaries of what a user was trying to do "
    "in a coding-agent session. Output ONLY the sentence — no preamble, "
    "no quotes, no markdown fences. Maximum 30 words. Use natural "
    "language; the audience is other AI agents who'll read this to "
    "decide if their current task is similar."
)

_STEPS_SYSTEM = (
    "You summarise a coding agent's steps as JSON: a list of "
    "objects with `tool` (string), `args_summary` (string), and "
    "`result_summary` (string). Each summary is short — under 100 "
    "words. Redact any concrete file paths, hostnames, or secrets you "
    "see — replace them with <path>, <host>, <secret>. Output ONLY "
    "the JSON list, no prose, no markdown fences. Maximum 8 steps."
)

_INSIGHT_SYSTEM = (
    "You write the `distilled_insight` field of a TraceCard — a short "
    "paragraph (1-3 sentences) capturing what another agent should "
    "remember from this session. Focus on what worked, what to avoid, "
    "and any edge cases discovered. Output ONLY the paragraph — no "
    "preamble, no quotes, no markdown. Maximum 300 characters."
)


def _build_intent_user(*, user_message: str) -> str:
    return (
        f"User's request:\n{user_message}\n\n"
        "Summarise their intent in ONE natural-language sentence."
    )


def _build_steps_user(*, user_message: str, transcript: str) -> str:
    return (
        f"User's request: {user_message}\n\n"
        f"Agent transcript:\n{transcript}\n\n"
        "Output the JSON list of steps. Maximum 8."
    )


def _build_insight_user(*, intent: str, transcript: str) -> str:
    return (
        f"Intent: {intent}\n\n"
        f"Agent transcript:\n{transcript}\n\n"
        "Write the distilled_insight paragraph."
    )


# ─── helpers ─────────────────────────────────────────────────────────


def _budget_allows(decision: Any) -> bool:
    if isinstance(decision, bool):
        return decision
    return bool(getattr(decision, "allowed", False))


def _extract_response_text(response: Any) -> str:
    raw = getattr(response, "content", None)
    if isinstance(raw, str):
        return raw
    msg = getattr(response, "message", None)
    raw = getattr(msg, "content", None) if msg is not None else None
    return raw if isinstance(raw, str) else ""


def _record_usage(cost_guard: Any, *, op: str) -> None:
    """Best-effort cost accounting that survives signature mismatches."""
    if cost_guard is None:
        return
    try:
        cost_guard.record_usage(
            "anthropic",
            cost_usd=_PROJECTED_COST_USD,
            operation=f"social_traces_{op}",
        )
    except TypeError:
        try:
            cost_guard.record_usage(
                provider="anthropic", cost_usd=_PROJECTED_COST_USD,
            )
        except Exception:  # noqa: BLE001
            _log.debug("cost_guard.record_usage failed", exc_info=True)
    except Exception:  # noqa: BLE001
        _log.debug("cost_guard.record_usage failed", exc_info=True)


async def _llm_call(
    *,
    provider: Any,
    cost_guard: Any | None,
    system: str,
    user: str,
    max_tokens: int,
    model: str,
    op: str,
) -> str | None:
    """Single Haiku call with cost-guard pre-flight + record. Returns
    None on any failure path so the caller can treat all errors the
    same way (skip distillation)."""
    if cost_guard is not None:
        try:
            decision = cost_guard.check_budget(
                "anthropic", projected_cost_usd=_PROJECTED_COST_USD,
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "social-traces: cost_guard.check_budget raised — skipping %s",
                op,
                exc_info=True,
            )
            return None
        if not _budget_allows(decision):
            _log.info("social-traces: %s skipped — cost_guard denied", op)
            return None

    try:
        response = await provider.complete(
            model=model,
            messages=[Message(role="user", content=user)],
            system=system,
            max_tokens=max_tokens,
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001
        _log.warning(
            "social-traces: %s provider.complete raised — returning None",
            op,
            exc_info=True,
        )
        return None

    _record_usage(cost_guard, op=op)
    return _extract_response_text(response)


# ─── per-call extraction ─────────────────────────────────────────────


async def _distill_intent(
    *,
    provider: Any,
    cost_guard: Any | None,
    user_message: str,
    redact_paths_layer: bool,
    redact_hostnames_layer: bool,
    sensitive_filter: Callable[[str], bool] | None,
    model: str,
) -> str | None:
    redacted_user = redactor.redact(
        user_message,
        redact_paths_layer=redact_paths_layer,
        redact_hostnames_layer=redact_hostnames_layer,
        sensitive_filter=sensitive_filter,
    )
    if not redactor.is_useful_body(redacted_user, min_chars=5):
        _log.info("social-traces: intent skipped — user message redacted out")
        return None

    raw = await _llm_call(
        provider=provider,
        cost_guard=cost_guard,
        system=_INTENT_SYSTEM,
        user=_build_intent_user(user_message=redacted_user),
        max_tokens=_INTENT_MAX_TOKENS,
        model=model,
        op="distill_intent",
    )
    if not raw:
        return None

    redacted_out = redactor.redact(
        raw,
        redact_paths_layer=redact_paths_layer,
        redact_hostnames_layer=redact_hostnames_layer,
        sensitive_filter=sensitive_filter,
    )
    flat = re.sub(r"\s+", " ", redacted_out).strip()
    if len(flat) > _INTENT_MAX_CHARS:
        flat = flat[: _INTENT_MAX_CHARS - 1].rstrip() + "…"
    if not redactor.is_useful_body(flat, min_chars=_INTENT_MIN_CHARS):
        _log.info(
            "social-traces: intent body sentinel-only / too short — skipping"
        )
        return None
    return flat


async def _distill_steps(
    *,
    provider: Any,
    cost_guard: Any | None,
    user_message: str,
    transcript: str,
    redact_paths_layer: bool,
    redact_hostnames_layer: bool,
    sensitive_filter: Callable[[str], bool] | None,
    model: str,
) -> tuple[TraceStep, ...] | None:
    """Returns ``None`` when the steps call fails or yields no usable
    structured output. An empty tuple is treated as failure (a
    TraceCard with zero steps is unhelpful to other agents)."""
    redacted_user = redactor.redact(
        user_message,
        redact_paths_layer=redact_paths_layer,
        redact_hostnames_layer=redact_hostnames_layer,
        sensitive_filter=sensitive_filter,
    )
    redacted_transcript = redactor.redact(
        transcript,
        redact_paths_layer=redact_paths_layer,
        redact_hostnames_layer=redact_hostnames_layer,
        sensitive_filter=sensitive_filter,
    )

    raw = await _llm_call(
        provider=provider,
        cost_guard=cost_guard,
        system=_STEPS_SYSTEM,
        user=_build_steps_user(
            user_message=redacted_user, transcript=redacted_transcript,
        ),
        max_tokens=_STEPS_MAX_TOKENS,
        model=model,
        op="distill_steps",
    )
    if not raw:
        return None

    parsed = _parse_steps_json(raw)
    if not parsed:
        return None

    # Re-redact each summary defensively before sealing into TraceStep.
    out: list[TraceStep] = []
    for step in parsed[:_MAX_STEPS]:
        tool_name = (step.get("tool") or "").strip()[:50]
        args = redactor.redact(
            step.get("args_summary") or "",
            redact_paths_layer=redact_paths_layer,
            redact_hostnames_layer=redact_hostnames_layer,
            sensitive_filter=sensitive_filter,
        )[:_STEP_SUMMARY_MAX]
        result = redactor.redact(
            step.get("result_summary") or "",
            redact_paths_layer=redact_paths_layer,
            redact_hostnames_layer=redact_hostnames_layer,
            sensitive_filter=sensitive_filter,
        )[:_STEP_SUMMARY_MAX]
        if not tool_name or not args:
            continue  # malformed step from the model; skip
        out.append(
            TraceStep(
                tool_name=tool_name,
                arguments_summary=args,
                result_summary=result,
                duration_ms=int(step.get("duration_ms", 0) or 0),
            )
        )
    if not out:
        return None
    return tuple(out)


def _parse_steps_json(text: str) -> list[dict] | None:
    """Tolerant JSON-list parser — skips markdown fences + prose."""
    import json

    # Try whole-text parse first.
    try:
        v = json.loads(text)
        if isinstance(v, list):
            return [s for s in v if isinstance(s, dict)]
    except (json.JSONDecodeError, ValueError):
        pass

    # Extract first ``[...]`` block.
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        v = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(v, list):
        return None
    return [s for s in v if isinstance(s, dict)]


async def _distill_insight(
    *,
    provider: Any,
    cost_guard: Any | None,
    intent: str,
    transcript: str,
    redact_paths_layer: bool,
    redact_hostnames_layer: bool,
    sensitive_filter: Callable[[str], bool] | None,
    model: str,
) -> str | None:
    redacted_transcript = redactor.redact(
        transcript,
        redact_paths_layer=redact_paths_layer,
        redact_hostnames_layer=redact_hostnames_layer,
        sensitive_filter=sensitive_filter,
    )
    raw = await _llm_call(
        provider=provider,
        cost_guard=cost_guard,
        system=_INSIGHT_SYSTEM,
        user=_build_insight_user(intent=intent, transcript=redacted_transcript),
        max_tokens=_INSIGHT_MAX_TOKENS,
        model=model,
        op="distill_insight",
    )
    if not raw:
        return None
    redacted_out = redactor.redact(
        raw,
        redact_paths_layer=redact_paths_layer,
        redact_hostnames_layer=redact_hostnames_layer,
        sensitive_filter=sensitive_filter,
    )
    flat = re.sub(r"\s+", " ", redacted_out).strip()
    if len(flat) > _INSIGHT_MAX_CHARS:
        flat = flat[: _INSIGHT_MAX_CHARS - 1].rstrip() + "…"
    if not redactor.is_useful_body(flat, min_chars=_INSIGHT_MIN_CHARS):
        _log.info(
            "social-traces: insight sentinel-only / too short — skipping"
        )
        return None
    return flat


# ─── tag derivation ──────────────────────────────────────────────────


_TAG_NORMALIZE_RE = re.compile(r"[^a-z0-9-]")


def _normalize_tags(raw: tuple[str, ...]) -> tuple[str, ...]:
    """Map raw extractor output to TraceCard-shaped tag strings.

    Server-side validation (see ``openhub-mvp.md`` §8.3) requires
    lowercase alphanumeric+hyphen, length 2-30, max 10 tags. We
    enforce that here so a borderline tag never escapes to the
    network. Order preserved for determinism.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw_tag in raw:
        norm = _TAG_NORMALIZE_RE.sub("-", raw_tag.lower()).strip("-")
        if len(norm) < _TAG_MIN_CHARS or len(norm) > _TAG_MAX_CHARS:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
        if len(out) >= _MAX_TAGS:
            break
    return tuple(out)


# ─── orchestrator + assembly ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _SessionInputs:
    """Inputs the orchestrator collects before kicking off Haiku calls.

    ``outcome`` is supplied by the caller (subscriber sources it from
    ``SessionEndEvent.had_errors`` / ``end_reason``) — the
    ``Message`` objects persisted in SessionDB don't carry the
    per-tool ``is_error`` flag, so error detection has to come from
    the event itself, not the transcript.
    """

    user_message: str
    transcript: str
    token_cost: int
    loop_count: int


def _read_session_inputs(
    session_id: str, profile_home: Path
) -> _SessionInputs | None:
    """Pull the user message + transcript + session metadata from
    SessionDB. Returns ``None`` if the session can't be read or has
    no user message.

    Same lazy SessionDB import pattern subscriber._read_session_for_judge
    uses — keeps the boundary surface predictable.
    """
    try:
        from opencomputer.agent.state import SessionDB

        db = SessionDB(profile_home / "sessions.db")
        messages = db.get_messages(session_id)
        meta = db.get_session(session_id) or {}
    except Exception:  # noqa: BLE001
        _log.debug(
            "social-traces: session %s — couldn't read SessionDB",
            session_id,
            exc_info=True,
        )
        return None

    user_message = ""
    transcript_lines: list[str] = []
    for m in messages:
        role = getattr(m, "role", "") or ""
        content = getattr(m, "content", "") or ""
        if role == "user" and not user_message:
            user_message = content
            continue
        if role == "user" and "<system-reminder>" in content:
            continue
        transcript_lines.append(f"[{role}] {content}")

    if not user_message.strip():
        return None

    transcript = "\n".join(transcript_lines)
    if len(transcript) > _TRANSCRIPT_CHAR_BUDGET:
        transcript = transcript[:_TRANSCRIPT_CHAR_BUDGET] + "\n…[truncated]"

    token_cost = int(meta.get("input_tokens", 0) or 0) + int(
        meta.get("output_tokens", 0) or 0
    )

    return _SessionInputs(
        user_message=user_message,
        transcript=transcript,
        token_cost=token_cost,
        loop_count=len(transcript_lines),
    )


def _validate(card: TraceCard) -> bool:
    """Final schema check before submission. Mirrors ``openhub-mvp.md`` §8.3."""
    if card.schema_version != TRACE_API_V1:
        return False
    if not (_INTENT_MIN_CHARS <= len(card.intent) <= _INTENT_MAX_CHARS):
        return False
    if not (
        _INSIGHT_MIN_CHARS <= len(card.distilled_insight) <= _INSIGHT_MAX_CHARS
    ):
        return False
    if not card.meta.tags or len(card.meta.tags) > _MAX_TAGS:
        return False
    for tag in card.meta.tags:
        if not (_TAG_MIN_CHARS <= len(tag) <= _TAG_MAX_CHARS):
            return False
    if not card.steps or len(card.steps) > _MAX_STEPS:
        return False
    if len(card.meta.submitter_hash) < 32 or len(card.meta.submitter_hash) > 64:
        return False
    return True


async def distill_session(
    *,
    session_id: str,
    profile_home: Path,
    submitter_hash: str,
    provider: Any | None = None,
    cost_guard: Any | None = None,
    redact_paths_layer: bool = True,
    redact_hostnames_layer: bool = True,
    sensitive_filter: Callable[[str], bool] | None = None,
    harness_version: str = "",
    outcome: str = "success",
    model: str = _DEFAULT_MODEL,
) -> TraceCard | None:
    """Phase 7: distill a finished session into a TraceCard.

    Returns ``None`` whenever the session can't be distilled cleanly —
    SessionDB unreadable, no user message, no provider, cost guard
    denied, LLM emits sentinel-only output, schema validation fails.
    Every failure logs at INFO/WARNING with the session_id; never
    raises into the subscriber.

    The subscriber (Phase 5) calls this with the constructor-injected
    provider + cost_guard + sensitive_filter. CLI single-shot path
    passes ``provider=None`` and gets ``None`` back without paying
    any LLM cost.
    """
    if provider is None:
        _log.debug(
            "social-traces: distiller %s skipped — no provider (production "
            "wiring deferred to Phase 9)",
            session_id,
        )
        return None

    inputs = _read_session_inputs(session_id, profile_home)
    if inputs is None:
        return None

    started = time.monotonic()

    # Stage 1 — intent
    intent = await _distill_intent(
        provider=provider,
        cost_guard=cost_guard,
        user_message=inputs.user_message,
        redact_paths_layer=redact_paths_layer,
        redact_hostnames_layer=redact_hostnames_layer,
        sensitive_filter=sensitive_filter,
        model=model,
    )
    if intent is None:
        return None

    # Stage 2 — steps
    steps = await _distill_steps(
        provider=provider,
        cost_guard=cost_guard,
        user_message=inputs.user_message,
        transcript=inputs.transcript,
        redact_paths_layer=redact_paths_layer,
        redact_hostnames_layer=redact_hostnames_layer,
        sensitive_filter=sensitive_filter,
        model=model,
    )
    if steps is None:
        return None

    # Stage 3 — insight
    insight = await _distill_insight(
        provider=provider,
        cost_guard=cost_guard,
        intent=intent,
        transcript=inputs.transcript,
        redact_paths_layer=redact_paths_layer,
        redact_hostnames_layer=redact_hostnames_layer,
        sensitive_filter=sensitive_filter,
        model=model,
    )
    if insight is None:
        return None

    # Tags — derive from the user message (free-form for v1; LLM
    # tag-extractor upgrade lands in Phase 8). Normalize to the
    # network's lowercase alphanumeric+hyphen shape.
    raw_tags = extract_tags_from_message(inputs.user_message, max_tags=_MAX_TAGS)
    tags = _normalize_tags(raw_tags)
    if not tags:
        # No tags is a hard fail on the server side; rather than
        # ship a card the network will reject, drop it here. Phase 8
        # LLM tag extractor mostly avoids this case.
        _log.info(
            "social-traces: %s — no tags extracted, dropping submission",
            session_id,
        )
        return None

    duration_ms = int((time.monotonic() - started) * 1000)

    # ``outcome`` defaults to "success" but the subscriber should pass
    # through ``SessionEndEvent.had_errors`` / ``end_reason`` so the
    # final card reflects what actually happened.
    if outcome not in {"success", "partial", "failed"}:
        outcome = "success"

    card = TraceCard(
        schema_version=TRACE_API_V1,
        intent=intent,
        meta=TraceMeta(
            tags=tags,
            outcome=outcome,  # type: ignore[arg-type]
            token_cost=inputs.token_cost,
            loop_count=inputs.loop_count,
            harness_version=harness_version,
            submitter_hash=submitter_hash,
        ),
        steps=steps,
        distilled_insight=insight,
        created_at=datetime.now(UTC).isoformat(),
    )

    if not _validate(card):
        _log.warning(
            "social-traces: %s — distilled card failed schema validation, "
            "dropping",
            session_id,
        )
        return None

    _log.info(
        "social-traces: %s distilled in %dms — tags=%s steps=%d",
        session_id, duration_ms, ",".join(tags), len(steps),
    )
    return card


__all__ = ["distill_session"]
