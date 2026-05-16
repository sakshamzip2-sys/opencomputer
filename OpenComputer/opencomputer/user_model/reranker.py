"""Context-aware reranker for the prompt's user-facts block (M3).

``build_user_facts`` historically sorted facts by ``(kind, confidence)``
only — static, and blind to the current conversation. This module
scores each candidate fact by a weighted blend of kind priority,
confidence, recency, and BM25 relevance to the live session, so the
facts injected into the prompt reflect what *this* chat is about.

Pure-Python — no model call, no external dependency. The reranker
output is cached per session by the caller. See
``docs/awareness/reranker.md``.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugin_sdk.user_model import Node


# ─── tokenisation ─────────────────────────────────────────────────────

#: Minimal stopword list — common words carry no discriminative signal
#: between a fact and a session and would otherwise inflate BM25.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "i", "in", "is", "it", "its", "of", "on", "or",
    "that", "the", "this", "to", "was", "were", "will", "with", "you",
    "your", "me", "my", "we", "our",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords.

    Deliberately lossy — URLs and snake_case identifiers fragment into
    their alphanumeric runs. Good enough for short node values vs short
    message contexts; see ``docs/awareness/reranker.md`` for the v1
    tokenisation caveats.
    """
    return [
        tok for tok in _TOKEN_RE.findall(text.lower())
        if tok not in _STOPWORDS
    ]


# ─── BM25 ─────────────────────────────────────────────────────────────

_BM25_K1 = 1.5
_BM25_B = 0.75


def bm25_scores(query: str, documents: Sequence[str]) -> list[float]:
    """Return the Okapi BM25 score of each document against ``query``.

    Standard BM25 (k1=1.5, b=0.75). The query is the session's recent
    messages; each document is a candidate node value. Scores are raw
    (not normalised) — :class:`UserFactsReranker` normalises across the
    candidate set. An empty query, or a document sharing no query term,
    scores ``0.0``.
    """
    docs_tokens = [_tokenize(d) for d in documents]
    query_tokens = set(_tokenize(query))
    if not query_tokens or not docs_tokens:
        return [0.0] * len(documents)

    n_docs = len(docs_tokens)
    doc_lengths = [len(d) for d in docs_tokens]
    avgdl = sum(doc_lengths) / n_docs

    # Document frequency per query term.
    df: dict[str, int] = {
        term: sum(1 for d in docs_tokens if term in d)
        for term in query_tokens
    }

    scores: list[float] = []
    for tokens, length in zip(docs_tokens, doc_lengths, strict=True):
        if not tokens:
            scores.append(0.0)
            continue
        tf: dict[str, int] = {}
        for t in tokens:
            if t in query_tokens:
                tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for term, freq in tf.items():
            n_t = df.get(term, 0)
            idf = math.log(((n_docs - n_t + 0.5) / (n_t + 0.5)) + 1.0)
            denom = freq + _BM25_K1 * (
                1.0 - _BM25_B + _BM25_B * (length / avgdl if avgdl else 0.0)
            )
            if denom:
                score += idf * (freq * (_BM25_K1 + 1.0)) / denom
        scores.append(max(0.0, score))
    return scores


# ─── reranker ─────────────────────────────────────────────────────────

#: Per-kind priority, normalised to (0, 1]. Identity facts are the most
#: load-bearing; relationship facts (not prompt-injected today) rank
#: lowest. Same ordering ``build_user_facts`` used, on a numeric scale.
_KIND_PRIORITY: dict[str, float] = {
    "identity": 1.0,
    "goal": 0.8,
    "preference": 0.6,
    "attribute": 0.4,
    "relationship": 0.2,
}

#: Half-life (days) for the recency term — a fact last asserted this
#: long ago contributes half of its full recency weight.
_RECENCY_HALF_LIFE_DAYS = 30.0
_SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True, slots=True)
class RerankWeights:
    """Blend weights for :class:`UserFactsReranker`.

    Defaults sum to 1.0 so the composite score stays in ``[0, 1]``.
    """

    kind: float = 0.40
    confidence: float = 0.20
    recency: float = 0.20
    bm25: float = 0.20


@dataclass(frozen=True, slots=True)
class SessionContext:
    """The live-session signal the reranker scores against.

    ``recent_messages`` are the last few user messages. An empty tuple
    puts the reranker in *context-free mode* (BM25 term skipped) — the
    fallback for cron / gateway runs with no conversation.
    """

    recent_messages: tuple[str, ...] = ()
    persona_tag: str | None = None
    foreground_app: str | None = None

    @property
    def is_context_free(self) -> bool:
        """True when there is no conversational signal to rank against."""
        return not self.recent_messages


@dataclass(frozen=True, slots=True)
class ScoredFact:
    """One reranked fact: the node, its composite score, and the
    per-term breakdown (consumed by ``oc awareness explain --session``).
    """

    node: Node
    score: float
    breakdown: dict[str, float]


class UserFactsReranker:
    """Rank user-model facts by relevance to the current session.

    ``score = w_kind·kind + w_conf·confidence + w_recency·recency
    + w_bm25·bm25`` — a weighted blend in ``[0, 1]``. In context-free
    mode the BM25 term is dropped and the remaining weights are
    renormalised so a cron run still gets a sensible static ranking.
    """

    def __init__(self, weights: RerankWeights | None = None) -> None:
        self.weights = weights if weights is not None else RerankWeights()

    def score(
        self,
        nodes: Sequence[Node],
        context: SessionContext,
        *,
        now: float | None = None,
    ) -> list[ScoredFact]:
        """Return ``nodes`` scored and sorted, highest score first."""
        reference = time.time() if now is None else float(now)
        w = self.weights

        # BM25 across the whole candidate set, then max-normalise to
        # [0, 1] so it composes with the other [0, 1] terms.
        if context.is_context_free:
            bm25_norm = [0.0] * len(nodes)
            active = w.kind + w.confidence + w.recency
            wk = w.kind / active if active else 0.0
            wc = w.confidence / active if active else 0.0
            wr = w.recency / active if active else 0.0
            wb = 0.0
        else:
            query = " ".join(context.recent_messages)
            raw = bm25_scores(query, [n.value for n in nodes])
            top = max(raw, default=0.0)
            bm25_norm = [(s / top if top > 0 else 0.0) for s in raw]
            wk, wc, wr, wb = w.kind, w.confidence, w.recency, w.bm25

        scored: list[ScoredFact] = []
        for node, bm25 in zip(nodes, bm25_norm, strict=True):
            kind_term = _KIND_PRIORITY.get(node.kind, 0.1)
            conf_term = max(0.0, min(1.0, node.confidence))
            age_days = max(
                0.0, (reference - node.last_seen_at) / _SECONDS_PER_DAY
            )
            recency_term = 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)
            composite = (
                wk * kind_term
                + wc * conf_term
                + wr * recency_term
                + wb * bm25
            )
            scored.append(ScoredFact(
                node=node,
                score=composite,
                breakdown={
                    "kind": kind_term,
                    "confidence": conf_term,
                    "recency": recency_term,
                    "bm25": bm25,
                },
            ))
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored


__all__ = [
    "UserFactsReranker",
    "RerankWeights",
    "SessionContext",
    "ScoredFact",
    "bm25_scores",
]
