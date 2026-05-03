"""LLM-mediated recall synthesis (Round 4 Item 1).

OC's keyword-FTS5 recall returns raw match snippets. For an open
question like "when did I ask about kubernetes?", that's a wall of
text the agent then has to re-parse. Hermes's
`tools/session_search_tool.py` proves the pattern: keep FTS5
retrieval (cheap), but post-process the top candidates through a
cheap LLM that synthesises a focused answer with citations.

This module does the LLM step. Given (query, candidates), it asks
``claude-haiku-4-5`` to produce 1-3 short sentences naming WHEN
something was asked + the session/turn citation, anchored to ids
that exist in the candidate set (so it can't hallucinate
non-existent sessions).

Opt-out:
- ``OPENCOMPUTER_RECALL_SYNTHESIS=0`` — env var disables for the
  whole process. Useful in CI or when offline.
- ``synthesize=False`` arg to :func:`synthesize_recall` — per-call.

Failure handling: every LLM-call exception is logged at debug and
returns ``None``, so callers fall back to raw candidates.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

_log = logging.getLogger("opencomputer.recall_synthesizer")

#: Cheap model used for synthesis. Independent of the user's primary
#: model so synthesis stays fast + cheap regardless of their config.
_SYNTH_MODEL = "claude-haiku-4-5"

#: Cap on the synthesis output. Short answers + citations only.
_SYNTH_MAX_TOKENS = 300

#: Minimum candidate count to bother synthesising. With <3 hits, raw
#: snippets are short enough that synthesis is pure overhead.
_MIN_CANDIDATES_FOR_SYNTHESIS = 3


@dataclass(frozen=True, slots=True)
class RecallCandidate:
    """One row passed to the synthesizer. Shape matches what FTS5
    surfaces: a tag (``episodic`` / ``message``), an id, a session
    pointer, a turn index, and the raw text the synthesizer can quote.

    Phase 2 v0 adds optional ``bm25_score`` + ``adjusted_score`` fields
    so the recall pipeline can carry the FTS5 raw rank alongside the
    post-penalty score that determined sort order.
    """

    kind: str  # "episodic" or "message"
    id: str
    session_id: str
    turn_index: int | None
    text: str
    bm25_score: float | None = None
    adjusted_score: float | None = None


# ─── Phase 2 v0: recall_penalty decay helpers ──────────────────────

_DECAY_PER_DAY = 0.95
_PENALTY_FLOOR = 0.05


def decay_factor(age_days: float) -> float:
    """Exponential decay: 0.95^days. Reaches ~0.05 around day 60."""
    if age_days <= 0:
        return 1.0
    return _DECAY_PER_DAY ** age_days


def apply_recall_penalty(
    raw_score: float, recall_penalty: float, age_days: float,
) -> float:
    """Multiplicative score adjustment.

    Floor at 0.05 ensures penalised memories remain reachable for
    re-evaluation — the engine can't cause a cascade of "memory
    penalised → never cited → can never recover."
    """
    if recall_penalty <= 0:
        return raw_score
    effective_penalty = recall_penalty * decay_factor(age_days)
    multiplier = max(_PENALTY_FLOOR, 1.0 - effective_penalty)
    return raw_score * multiplier


def _candidates_to_prompt_block(candidates: list[RecallCandidate]) -> str:
    """Format candidates as a numbered block the LLM can reference by index."""
    lines = []
    for i, c in enumerate(candidates, 1):
        ref = f"session={c.session_id[:8]}"
        if c.turn_index is not None:
            ref += f" turn={c.turn_index}"
        lines.append(f"[{i}] ({c.kind}, {ref}) {c.text}")
    return "\n".join(lines)


_SYSTEM_PROMPT = (
    "You answer recall queries about a user's past conversations with "
    "the OpenComputer agent. You receive: a question, plus a numbered "
    "list of candidate hits from FTS5 keyword search. Your job is to "
    "produce a 1-3 sentence answer that cites WHICH candidate(s) "
    "support the answer using the bracket notation [N]. "
    "Rules:\n"
    "1. Cite ONLY candidate numbers from the input list. Never invent.\n"
    "2. If no candidate actually answers the question, say so verbatim: "
    "'No matching memory found in the candidates.'\n"
    "3. Keep it short — the user can click through the citations.\n"
    "4. Don't repeat the candidate text verbatim; synthesise."
)


def synthesize_recall(
    query: str,
    candidates: list[RecallCandidate],
    *,
    synthesize: bool | None = None,
    provider: object | None = None,
) -> str | None:
    """Return a synthesised answer for ``query`` over ``candidates``,
    or ``None`` if synthesis is skipped / fails.

    Caller decides what to show on ``None`` — typically the raw FTS5
    listing already in their hand. Synthesis NEVER substitutes
    candidates: the synthesised string is ADDITIVE context.

    Args:
      query: The user's recall query (single string).
      candidates: List of :class:`RecallCandidate`. The synthesizer
        runs only when there are at least
        :data:`_MIN_CANDIDATES_FOR_SYNTHESIS` items, otherwise returns
        ``None`` (raw is short enough already).
      synthesize: ``None`` (default) → respect the
        ``OPENCOMPUTER_RECALL_SYNTHESIS`` env var. Pass ``False`` to
        skip explicitly; ``True`` to force-on regardless of env.
      provider: Optional provider instance (BaseProvider). When None,
        we resolve via the plugin registry. Tests inject a fake.
    """
    if synthesize is False:
        return None
    if synthesize is None and os.environ.get("OPENCOMPUTER_RECALL_SYNTHESIS") == "0":
        return None
    if len(candidates) < _MIN_CANDIDATES_FOR_SYNTHESIS:
        return None

    if provider is None:
        try:
            provider = _resolve_cheap_provider()
        except Exception as exc:  # noqa: BLE001 — caller falls back to raw
            _log.debug("recall synthesizer: provider resolution failed: %s", exc)
            return None

    user_block = (
        f"Question: {query}\n\n"
        f"Candidates:\n{_candidates_to_prompt_block(candidates)}\n\n"
        f"Answer (cite numbers in brackets):"
    )

    try:
        # Run the LLM call. The provider's complete() is async on the
        # real interface; we run it via asyncio.run inside this sync
        # context. The sync entry-point matches the rest of the recall
        # tool which is sync-only.
        import asyncio

        from plugin_sdk.core import Message

        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=user_block),
        ]
        response = asyncio.run(
            provider.complete(  # type: ignore[union-attr]
                messages=messages,
                model=_SYNTH_MODEL,
                max_tokens=_SYNTH_MAX_TOKENS,
            )
        )
        text = response.message.content
    except Exception as exc:  # noqa: BLE001
        _log.debug("recall synthesizer: LLM call failed: %s", exc)
        return None

    if not text or not text.strip():
        return None

    # Citation guard: reject responses that cite indices outside the
    # candidate range. Defence-in-depth against hallucinated sources.
    if not _citations_are_in_range(text, len(candidates)):
        _log.debug(
            "recall synthesizer: citations out of range; falling back to raw. "
            "synthesis text: %r",
            text[:200],
        )
        return None

    return text.strip()


def _resolve_cheap_provider():
    """Look up the configured provider plugin and return a usable instance.

    We deliberately use the SAME provider the user already has wired
    (Anthropic, OpenAI, etc.) — just with a cheap model. That way the
    synthesizer inherits the user's auth + base URL config (Claude
    Router proxy, Anthropic-compatible endpoint, etc.) without new
    setup.
    """
    from opencomputer.agent.config import default_config
    from opencomputer.plugins.registry import registry as plugin_registry

    cfg = default_config()
    provider_cls = plugin_registry.providers.get(cfg.model.provider)
    if provider_cls is None:
        # Plugin registry not loaded yet — caller paths that hit this
        # before plugin discovery just skip synthesis. Acceptable.
        raise RuntimeError(
            f"provider {cfg.model.provider!r} not registered; skipping synthesis"
        )
    return provider_cls() if isinstance(provider_cls, type) else provider_cls


def _citations_are_in_range(text: str, n_candidates: int) -> bool:
    """Return False if the LLM cited an index ≥ n_candidates or ≤0.

    The synthesizer prompt forbids hallucinated citations; this check
    enforces it. Match ``[N]`` patterns; if every match is in
    ``1..n_candidates`` (inclusive), accept.
    """
    import re

    matches = re.findall(r"\[(\d+)\]", text)
    if not matches:
        # No citations — that's fine; the model said
        # "No matching memory found" or similar.
        return True
    for m in matches:
        try:
            idx = int(m)
        except ValueError:
            return False
        if idx < 1 or idx > n_candidates:
            return False
    return True


__all__ = [
    "RecallCandidate",
    "synthesize_recall",
]


def to_json_payload(synthesis: str | None, candidates: list[RecallCandidate]) -> str:
    """Render the recall result for the agent as a single string.

    When synthesis is present: shows the synthesis FIRST (the
    answer-shaped output), then the raw candidates for verifiability.
    When None: just the raw candidates (existing behaviour).
    """
    raw_lines = [
        "## Candidates",
        *(_candidates_to_prompt_block(candidates).split("\n")),
    ]
    if synthesis:
        return "\n".join(["## Synthesis", synthesis, "", *raw_lines])
    return "\n".join(raw_lines)
