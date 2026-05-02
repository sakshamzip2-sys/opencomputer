"""Skill extractor — LLM-driven SKILL.md generator for auto-skill-evolution (T3).

Given a session that has cleared Stage 1 (heuristic) and Stage 2 (LLM judge)
of :mod:`extensions.skill_evolution.pattern_detector`, this module produces a
:class:`ProposedSkill` — a SKILL.md draft plus provenance metadata — which T4
will stage under ``~/.claude/skills/proposed/`` for human review.

Three independent LLM calls
---------------------------
The extractor runs three short Haiku calls in series, each separately
cost-guarded:

1. **Intent**       — one-sentence summary of what the user was trying to do.
2. **Procedure**    — numbered steps describing how the agent succeeded.
                       Path-like strings and obvious secrets are scrubbed in
                       the prompt itself, then re-scrubbed in the response.
3. **Trigger**      — the ``Use when ...`` phrasing that goes into SKILL.md
                       frontmatter ``description``.

Any of these failing — budget exhaustion, provider exception, empty content,
or post-redaction sentinel-only output — short-circuits to ``None`` so the
caller can quietly drop the candidate and move on.

Privacy layers
--------------
Two redaction sweeps run on every LLM response:

* **Caller filter.**  An optional ``sensitive_filter`` callable can mark
  text as too sensitive to keep. Matches are replaced with ``<redacted>``.
* **Built-in PII regex.** Credit-card-shaped digit groups and SSN-shaped
  ``XXX-XX-XXXX`` strings are replaced with ``<redacted-pii>`` regardless
  of the caller filter.

If after redaction the body is empty / sentinel-only / shorter than a
useful threshold, we return ``None`` rather than ship a degenerate skill.

Naming
------
``auto-{session_id_prefix(8)}-{slug(intent, 30)}`` — collision-resistant
within a session DB and human-scannable in the staging UI.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

_log = logging.getLogger("opencomputer.skill_evolution.skill_extractor")

# ─── tunables ─────────────────────────────────────────────────────────

#: Heuristic per-call cost we pre-flight against ``cost_guard``. Three of
#: these run per extraction (one per Haiku call). Tuned to leave headroom
#: under typical $0.01-per-call Haiku 4.5 spend.
_PROJECTED_COST_USD = 0.01

#: Frontmatter description hard-cap (SKILL.md convention — keeps the
#: skill picker readable when many auto-skills are loaded).
_DESCRIPTION_MAX_LEN = 200

#: Minimum length of the redacted body for the proposal to be useful.
#: Below this we return ``None`` rather than ship a sentinel-only skill.
_MIN_BODY_LEN = 20

_REDACTED = "<redacted>"
_REDACTED_PII = "<redacted-pii>"

# Credit-card-shaped: 16 digits in 4-4-4-4 with optional space/hyphen
# separators. We don't Luhn-validate — false positives at this stage are
# acceptable, false negatives would leak.
_CREDIT_CARD_RE = re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# Path-ish strings — abs paths, tilde paths, env-var-looking tokens. Used
# only when sensitive_filter is provided, to give the filter a reasonable
# net for /Users/saksham/... and similar leaks.
_PATH_LIKE_RE = re.compile(
    r"(?:/[A-Za-z0-9._-]+)+|~/[A-Za-z0-9._/-]+|\$[A-Z_]+"
)


# ─── public dataclasses ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProposedSkill:
    """A fully-rendered SKILL.md draft plus provenance.

    ``body`` is the entire file content (frontmatter + source comment +
    body). ``provenance`` carries the auditing metadata T4 will dump into
    a sibling ``provenance.json`` next to the staged SKILL.md.
    """

    name: str
    description: str
    body: str
    provenance: dict


# ─── helpers ──────────────────────────────────────────────────────────


def _slugify(text: str, max_len: int = 30) -> str:
    """Lowercase, alphanumeric+hyphen only, truncated.

    Returns ``"untitled"`` for empty / whitespace / all-special input so
    skill names always stay human-typable.
    """
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    s = s[:max_len].rstrip("-")
    return s or "untitled"


def _extract_response_text(response: Any) -> str:
    """Pull text out of a ProviderResponse-like object.

    Mirrors :func:`pattern_detector._extract_response_text`. Tests mock
    a bare ``MagicMock(content="...")``; production passes a real
    ``ProviderResponse`` with ``.message.content``.
    """
    raw = getattr(response, "content", None)
    if isinstance(raw, str):
        return raw
    msg = getattr(response, "message", None)
    raw = getattr(msg, "content", None) if msg is not None else None
    if isinstance(raw, str):
        return raw
    return ""


def _budget_allows(decision: Any) -> bool:
    """Normalise cost_guard.check_budget result into a bool.

    Real API returns ``BudgetDecision`` with ``.allowed``; tests mock
    bare ``True``/``False``. Accept either.
    """
    if isinstance(decision, bool):
        return decision
    return bool(getattr(decision, "allowed", False))


def _record_usage(cost_guard: Any) -> None:
    """Best-effort cost accounting that survives signature mismatches."""
    try:
        cost_guard.record_usage(
            "anthropic",
            cost_usd=_PROJECTED_COST_USD,
            operation="skill_evolution_extract",
        )
    except TypeError:
        cost_guard.record_usage(
            provider="anthropic", cost_usd=_PROJECTED_COST_USD
        )
    except Exception:  # noqa: BLE001 — never let recording crash extraction
        _log.warning(
            "skill-evolution: cost_guard.record_usage failed", exc_info=True
        )


def _redact_pii(text: str) -> str:
    """Replace credit-card-shaped and SSN-shaped substrings with sentinel."""
    if not text:
        return text
    text = _CREDIT_CARD_RE.sub(_REDACTED_PII, text)
    text = _SSN_RE.sub(_REDACTED_PII, text)
    return text


def _apply_sensitive_filter(
    text: str, sensitive_filter: Callable[[str], bool] | None
) -> str:
    """Run the caller's filter against full text and path-like substrings.

    The filter is asked once with the whole text — if it says "yes,
    sensitive", we replace the entire body with a sentinel. We also
    sweep path-like substrings independently so a per-line filter can
    catch leaks the whole-body check missed.
    """
    if sensitive_filter is None or not text:
        return text

    try:
        if sensitive_filter(text):
            # Whole-text sensitive: collapse to sentinel.
            return _REDACTED
    except Exception:  # noqa: BLE001 — filter bugs shouldn't crash extractor
        _log.warning(
            "skill-evolution: sensitive_filter raised — redacting body",
            exc_info=True,
        )
        return _REDACTED

    def _replace_path(match: re.Match[str]) -> str:
        token = match.group(0)
        try:
            return _REDACTED if sensitive_filter(token) else token
        except Exception:  # noqa: BLE001
            return _REDACTED

    return _PATH_LIKE_RE.sub(_replace_path, text)


def _is_useful_body(text: str) -> bool:
    """Reject bodies that are empty, sentinel-only, or too short."""
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    # Strip every redaction sentinel and whitespace; if nothing's left,
    # the body was sensitive-only.
    no_sentinels = stripped
    for sentinel in (_REDACTED, _REDACTED_PII):
        no_sentinels = no_sentinels.replace(sentinel, "")
    no_sentinels = re.sub(r"\s+", " ", no_sentinels).strip()
    return len(no_sentinels) >= _MIN_BODY_LEN


def _truncate_one_line(text: str, max_len: int) -> str:
    """Collapse to one line and clip to ``max_len`` chars."""
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= max_len:
        return flat
    return flat[: max_len - 1].rstrip() + "…"


# ─── prompts ──────────────────────────────────────────────────────────


_INTENT_SYSTEM = (
    "You write one-sentence summaries of a user's intent in a coding "
    "session. Output only the sentence — no preamble, no quotes, no "
    "markdown. Maximum 25 words."
)

_PROCEDURE_SYSTEM = (
    "You summarise a coding agent's successful procedure as numbered "
    "steps (`1. ...\\n2. ...\\n3. ...`). Each step is a short imperative "
    "sentence. 3–8 steps total. Redact concrete file paths and any "
    "obvious secrets — replace them with `<path>` or `<secret>`. Do NOT "
    "include a preamble or markdown fences. Output only the numbered "
    "list."
)

# Subsystem C follow-up (2026-05-02) — schema-validated path for the
# procedure call. When supported by the provider, the LLM emits a JSON
# object matching :class:`_ExtractedProcedure` and we render the
# numbered list deterministically from the validated steps. Falls back
# to legacy text-output parsing for providers without schema support.
_PROCEDURE_SYSTEM_STRUCTURED = (
    "You summarise a coding agent's successful procedure. Each step is "
    "a short imperative sentence. 3–8 steps total. Redact concrete file "
    "paths and obvious secrets — replace them with `<path>` or `<secret>`."
)

_TRIGGER_SYSTEM = (
    "You write the `description:` line for a Claude SKILL.md frontmatter. "
    "Phrase as 'Use when [user request shape]'. One line, no period at the "
    "end, max 200 characters. Output only the description — no preamble, "
    "no quotes, no markdown."
)


def _build_intent_user(session_summary: str, judge_reason: str) -> str:
    return (
        f"Session summary: {session_summary}\n"
        f"Judge reason: {judge_reason}\n\n"
        "Summarise what the user was trying to do in one sentence."
    )


def _build_procedure_user(intent: str, session_summary: str) -> str:
    return (
        f"User intent: {intent}\n"
        f"Session summary: {session_summary}\n\n"
        "Write the agent's successful procedure as numbered steps. "
        "Redact paths and secrets."
    )


def _build_trigger_user(intent: str, procedure: str) -> str:
    return (
        f"Intent: {intent}\n"
        f"Procedure:\n{procedure}\n\n"
        "Write the SKILL.md description line. Format: 'Use when ...'."
    )


# ─── single-call helper ───────────────────────────────────────────────


async def _llm_call(
    *,
    provider: Any,
    model: str,
    system: str,
    user: str,
    cost_guard: Any,
    max_tokens: int = 400,
) -> str | None:
    """Pre-flight cost guard, run one provider.complete, return text or None."""
    decision = cost_guard.check_budget(
        "anthropic", projected_cost_usd=_PROJECTED_COST_USD
    )
    if not _budget_allows(decision):
        _log.info("skill-evolution: extractor skipped — cost_guard denied")
        return None

    try:
        from plugin_sdk.core import Message

        messages = [Message(role="user", content=user)]
    except Exception:  # noqa: BLE001 — fall back to plain dicts in tests
        messages = [{"role": "user", "content": user}]

    try:
        response = await provider.complete(
            model=model,
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001 — provider failures surface as None
        _log.warning(
            "skill-evolution: extractor provider.complete raised", exc_info=True
        )
        return None

    _record_usage(cost_guard)

    text = _extract_response_text(response).strip()
    return text or None


# ─── SKILL.md rendering ───────────────────────────────────────────────


def _title_case(text: str) -> str:
    """Title-case the first word of each space-split chunk for the H1 heading."""
    return " ".join(w[:1].upper() + w[1:] for w in text.split() if w)


def _render_skill_md(
    *,
    name: str,
    description: str,
    intent: str,
    procedure: str,
    trigger_body: str,
    session_id: str,
    confidence: int,
    generated_at: str,
) -> str:
    """Compose the full SKILL.md text from extracted fragments.

    Uses the OC convention seen in ``opencomputer/skills/code-review/SKILL.md``:
    YAML frontmatter, `# Title`, `## When to use`, `## Procedure`, `## Notes`.
    """
    title = _title_case(intent.rstrip(".")) or name
    date_only = generated_at[:10]  # YYYY-MM-DD slice of ISO timestamp
    return (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"---\n\n"
        f"<!-- Source: auto-generated from session {session_id} on {date_only}; "
        f"provenance.json has full metadata -->\n\n\n"
        f"# {title}\n\n"
        f"## When to use\n\n"
        f"{trigger_body}\n\n"
        f"## Procedure\n\n"
        f"{procedure}\n\n"
        f"## Notes\n\n"
        f"- Generated automatically from a successful session pattern.\n"
        f"- Confidence score: {confidence}/100.\n"
        f"- Review and edit before relying on this skill in production workflows.\n"
    )


# ─── public entry point ───────────────────────────────────────────────


# ─── Subsystem C follow-up: schema-validated procedure extraction ────


class _ExtractedProcedure(BaseModel):
    """Schema for the procedure-extraction LLM call."""

    steps: list[str] = Field(
        min_length=3,
        max_length=8,
        description=(
            "Numbered procedure steps as a list of short imperative "
            "sentences. 3-8 entries, each a single sentence."
        ),
    )


async def _extract_procedure_steps(
    *,
    provider: Any,
    model: str,
    intent: str,
    session_summary: str,
    cost_guard: Any,
    sensitive_filter: Callable[[str], bool] | None,
) -> list[str] | None:
    """Get procedure steps as a validated list when possible.

    Tries :func:`opencomputer.agent.structured.parse_structured` first.
    Schema validation guarantees 3-8 steps, all strings. Falls back to
    the legacy text-output path for providers that don't support
    ``response_schema`` (older models, OpenAI-compat plugins without
    schema wiring).

    Returns ``None`` on cost-guard denial, provider failure, redaction
    that strips the body, or schema mismatch in the legacy parse path —
    all converge on the same caller-handling.
    """
    decision = cost_guard.check_budget(
        "anthropic", projected_cost_usd=_PROJECTED_COST_USD
    )
    if not _budget_allows(decision):
        _log.info("skill-evolution: extractor skipped — cost_guard denied")
        return None

    user_prompt = _build_procedure_user(intent, session_summary)

    # Schema-validated path.
    try:
        from opencomputer.agent.structured import (
            StructuredOutputError,
            parse_structured,
        )
        from plugin_sdk.core import Message

        result = await parse_structured(
            response_model=_ExtractedProcedure,
            messages=[Message(role="user", content=user_prompt)],
            provider=provider,
            model=model,
            system=_PROCEDURE_SYSTEM_STRUCTURED,
            max_tokens=600,
            name="extracted_procedure",
        )
        _record_usage(cost_guard)
        # Apply redaction per-step (preserves per-step granularity for
        # PII filtering); apply the body-usefulness check to the JOINED
        # output, matching the legacy path's whole-body semantics.
        clean_steps: list[str] = [
            _redact_pii(
                _apply_sensitive_filter(step, sensitive_filter)
            ).strip()
            for step in result.steps
        ]
        clean_steps = [s for s in clean_steps if s]
        joined = "\n".join(clean_steps)
        if not (3 <= len(clean_steps) <= 8) or not _is_useful_body(joined):
            # Filter degraded the body below useful threshold — fall
            # through to legacy path rather than emit a degraded skill.
            _log.info(
                "skill-evolution: schema procedure had %d useful steps; "
                "falling back to text path",
                len(clean_steps),
            )
        else:
            return clean_steps
    except StructuredOutputError as exc:
        _log.info(
            "skill-evolution: schema procedure call failed (%s); "
            "falling back to text path",
            exc,
        )
    except Exception:  # noqa: BLE001 — provider/import failures fall back
        _log.info(
            "skill-evolution: schema procedure path raised; falling back",
            exc_info=True,
        )

    # Legacy text-output fallback — preserved for providers without
    # response_schema support.
    procedure_raw = await _llm_call(
        provider=provider,
        model=model,
        system=_PROCEDURE_SYSTEM,
        user=user_prompt,
        cost_guard=cost_guard,
        max_tokens=600,
    )
    if not procedure_raw:
        return None
    procedure_red = _redact_pii(
        _apply_sensitive_filter(procedure_raw, sensitive_filter)
    )
    if not _is_useful_body(procedure_red):
        return None
    # Parse the numbered list back into individual steps so the caller
    # gets a uniform list[str] regardless of which path produced it.
    raw = procedure_red.strip()
    steps: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip "1. " / "2." / "(1)" prefixes so the renderer adds them
        # consistently.
        match = re.match(r"^(?:[(]?\d+[)]?[.)]?\s*)(.*)$", line)
        if match and match.group(1):
            steps.append(match.group(1))
        else:
            steps.append(line)
    if not (3 <= len(steps) <= 8):
        # Legacy output didn't match the count constraint — return None
        # rather than emit a malformed skill.
        return None
    return steps


async def extract_skill_from_session(
    session_id: str,
    *,
    session_db: Any,
    judge_result: Any,
    provider: Any,
    model: str = "claude-haiku-4-5-20251001",
    cost_guard: Any,
    sensitive_filter: Callable[[str], bool] | None = None,
) -> ProposedSkill | None:
    """Generate a SKILL.md candidate from a passing session.

    See module docstring for the three-call pipeline overview.

    Returns ``None`` when:

    * ``cost_guard`` denies any of the three calls;
    * any provider call raises;
    * any LLM response is empty / unusable;
    * after redaction the body is sentinel-only / too short.
    """
    # Best-effort transcript pull. We don't fail hard if the row is
    # missing — the LLM gets less context but the extraction can still
    # produce something marginally useful from the judge's reason alone.
    summary_parts: list[str] = []
    try:
        row = session_db.get_session(session_id)
    except Exception:  # noqa: BLE001
        _log.warning(
            "skill-evolution: get_session(%r) failed in extractor",
            session_id,
            exc_info=True,
        )
        row = None

    if row is not None:
        user_concat = str(getattr(row, "user_messages_concat", "") or "")
        tool_summary = str(getattr(row, "tool_calls_summary", "") or "")
        if user_concat:
            summary_parts.append(f"User said: {user_concat[:1500]}")
        if tool_summary:
            summary_parts.append(f"Tool activity: {tool_summary[:1000]}")
    session_summary = (
        "\n".join(summary_parts) if summary_parts else "(no transcript available)"
    )

    judge_reason = str(getattr(judge_result, "reason", "") or "")
    confidence = int(getattr(judge_result, "confidence", 0) or 0)

    # ── Call 1: Intent ────────────────────────────────────────────────
    intent_raw = await _llm_call(
        provider=provider,
        model=model,
        system=_INTENT_SYSTEM,
        user=_build_intent_user(session_summary, judge_reason),
        cost_guard=cost_guard,
        max_tokens=120,
    )
    if not intent_raw:
        return None
    intent_red = _redact_pii(_apply_sensitive_filter(intent_raw, sensitive_filter))
    if not _is_useful_body(intent_red):
        return None
    intent = _truncate_one_line(intent_red, 200)

    # ── Call 2: Procedure ─────────────────────────────────────────────
    # Subsystem C follow-up — try the schema-validated path first; fall
    # back to legacy text-output if the provider doesn't support
    # response_schema (e.g. older models, providers without native
    # structured-outputs wiring). Failure modes converge on returning
    # None so the rest of the flow handles them identically.
    procedure_steps = await _extract_procedure_steps(
        provider=provider,
        model=model,
        intent=intent,
        session_summary=session_summary,
        cost_guard=cost_guard,
        sensitive_filter=sensitive_filter,
    )
    if procedure_steps is None:
        return None
    procedure = "\n".join(
        f"{i + 1}. {step}" for i, step in enumerate(procedure_steps)
    )

    # ── Call 3: Trigger description ───────────────────────────────────
    trigger_raw = await _llm_call(
        provider=provider,
        model=model,
        system=_TRIGGER_SYSTEM,
        user=_build_trigger_user(intent, procedure),
        cost_guard=cost_guard,
        max_tokens=120,
    )
    if not trigger_raw:
        return None
    trigger_red = _redact_pii(
        _apply_sensitive_filter(trigger_raw, sensitive_filter)
    )
    if not _is_useful_body(trigger_red):
        return None
    description = _truncate_one_line(trigger_red, _DESCRIPTION_MAX_LEN)

    # ── Compose the skill ─────────────────────────────────────────────
    name = f"auto-{session_id[:8]}-{_slugify(intent, max_len=30)}"

    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    body = _render_skill_md(
        name=name,
        description=description,
        intent=intent,
        procedure=procedure,
        trigger_body=description,
        session_id=session_id,
        confidence=confidence,
        generated_at=generated_at,
    )

    # Final defensive PII sweep on the rendered body — covers any leak
    # the per-fragment passes missed (e.g. credit-card text inside a
    # later step that we render verbatim).
    body = _redact_pii(body)

    if not _is_useful_body(body):
        return None

    provenance = {
        "session_id": session_id,
        "generated_at": generated_at,
        "confidence_score": confidence,
        "source_summary": session_summary[:500],
    }

    return ProposedSkill(
        name=name,
        description=description,
        body=body,
        provenance=provenance,
    )


__all__ = [
    "ProposedSkill",
    "_slugify",
    "extract_skill_from_session",
]
