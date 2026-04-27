"""Pattern detector — two-stage filter for auto-skill-evolution (T2).

Stage 1 is a cheap heuristic that runs on every ``SessionEndEvent``. It
rejects sessions that are obviously not skill-worthy (too short, pure
filler, sensitive context, near-duplicate of an existing skill, or
errored-out without recovery). The check is intentionally pessimistic
— it should let through marginal cases, since Stage 2 is the expensive
step. False positives at Stage 1 are cheap; false negatives leak past.

Stage 2 is an LLM judge backed by a small Haiku call. Before invoking
the model we pre-flight against ``cost_guard`` so a runaway loop can
never burn more than the configured per-day budget. The judge returns a
structured score (confidence + novelty + reason) which the caller
thresholds — the judge itself does **not** apply a confidence cutoff.

The two surfaces are split so they can be unit-tested independently:
Stage 1 is pure / sync, Stage 2 is async + IO. The downstream pipeline
(T3) wires them together and applies the threshold + persistence.

Privacy
-------
Stage 1 reads ``user_messages_concat`` from the SessionDB row to do the
keyword-overlap dedup. The string is used in-process only — no value is
attached to the returned ``CandidateScore`` (the ``summary_hint`` field
is left empty here; T3's summariser populates it before Stage 2 runs).

SessionDB shape
---------------
The functions read the following attributes off the value returned by
``session_db.get_session(session_id)``, falling back to safe defaults
when missing:

* ``turn_count`` (int)
* ``user_messages_total_chars`` (int)
* ``user_messages_concat`` (str) — best-effort concat of user messages
* ``tool_calls`` (iterable of objects with ``is_error: bool`` and
  ``turn_index: int``)

The current ``SessionDB.get_session`` returns a raw row dict with only
the ``sessions`` table columns (id, started_at, model, ...). The fields
above are computed downstream — either by an adapter that wraps the row
with the derived counts (preferred), or by extending ``SessionDB`` with
a richer accessor in T3. The tests inject a ``MagicMock`` with the
attributes set directly, which is the contract this module commits to.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plugin_sdk.ingestion import SessionEndEvent

_log = logging.getLogger("opencomputer.skill_evolution.pattern_detector")

# ─── tunables ─────────────────────────────────────────────────────────

#: Sessions shorter than this many turns can't carry a learnable pattern.
_MIN_TURNS = 3

#: Total user-message char budget below which we treat the session as
#: conversational filler (greeting / yes-no / "thanks").
_MIN_USER_CHARS = 50

#: Fraction of significant words from an existing skill description that
#: must overlap with the session's user messages for us to flag the
#: session as a duplicate of that skill. Tuned to be pessimistic — false
#: negatives (missed dedup) are recoverable in Stage 2; false positives
#: silently kill candidate sessions.
_DUPE_OVERLAP_THRESHOLD = 0.5

#: Heuristic per-call cost we pre-flight against ``cost_guard``. Real
#: spend is recorded post-call by ``record_usage``. Keeping this small
#: but non-zero means the daily-limit check actually fires before the
#: budget is fully drained. Haiku 4.5 prompt+completion for the judge
#: prompt below is well under $0.01; we round up for headroom.
_JUDGE_PROJECTED_COST_USD = 0.01

#: Words that contribute no signal to overlap detection. Kept short on
#: purpose — adding too many false-negatives the dedup check.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "please",
        "the",
        "this",
        "to",
        "use",
        "with",
        "when",
        "you",
        "your",
    }
)

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")
_FRONTMATTER_DESC_RE = re.compile(
    r"^---\s*\n(?P<body>.*?)\n---\s*\n", re.DOTALL | re.MULTILINE
)
_DESC_LINE_RE = re.compile(r"^description:\s*(?P<desc>.+?)\s*$", re.MULTILINE)


# ─── public dataclasses ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CandidateScore:
    """Stage-1 verdict.

    ``is_candidate`` is True when the session passes every cheap filter.
    ``rejection_reason`` is the specific rule that fired (only populated
    when ``is_candidate`` is False — empty string otherwise). The other
    fields surface metadata Stage 2 needs without re-querying the DB.
    """

    is_candidate: bool
    rejection_reason: str = ""
    session_id: str = ""
    turn_count: int = 0
    summary_hint: str = ""


@dataclass(frozen=True, slots=True)
class JudgeResult:
    """Stage-2 verdict from the LLM judge.

    ``confidence`` is 0..100 (the judge's self-reported certainty).
    ``is_novel`` flips False when the judge thinks an existing skill
    already covers the pattern. ``reason`` is a single-sentence
    explanation surfaced in the proposal review UX (T6). ``used_tokens``
    is best-effort — populated when the provider response carries a
    ``usage`` dataclass; left at 0 when unavailable.
    """

    confidence: int
    is_novel: bool
    reason: str
    used_tokens: int = 0


# ─── Stage 1: heuristic ───────────────────────────────────────────────


def _significant_words(text: str) -> set[str]:
    """Return the lowercased non-stopword tokens of length >=3."""
    return {
        w.lower()
        for w in _WORD_RE.findall(text or "")
        if w.lower() not in _STOPWORDS
    }


def _extract_skill_description(skill_md: Path) -> str:
    """Best-effort frontmatter ``description:`` extractor.

    Returns ``""`` for any failure mode (unreadable, no frontmatter, no
    ``description:`` key). This is a heuristic file-walker — we don't
    want a malformed SKILL.md to crash the detector.
    """
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = _FRONTMATTER_DESC_RE.match(text)
    if not m:
        return ""
    body = m.group("body")
    desc = _DESC_LINE_RE.search(body)
    if not desc:
        return ""
    return desc.group("desc").strip().strip('"').strip("'")


def _iter_existing_descriptions(skills_dir: Path) -> Iterable[tuple[str, str]]:
    """Yield (skill_name, description) for every SKILL.md under ``skills_dir``."""
    if not skills_dir or not skills_dir.exists():
        return
    for skill_md in skills_dir.glob("*/SKILL.md"):
        desc = _extract_skill_description(skill_md)
        if desc:
            yield skill_md.parent.name, desc


def _has_recovery_after_error(tool_calls: Iterable[Any]) -> bool:
    """Return True when at least one successful tool call follows the
    last errored call (or no errors occurred at all).

    Sessions that hit an error and never recover are usually not the
    pattern we want to learn from — they're either a debugging dead-end
    or a tool the user gave up on. Sessions that error and continue,
    on the other hand, often encode useful "how to recover from X"
    knowledge worth promoting to a skill.
    """
    last_error_turn = -1
    last_success_turn = -1
    saw_error = False
    for tc in tool_calls or ():
        is_error = bool(getattr(tc, "is_error", False))
        turn = int(getattr(tc, "turn_index", 0))
        if is_error:
            saw_error = True
            if turn > last_error_turn:
                last_error_turn = turn
        else:
            if turn > last_success_turn:
                last_success_turn = turn
    if not saw_error:
        return True
    return last_success_turn > last_error_turn


def is_candidate_session(
    session_end_event: SessionEndEvent,
    *,
    session_db: Any,
    existing_skills_dir: Path,
    sensitive_filter: Callable[[Any], bool] | None,
) -> CandidateScore:
    """Stage 1: cheap heuristic filter. No LLM calls.

    Reject if:
      * ``had_errors`` AND no successful tool call followed the last
        error (no recovery signal).
      * ``turn_count`` < :data:`_MIN_TURNS` (too short to be a pattern).
      * Total user-message chars < :data:`_MIN_USER_CHARS` (filler).
      * The session contained sensitive-app foreground events
        (``sensitive_filter`` returned True for the session row).
      * The session's user messages share more than
        :data:`_DUPE_OVERLAP_THRESHOLD` of their significant tokens with
        any existing skill's ``description:`` line.

    Returns
    -------
    CandidateScore
        ``is_candidate=True`` when no rule fired. ``rejection_reason``
        names the rule that fired on rejection.
    """
    session_id = getattr(session_end_event, "session_id", None) or ""
    turn_count = int(session_end_event.turn_count)

    # Fast path: turn count is on the event itself, no DB hit needed.
    if turn_count < _MIN_TURNS:
        return CandidateScore(
            is_candidate=False,
            rejection_reason=f"turn_count too short ({turn_count} < {_MIN_TURNS})",
            session_id=session_id,
            turn_count=turn_count,
        )

    row = None
    try:
        row = session_db.get_session(session_id)
    except Exception:  # noqa: BLE001 — DB hiccup must not crash the detector
        _log.warning(
            "skill-evolution: get_session(%r) failed — treating as non-candidate",
            session_id,
            exc_info=True,
        )
        return CandidateScore(
            is_candidate=False,
            rejection_reason="session_db unavailable",
            session_id=session_id,
            turn_count=turn_count,
        )

    if row is None:
        return CandidateScore(
            is_candidate=False,
            rejection_reason="session row missing",
            session_id=session_id,
            turn_count=turn_count,
        )

    # Pull derived fields with safe defaults — see module docstring.
    user_chars = int(getattr(row, "user_messages_total_chars", 0) or 0)
    user_text = str(getattr(row, "user_messages_concat", "") or "")
    tool_calls = list(getattr(row, "tool_calls", []) or [])

    if user_chars < _MIN_USER_CHARS:
        return CandidateScore(
            is_candidate=False,
            rejection_reason=(
                f"conversational filler — only {user_chars} user chars "
                f"(< {_MIN_USER_CHARS})"
            ),
            session_id=session_id,
            turn_count=turn_count,
        )

    if session_end_event.had_errors and not _has_recovery_after_error(tool_calls):
        return CandidateScore(
            is_candidate=False,
            rejection_reason="errors with no recovery signal after last failure",
            session_id=session_id,
            turn_count=turn_count,
        )

    if sensitive_filter is not None:
        try:
            if sensitive_filter(row):
                return CandidateScore(
                    is_candidate=False,
                    rejection_reason="sensitive-app context in session",
                    session_id=session_id,
                    turn_count=turn_count,
                )
        except Exception:  # noqa: BLE001 — filter bug must not crash detector
            _log.warning(
                "skill-evolution: sensitive_filter raised — treating as sensitive",
                exc_info=True,
            )
            return CandidateScore(
                is_candidate=False,
                rejection_reason="sensitive_filter raised — defaulting to reject",
                session_id=session_id,
                turn_count=turn_count,
            )

    # Dedup against existing skills. Best-effort overlap of significant
    # tokens — false negatives are fine (Stage 2 catches them); false
    # positives silently kill viable candidates so we err pessimistic.
    user_tokens = _significant_words(user_text)
    if user_tokens:
        for skill_name, desc in _iter_existing_descriptions(existing_skills_dir):
            desc_tokens = _significant_words(desc)
            if not desc_tokens:
                continue
            overlap = len(user_tokens & desc_tokens) / len(desc_tokens)
            if overlap >= _DUPE_OVERLAP_THRESHOLD:
                return CandidateScore(
                    is_candidate=False,
                    rejection_reason=(
                        f"duplicate of existing skill {skill_name!r} "
                        f"(overlap={overlap:.2f})"
                    ),
                    session_id=session_id,
                    turn_count=turn_count,
                )

    return CandidateScore(
        is_candidate=True,
        rejection_reason="",
        session_id=session_id,
        turn_count=turn_count,
        summary_hint="",  # T3 fills this from the session transcript.
    )


# ─── Stage 2: LLM judge ───────────────────────────────────────────────


_JUDGE_SYSTEM_PROMPT = """\
You are a careful classifier deciding whether a coding-agent session
represents a NEW, REUSABLE pattern that should become a Claude skill.

Return ONLY a single JSON object — no prose, no markdown fences. Schema:

  {"confidence": <int 0..100>, "novel": <bool>, "reason": "<one sentence>"}

`confidence` reflects how strongly the session encodes a reusable pattern
(0 = definitely a one-off, 100 = clearly a repeatable workflow).
`novel` is False when an existing skill from the provided list already
covers the pattern.

Calibration examples:
- POSITIVE: "User asked to port a C++ module to Python via Cython,
  including struct mapping and error-handling wrappers." →
  {"confidence": 82, "novel": true, "reason": "specific port-and-wrap workflow not in existing skills"}
- NEGATIVE (one-off chat): "User said hi and asked the model what
  weather is like." →
  {"confidence": 5, "novel": false, "reason": "conversational filler, no transferable workflow"}
- NEGATIVE (already covered): "User reviewed a pull request for
  security issues." (when existing skills include `code-review`) →
  {"confidence": 20, "novel": false, "reason": "code-review skill already covers this"}
"""


def _build_judge_user_prompt(
    *, transcript_summary: str, existing_skill_names: list[str]
) -> str:
    skills_list = (
        "\n".join(f"- {n}" for n in existing_skill_names)
        if existing_skill_names
        else "(none)"
    )
    return (
        f"Existing skill names:\n{skills_list}\n\n"
        f"Session summary:\n{transcript_summary}\n\n"
        "Respond with the JSON object only."
    )


def _extract_response_text(response: Any) -> str:
    """Pull text out of a ProviderResponse-like object.

    The real ``ProviderResponse`` exposes the assistant text via
    ``response.message.content``. The tests, however, mock the
    provider's response with a plain ``MagicMock(content="...")`` —
    so we look for ``content`` on the response first and fall back to
    the message attribute if needed. Returns ``""`` when neither path
    produces a string (which downstream parses as a parse failure).
    """
    raw = getattr(response, "content", None)
    if isinstance(raw, str):
        return raw
    msg = getattr(response, "message", None)
    raw = getattr(msg, "content", None) if msg is not None else None
    if isinstance(raw, str):
        return raw
    return ""


def _parse_judge_response(text: str) -> JudgeResult | None:
    """Parse the judge's JSON output. Returns None on any failure.

    The judge prompt asks for a bare JSON object, but defensively we
    strip surrounding markdown fences and stray prose by locating the
    first ``{`` / last ``}`` pair before parsing. Fields outside the
    schema are ignored.
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
        confidence = int(parsed.get("confidence", -1))
    except (TypeError, ValueError):
        return None
    if not 0 <= confidence <= 100:
        return None
    return JudgeResult(
        confidence=confidence,
        is_novel=bool(parsed.get("novel", False)),
        reason=str(parsed.get("reason", "")).strip(),
    )


def _budget_allows(decision: Any) -> bool:
    """Normalise a cost_guard.check_budget return into a bool.

    The real API returns a :class:`BudgetDecision` dataclass with an
    ``.allowed`` attribute. Tests, by contrast, mock the call to return
    a bare ``True`` / ``False``. We accept either shape so the production
    wiring and the unit tests both work without an adapter.
    """
    if isinstance(decision, bool):
        return decision
    return bool(getattr(decision, "allowed", False))


async def judge_candidate_async(
    score: CandidateScore,
    *,
    transcript_summary: str,
    existing_skill_names: list[str],
    provider: Any,
    model: str = "claude-haiku-4-5-20251001",
    cost_guard: Any,
    daily_budget_calls: int = 20,  # noqa: ARG001 — caller-side throttle, kept for future use
) -> JudgeResult | None:
    """Stage 2: LLM judge. Returns ``None`` on budget exhaustion or parse
    failure, so callers can treat ``None`` as "no judgement available".

    The judge is intentionally cheap: a single Haiku call with a short
    structured prompt. We pre-flight a heuristic projected cost against
    ``cost_guard.check_budget`` — if denied, we never call the provider
    and return ``None``. After a successful response we record actual
    usage via ``cost_guard.record_usage`` so the next call's pre-flight
    sees the up-to-date total.

    Caller-side filtering
    ---------------------
    Callers should apply their own confidence threshold (e.g. 70). We
    return the raw judgement so the downstream pipeline (T3) can plot
    the distribution and tune thresholds without invalidating cached
    judgements.
    """
    decision = cost_guard.check_budget(
        "anthropic", projected_cost_usd=_JUDGE_PROJECTED_COST_USD
    )
    if not _budget_allows(decision):
        _log.info(
            "skill-evolution: judge skipped — cost_guard denied (session=%s)",
            score.session_id,
        )
        return None

    user_prompt = _build_judge_user_prompt(
        transcript_summary=transcript_summary,
        existing_skill_names=existing_skill_names,
    )

    try:
        from plugin_sdk.core import Message  # local import — keeps the
        # detector importable in tests that don't have the full SDK
        # dependency tree available.

        messages = [Message(role="user", content=user_prompt)]
    except Exception:  # noqa: BLE001 — fall back to dict-shaped messages
        messages = [{"role": "user", "content": user_prompt}]

    try:
        response = await provider.complete(
            model=model,
            messages=messages,
            system=_JUDGE_SYSTEM_PROMPT,
            max_tokens=256,
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001 — provider failure surfaces as None
        _log.warning(
            "skill-evolution: judge provider.complete raised — treating as None",
            exc_info=True,
        )
        return None

    text = _extract_response_text(response)
    parsed = _parse_judge_response(text)

    # Best-effort cost recording — only when we actually received a
    # response (whether parseable or not). Same projected number we
    # used for the pre-flight; T3 may swap this for a tokens-aware
    # estimate once usage data is wired through.
    try:
        cost_guard.record_usage(
            "anthropic",
            cost_usd=_JUDGE_PROJECTED_COST_USD,
            operation="skill_evolution_judge",
        )
    except TypeError:
        # Tests mock record_usage as a bare MagicMock — it accepts any
        # signature, but production code requires a kwarg. The bare
        # except above would mask real provider bugs; this narrow
        # catch only handles the signature-mismatch path.
        cost_guard.record_usage(provider="anthropic", cost_usd=_JUDGE_PROJECTED_COST_USD)
    except Exception:  # noqa: BLE001 — never let recording crash the judge path
        _log.warning("skill-evolution: cost_guard.record_usage failed", exc_info=True)

    if parsed is None:
        return None

    # Best-effort token count from a real ProviderResponse.usage.
    usage = getattr(response, "usage", None)
    used_tokens = 0
    if usage is not None:
        used_tokens = int(
            getattr(usage, "output_tokens", 0) or 0
        ) + int(getattr(usage, "input_tokens", 0) or 0)

    if used_tokens:
        # Re-pack with token count populated.
        return JudgeResult(
            confidence=parsed.confidence,
            is_novel=parsed.is_novel,
            reason=parsed.reason,
            used_tokens=used_tokens,
        )
    return parsed


__all__ = [
    "CandidateScore",
    "JudgeResult",
    "is_candidate_session",
    "judge_candidate_async",
]
