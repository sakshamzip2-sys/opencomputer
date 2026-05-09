"""Embedding-API contract for OpenComputer providers (v1.1 plan-3 M6.6).

Providers that natively expose an embeddings endpoint (OpenAI's
``text-embedding-3-*``, Voyage AI, Cohere, etc.) implement
:meth:`BaseProvider.embed` and return an :class:`EmbeddingBatch`.

Callers (notably the M6.2 vector index) are tolerant of providers that
do NOT support embeddings — they catch :class:`EmbeddingsUnsupportedError`
and fall back to BM25-only retrieval.

Design notes:
- The contract is text-list-in, vectors-list-out plus minimal metadata
  (dimensionality, model_id, cost_estimate_usd).  Adding a richer shape
  (token counts, model variants per text, etc.) is a v2 concern.
- ``MAX_BATCH_SIZE = 100`` matches OpenAI's typical chunking guidance
  and Voyage's per-request limit.  Implementations should chunk
  internally if a single call exceeds the cap rather than rejecting.
- ``cost_estimate_usd`` is a best-effort scalar.  Providers that don't
  publish per-token pricing return 0.0 (the field is informational, not
  load-bearing for routing decisions).
"""

from __future__ import annotations

from dataclasses import dataclass, field


class EmbeddingsUnsupportedError(Exception):
    """Raised by providers that do not implement embeddings.

    The vector index catches this and falls back to BM25-only retrieval,
    logging a one-time warning at session start.
    """


# Maximum number of strings any provider should accept in a single
# ``embed()`` call.  Implementations that natively chunk smaller batches
# may impose a tighter cap; this is the SDK-side ceiling.
MAX_BATCH_SIZE: int = 100


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """One ``embed()`` response.

    Fields:
        vectors:           Parallel to the input list of texts.  Each
                           inner list has length ``dimensionality``.
        dimensionality:    Vector size in floats.  Constant across all
                           rows of one batch (a provider that returns
                           rows of differing dimensionality is a bug).
        model_id:          Concrete model id used (e.g.
                           ``"text-embedding-3-small"``).  Distinct from
                           the provider name; cache-busting consumers
                           record this so a model swap invalidates
                           cached embeddings.
        cost_estimate_usd: Best-effort cost of this batch.  ``0.0`` if
                           the provider does not publish per-token
                           pricing or did not return token counts.
        prompt_tokens:     Optional input token count.  Some providers
                           return this; ``None`` when not available.
    """

    vectors: list[list[float]]
    dimensionality: int
    model_id: str
    cost_estimate_usd: float = 0.0
    prompt_tokens: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.vectors:
            actual_dim = len(self.vectors[0])
            if actual_dim != self.dimensionality:
                raise ValueError(
                    f"EmbeddingBatch dimensionality={self.dimensionality} "
                    f"but first vector has length {actual_dim}"
                )
            for i, vec in enumerate(self.vectors):
                if len(vec) != self.dimensionality:
                    raise ValueError(
                        f"EmbeddingBatch vector at index {i} has length "
                        f"{len(vec)} != declared dimensionality "
                        f"{self.dimensionality}"
                    )
        if self.cost_estimate_usd < 0:
            raise ValueError(
                f"cost_estimate_usd cannot be negative; got "
                f"{self.cost_estimate_usd}"
            )


__all__ = [
    "EmbeddingBatch",
    "EmbeddingsUnsupportedError",
    "MAX_BATCH_SIZE",
]
