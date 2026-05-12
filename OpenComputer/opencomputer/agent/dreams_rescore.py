"""DREAMS.md re-scoring — Gap 3 from self-evolution-gaps-deep-dive.md.

Parse DREAMS.md (the dreaming-v2 holding pen for entries that failed the
score gate but cleared recall+diversity), re-score each entry through a
configurable provider, and report the diff. The deep-dive doc's
diagnostic move: "Pull the ~20% real-conversation content out of
DREAMS.md, re-score with Sonnet (not Haiku), compare scores — that tells
you whether the score gate is correctly conservative, underconfident, or
miscalibrated for your use pattern."

Design constraints:

* **Pure parsing.** ``parse_dreams_md`` is IO-free; takes a string,
  returns ``list[DreamEntry]``. CLI handles IO.
* **Per-entry try/except.** A malformed line never aborts the whole
  rescore; it's logged at WARN and skipped.
* **Cap by default.** ``--limit`` enforces a maximum number of entries
  to rescore so a 5,000-line DREAMS.md can't accidentally trigger 5,000
  LLM calls + a $50 bill.
* **Dry-run default.** Display the diff; only write to MEMORY.md when
  the caller explicitly passes ``--apply``.
* **No transcript persistence.** Parser keeps raw text in-memory while
  the CLI runs; nothing is written to disk except the optional
  MEMORY.md promotions when ``--apply`` is set.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger("opencomputer.agent.dreams_rescore")


# ── parser ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DreamEntry:
    """One DREAMS.md row, parsed into structured fields.

    The raw_text field preserves the exact entry-line as parsed so the
    rescorer can pass identical content to the LLM as dreaming-v2's
    original Haiku call would have. Date / tools / question / answer
    are derived from raw_text for diagnostic display.
    """

    raw_text: str
    date: str  # YYYY-MM-DD; empty string when missing
    tools: tuple[str, ...]  # [] when no `[tools: …]` prefix
    question: str  # truncated/cleaned ``Q: …`` body, possibly empty
    answer: str  # truncated/cleaned ``A: …`` body, possibly empty


# Patterns for "- DATE: [tools: X, Y] Q: ... → A: ..."
# - All four substring boundaries optional except the Q: → A: pair.
# - Date is a 10-char YYYY-MM-DD prefix; missing → empty string.
# - Tools is an inline `[tools: ...]` prefix; missing → empty.
# - Question / Answer extracted non-greedy on Q-side so the FIRST "→"
#   becomes the boundary even when the answer contains more arrows.
_TOOLS_RE = re.compile(r"^\[tools:\s*([^\]]+)\]\s*")
_QA_RE = re.compile(r"^(?:Q:\s*)?(.*?)\s*→\s*A:\s*(.*)$", re.DOTALL)


def parse_dreams_md(content: str, *, max_entries: int | None = None) -> list[DreamEntry]:
    """Parse DREAMS.md text into a list of structured entries.

    The DREAMS.md format is one entry per text block (entries separated
    by blank lines), each block starting with ``- DATE: [tools: ...]
    Q: ... → A: ...``. Malformed blocks are skipped with WARN-level
    logging — a single bad entry never aborts the whole parse.

    ``max_entries`` truncates the output if specified. Callers that want
    a hard upper bound pass it explicitly; the parser itself does NOT
    impose a default cap so unit tests can cover large inputs.
    """
    entries: list[DreamEntry] = []
    if not content:
        return entries
    # Use a regex split that captures empty lines between blocks but
    # preserves multi-line blocks. Splitting on /^$/ via empty-line is
    # robust to trailing whitespace.
    blocks = re.split(r"\n\s*\n", content.strip())
    for raw_block in blocks:
        block = raw_block.strip()
        if not block:
            continue
        entry = _parse_one_block(block)
        if entry is None:
            continue
        entries.append(entry)
        if max_entries is not None and len(entries) >= max_entries:
            break
    return entries


def _parse_one_block(block: str) -> DreamEntry | None:
    """Parse a single DREAMS.md text block. Returns None on malformed shape.

    Order of operations:
      1. Strip leading ``- `` / ``-`` bullet (most entries have it).
      2. Try to match the ``YYYY-MM-DD:`` prefix; capture and strip if present.
      3. Try to match the ``[tools: …]`` prefix; capture and strip if present.
      4. Try to match the ``Q: … → A: …`` body. If the block has neither
         a date prefix nor a Q/A marker, treat as noise and skip.
    """
    date = ""
    body = block
    if body.startswith("- "):
        body = body[2:]
    elif body.startswith("-"):
        body = body[1:].lstrip()
    # Date prefix on the (now-bullet-stripped) body.
    date_match = re.match(r"^\s*(\d{4}-\d{2}-\d{2})\s*:\s*", body)
    if date_match:
        date = date_match.group(1)
        body = body[date_match.end():]
    elif "→" not in body or "A:" not in body:
        logger.warning(
            "dreams_rescore: skipping block with no date prefix and no Q:/A: markers"
        )
        return None
    tools: tuple[str, ...] = ()
    tm = _TOOLS_RE.match(body)
    if tm:
        tools_raw = tm.group(1)
        tools = tuple(t.strip() for t in tools_raw.split(",") if t.strip())
        body = body[tm.end():]
    qa = _QA_RE.match(body)
    if qa is None:
        # No Q:→A: structure detected. Preserve raw_text so caller can
        # decide what to do; but the Q/A fields are empty. Most
        # downstream consumers will skip these.
        logger.debug("dreams_rescore: block has no Q: → A: structure; %s", block[:60])
        return DreamEntry(
            raw_text=block,
            date=date,
            tools=tools,
            question="",
            answer="",
        )
    question = qa.group(1).strip()
    answer = qa.group(2).strip()
    return DreamEntry(
        raw_text=block,
        date=date,
        tools=tools,
        question=question,
        answer=answer,
    )


# ── rescore engine ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RescoreOutcome:
    """One entry's re-scoring result.

    ``new_score`` is in [0.0, 1.0]. ``promoted_candidate`` is True when
    the new score crosses the caller-supplied threshold AND the entry
    has both a question and an answer. The CLI uses this flag to filter
    candidates for ``--apply`` mode.
    """

    entry: DreamEntry
    new_score: float
    error: str | None = None
    promoted_candidate: bool = False

    @property
    def display_question(self) -> str:
        """Truncated Q: for table display (max 60 chars + ellipsis)."""
        q = self.entry.question or "(no Q)"
        return q[:60] + ("…" if len(q) > 60 else "")


ScoreFn = Callable[[str], Awaitable[float]]


async def rescore_entries(
    entries: list[DreamEntry],
    *,
    score_fn: ScoreFn,
    promote_threshold: float = 0.75,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[RescoreOutcome]:
    """Re-score every entry via the caller-provided ``score_fn``.

    A failing ``score_fn`` call records ``error=...`` on the outcome and
    moves on — one provider blip never aborts the whole rescore. The
    caller renders errors visibly so the operator can decide whether to
    retry.

    ``promote_threshold`` defaults to ~0.75 (a meaningful jump above the
    score gate's default 0.65) so that "promotion candidates" are
    entries where the rescorer thinks there's clear improvement, not
    borderline cases.
    """
    outcomes: list[RescoreOutcome] = []
    total = len(entries)
    for i, entry in enumerate(entries, start=1):
        if on_progress is not None:
            try:
                on_progress(i, total)
            except Exception:  # noqa: BLE001
                # Progress callback must never abort the rescore.
                logger.debug("dreams_rescore: on_progress callback raised", exc_info=True)
        # Privacy: feed only the raw_text into score_fn (same surface
        # the original dreaming-v2 pipeline would have fed). We do NOT
        # pass question/answer separately — that would be a behavior
        # change relative to the original gate.
        try:
            new_score = float(await score_fn(entry.raw_text))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "dreams_rescore: score_fn raised on entry; recording as error",
                exc_info=True,
            )
            outcomes.append(
                RescoreOutcome(
                    entry=entry,
                    new_score=0.0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        # Clamp into [0.0, 1.0] — defensive against provider returning
        # an out-of-range string-coerced value.
        new_score = max(0.0, min(1.0, new_score))
        promoted = new_score >= promote_threshold and bool(entry.question) and bool(entry.answer)
        outcomes.append(
            RescoreOutcome(
                entry=entry,
                new_score=new_score,
                promoted_candidate=promoted,
            )
        )
    return outcomes


# ── promotion writer (M4 stretch) ────────────────────────────────────


def render_promotion_candidates(outcomes: list[RescoreOutcome]) -> list[str]:
    """Extract MEMORY.md-ready lines from successful promotion candidates.

    Each candidate becomes a single bullet of the form::

        - DATE: Q: <question> → A: <answer>

    Returns the bullet strings; caller is responsible for atomic batch
    write into MEMORY.md (which has its own MemoryManager + flock).
    """
    lines: list[str] = []
    for o in outcomes:
        if not o.promoted_candidate:
            continue
        date_part = f"{o.entry.date}: " if o.entry.date else ""
        lines.append(f"- {date_part}Q: {o.entry.question} → A: {o.entry.answer}")
    return lines


__all__ = [
    "DreamEntry",
    "RescoreOutcome",
    "ScoreFn",
    "parse_dreams_md",
    "rescore_entries",
    "render_promotion_candidates",
]
