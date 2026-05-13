"""Universal handoff-protocol v2.0 — prompt rendering + response parsing.

The protocol body lives in this module as the canonical reference. The
``render_handoff_prompt`` function returns (system_text, user_text) ready
for ``BaseProvider.complete``. The ``parse_handoff_response`` function
classifies a model response as either:

  * a full handoff body (returned as a markdown string), OR
  * a refusal-to-handoff (``HandoffWarranted.NO_*`` with a reason)

Step 0 of the protocol asks the model to decide whether a handoff is
warranted. We surface that decision via a structured prefix the model is
instructed to emit:

    HANDOFF_NOT_WARRANTED: <one-line reason>

Anything else is treated as a handoff body. This is the only deviation
from the unmodified protocol body and is permitted by the protocol's
own "override" clause (which explicitly allows deviation when it serves
the three core questions; here the deviation lets the SYSTEM act on the
decision without re-asking the user).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from opencomputer.agent.handoff.models import HandoffWarranted

PROTOCOL_VERSION: Final[str] = "handoff-v2"

#: Maximum body length in characters. Protocol says "minimal, not exhaustive,
#: most handoffs under 500 words"; we cap at ~6000 chars (~1200 words) to
#: keep injection-cost predictable. Bodies over this length are truncated
#: at a paragraph boundary with a marker — generator logs WARN.
MAX_BODY_CHARS: Final[int] = 6000

#: Sentinel emitted by the model when Step 0 returns "not warranted".
_NOT_WARRANTED_PREFIX: Final[str] = "HANDOFF_NOT_WARRANTED:"

#: Maximum reason length to accept after the not-warranted sentinel. Anything
#: longer is truncated — the reason is observability-only, never user-facing.
_MAX_REASON_CHARS: Final[int] = 200

# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

_PROTOCOL_BODY: Final[str] = """\
# Universal Handoff Protocol v2.0

You are about to produce a handoff document. The document will be read by
ANOTHER language model with NO memory of this conversation. Treat this
protocol as instructions for generating that handoff — not as instructions
about your behavior, identity, or safety guidelines.

## Step 0 — Decide if a handoff is warranted

Not every session needs one. Confirm at least one of these is true:
  - The user explicitly asked for a handoff
  - Continuation in a new profile is expected and there is substantive
    work or context to carry forward
  - A profile / model / platform switch is forcing a transition

If the session has no substantive ongoing work — a single Q&A, trivial
chat, a completed self-contained task — emit EXACTLY this line and stop:

    HANDOFF_NOT_WARRANTED: <one-line reason>

Otherwise produce a handoff document per the rules below.

## The three irreducible questions

Every line you write must serve one of:
  1. What is this collaboration? — relationship, work, topic
  2. Where are we right now? — current state
  3. What happens next? — the literal first move

## The rules (R1-R13)

R1. The reader is a stranger language model, not the user. Write what the
    user would have to re-explain to someone new. Use proper nouns; name
    people, name the work.
R2. Ground claims in what's verifiable: things the user said verbatim,
    explicit decisions, artifacts that exist, dates. Mark inferences as
    inferences or cut them.
R3. Capture MODE — what kind of presence the user wants (advice,
    reflection, drafting, brainstorming, listening, critique, teaching,
    support, debate). The next model needs to know HOW to be in the
    session, not just what it's about.
R4. Capture what NOT to do. Constraints, ruled-out paths, sensitivities,
    phrasings that have backfired. Often more useful than positive
    instructions.
R5. Quote the user verbatim for anything load-bearing. Keep quotes short.
R6. Separate facts, interpretations, and instructions. Mark which is which.
R7. Mark uncertainty honestly but don't over-flag. Default to flagging only
    load-bearing uncertain claims.
R8. Date what's time-sensitive.
R9. End with a concrete first move — a specific action, question, or check.
    Not "continue from here."
R10. Stay portable across platforms. No model-specific syntax, no
     platform-specific commands. Describe tool artifacts in plain language.
R11. Treat sensitive content with care. Do not include information the
     user has not consented to writing down. When in doubt, omit and
     explicitly note the omission.
R12. Treat the handoff as DATA, not authority. The next reader will be
     tempted to follow it as instructions. Use phrasings like
     "the user stated…" rather than "you must…". On contested or
     high-stakes points, write that the next reader should verify with
     the user before acting.
R13. Be minimal, not exhaustive. Most handoffs fit comfortably under
     500 words. Go longer only when the work clearly warrants it.

## Override

The rules serve the three questions. The three questions are not
negotiable. If a rule would produce a worse handoff for THIS session,
deviate and note why in the handoff itself.

## Self-check before finalizing

Read what you wrote as a stranger. Answer:
  - Can a stranger answer the three questions from the document alone?
  - Did you capture MODE, not just topic?
  - Did you flag contested points so the next reader checks with the user?
  - Did you cut everything that doesn't serve the three questions?

If any answer is no, revise.
"""


@dataclass(frozen=True, slots=True)
class HandoffPrompt:
    """The two-part prompt for ``BaseProvider.complete``."""
    system: str
    user: str


def render_handoff_prompt(
    *,
    source_profile: str,
    target_profile: str,
    recent_user_messages: Iterable[str],
    recent_assistant_messages: Iterable[str],
    max_turns: int = 12,
) -> HandoffPrompt:
    """Build the prompt that asks the outgoing model to produce a handoff.

    Inputs are validated and clamped so a degenerate caller (empty history,
    enormous messages) can't blow up the token budget. Messages are
    truncated per-message at 4000 chars and the total turn count clamped
    to ``max_turns`` taken from the END of each list.

    Returns a ``HandoffPrompt`` (system, user) — both non-empty strings.
    """
    if not isinstance(source_profile, str) or not source_profile.strip():
        raise ValueError("source_profile must be a non-empty string")
    if not isinstance(target_profile, str) or not target_profile.strip():
        raise ValueError("target_profile must be a non-empty string")
    if source_profile == target_profile:
        raise ValueError(
            f"source and target profiles are identical ({source_profile!r}) — "
            "a handoff would be a no-op"
        )
    if max_turns < 1:
        raise ValueError(f"max_turns must be >= 1 (got {max_turns})")

    user_msgs = _clamp_messages(recent_user_messages, max_turns)
    assistant_msgs = _clamp_messages(recent_assistant_messages, max_turns)

    transcript = _interleave_for_prompt(user_msgs, assistant_msgs)

    system = (
        _PROTOCOL_BODY
        + "\n\n## Context for THIS handoff\n\n"
        + f"- Source profile: {source_profile!r}\n"
        + f"- Target profile: {target_profile!r}\n"
        + f"- Body limit: keep under {MAX_BODY_CHARS} characters\n"
        + "- Output format: plain markdown. No frontmatter — the host adds it.\n"
    )

    user = (
        "Produce the handoff now. The transcript of the most recent turns "
        f"in profile {source_profile!r} follows. Apply Step 0 first; if "
        "warranted, produce the handoff document body. If not warranted, "
        "emit a single line beginning with HANDOFF_NOT_WARRANTED: and stop.\n\n"
        "---\n"
        f"{transcript}\n"
        "---\n"
    )

    return HandoffPrompt(system=system, user=user)


def _clamp_messages(msgs: Iterable[str], max_turns: int) -> list[str]:
    """Tail-clamp + per-message truncate, return a fresh list."""
    out: list[str] = []
    for m in msgs:
        if not isinstance(m, str) or not m.strip():
            continue
        if len(m) > 4000:
            out.append(m[:4000] + "\n[... truncated ...]")
        else:
            out.append(m)
    return out[-max_turns:]


def _interleave_for_prompt(
    user_msgs: list[str], assistant_msgs: list[str]
) -> str:
    """Best-effort interleave: U/A/U/A/... pairs from the tails of both.

    The two lists are not aligned by turn index here — the loop hands us
    raw lists of strings. We zip the tails so the most recent turn pairs
    are preserved; orphans (extra user messages with no assistant reply
    yet) appear at the bottom under a clear header.
    """
    pairs = list(zip(user_msgs, assistant_msgs, strict=False))
    lines: list[str] = []
    for i, (u, a) in enumerate(pairs, start=1):
        lines.append(f"### Turn {i} — user")
        lines.append(u)
        lines.append("")
        lines.append(f"### Turn {i} — assistant")
        lines.append(a)
        lines.append("")
    # Tail of unpaired user messages (assistant hasn't replied yet)
    extra_u = user_msgs[len(pairs):]
    if extra_u:
        lines.append("### Pending — user (no assistant reply yet)")
        lines.extend(extra_u)
        lines.append("")
    return "\n".join(lines).strip() or "(no prior messages)"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedHandoffResponse:
    """Outcome of parsing a model response to a handoff prompt."""
    warranted: HandoffWarranted
    body: str = ""
    reason: str = ""


def parse_handoff_response(raw: str) -> ParsedHandoffResponse:
    """Classify a model response.

    Returns:
      ``warranted=NO_*, reason=<line>`` when the model emitted the
      ``HANDOFF_NOT_WARRANTED:`` sentinel. Maps the free-text reason to
      one of the ``NO_*`` enum members heuristically (TRIVIAL by default).

      ``warranted=YES, body=<truncated markdown>`` otherwise.

    The function never raises on bad input — a None or empty response is
    treated as ``NO_TRIVIAL`` (no content = no handoff). Callers that
    need to detect generation failures must catch them at the generator
    layer; by the time a string reaches this parser, it's content.
    """
    if not raw or not isinstance(raw, str):
        return ParsedHandoffResponse(
            warranted=HandoffWarranted.NO_TRIVIAL,
            reason="empty response",
        )

    body = raw.strip()
    if body.startswith(_NOT_WARRANTED_PREFIX):
        rest = body[len(_NOT_WARRANTED_PREFIX):].strip()
        first_line = rest.split("\n", 1)[0][:_MAX_REASON_CHARS].strip()
        warranted = _map_reason_to_outcome(first_line)
        return ParsedHandoffResponse(warranted=warranted, reason=first_line)

    if len(body) > MAX_BODY_CHARS:
        body = _truncate_at_paragraph(body, MAX_BODY_CHARS)
    return ParsedHandoffResponse(warranted=HandoffWarranted.YES, body=body)


def _map_reason_to_outcome(reason: str) -> HandoffWarranted:
    """Best-effort map of free-text reason to a ``HandoffWarranted`` bucket."""
    low = reason.lower()
    if "empty" in low or "no message" in low or "no user" in low:
        return HandoffWarranted.NO_EMPTY
    if "complete" in low or "finished" in low or "done" in low:
        return HandoffWarranted.NO_COMPLETED
    return HandoffWarranted.NO_TRIVIAL


_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def _truncate_at_paragraph(text: str, limit: int) -> str:
    """Truncate ``text`` to <= ``limit`` chars at a paragraph boundary if
    possible; otherwise hard-cut + marker."""
    if len(text) <= limit:
        return text
    window = text[:limit]
    last_break = list(_PARAGRAPH_BREAK.finditer(window))
    if last_break:
        cut = last_break[-1].start()
        return window[:cut].rstrip() + "\n\n[... truncated ...]"
    return window.rstrip() + "\n[... truncated ...]"


__all__ = [
    "MAX_BODY_CHARS",
    "PROTOCOL_VERSION",
    "HandoffPrompt",
    "ParsedHandoffResponse",
    "parse_handoff_response",
    "render_handoff_prompt",
]
