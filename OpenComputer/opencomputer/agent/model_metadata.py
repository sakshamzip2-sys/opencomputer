"""Model metadata registry — context length + cost lookup (G.32 / Tier 4).

A small in-memory registry that answers two questions about any model
id without hitting an external pricing API:

* ``context_length(model_id)`` → max prompt+completion tokens.
* ``cost_per_million(model_id)`` → ``(input_usd_per_million,
  output_usd_per_million)`` tuple, or ``None`` if unknown.

The registry ships with a curated default catalog covering the models
OC users actually run today (Anthropic + OpenAI). New entries can be
contributed at runtime by plugins via ``register_model(...)`` so a
third-party provider plugin can teach core about its own models'
metadata without forking the catalog.

Why this lives in core (not the provider plugin):

* The cost-guard module (G.8) and CompactionEngine both want to read
  context-length / cost without instantiating the provider plugin.
  Putting the table here keeps those callers cheap.
* Provider plugins still own runtime decisions (auth, transport,
  schema mapping). They just contribute metadata to the shared
  registry on ``register()``.

The registry is process-local. There's no persistence — each fresh
agent process re-builds the table from the curated defaults plus
whatever plugins register. Mirrors how Hermes maintains its
``catalog`` map at ``sources/hermes-agent-2026.4.23/agent/catalog.py``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger("opencomputer.agent.model_metadata")


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    """Per-model metadata. All numeric fields are nullable so callers
    can distinguish "not declared" from "declared as 0"."""

    model_id: str
    context_length: int | None = None
    """Max prompt+completion tokens. ``None`` means unknown."""

    input_usd_per_million: float | None = None
    """USD cost per 1,000,000 input tokens. ``None`` means unknown."""

    output_usd_per_million: float | None = None
    """USD cost per 1,000,000 output tokens. ``None`` means unknown."""


# Curated defaults. Numbers reflect the documented context-length /
# pricing pages for each model family as of 2026-04. Plugins are
# encouraged to override entries via register_model() rather than
# editing this dict in place.
_DEFAULT_CATALOG: dict[str, ModelMetadata] = {
    # Anthropic — Claude family
    "claude-opus-4-7": ModelMetadata(
        model_id="claude-opus-4-7",
        context_length=200_000,
        input_usd_per_million=15.00,
        output_usd_per_million=75.00,
    ),
    "claude-sonnet-4-6": ModelMetadata(
        model_id="claude-sonnet-4-6",
        context_length=200_000,
        input_usd_per_million=3.00,
        output_usd_per_million=15.00,
    ),
    "claude-haiku-4-5-20251001": ModelMetadata(
        model_id="claude-haiku-4-5-20251001",
        context_length=200_000,
        input_usd_per_million=0.80,
        output_usd_per_million=4.00,
    ),
    # OpenAI — GPT-5 + reasoning
    "gpt-5.4": ModelMetadata(
        model_id="gpt-5.4",
        context_length=128_000,
        input_usd_per_million=10.00,
        output_usd_per_million=40.00,
    ),
    "gpt-4o": ModelMetadata(
        model_id="gpt-4o",
        context_length=128_000,
        input_usd_per_million=2.50,
        output_usd_per_million=10.00,
    ),
    "o1": ModelMetadata(
        model_id="o1",
        context_length=200_000,
        input_usd_per_million=15.00,
        output_usd_per_million=60.00,
    ),
    "o3": ModelMetadata(
        model_id="o3",
        context_length=200_000,
        input_usd_per_million=15.00,
        output_usd_per_million=60.00,
    ),
    "o4-mini": ModelMetadata(
        model_id="o4-mini",
        context_length=200_000,
        input_usd_per_million=1.10,
        output_usd_per_million=4.40,
    ),
}


_lock = threading.Lock()
_registry: dict[str, ModelMetadata] = dict(_DEFAULT_CATALOG)


def get_metadata(model_id: str) -> ModelMetadata | None:
    """Return the metadata entry for ``model_id``, or ``None`` if absent.

    Lookup is exact-match on ``model_id``. Callers that want
    prefix-based lookup (so ``claude-opus-4-7-20250101`` matches
    ``claude-opus-4-7``) should walk the prefixes themselves —
    different callers want different prefix-stripping rules and we
    don't want to bake one in here.
    """
    with _lock:
        return _registry.get(model_id)


def context_length(model_id: str) -> int | None:
    """Convenience: return the context length for ``model_id``, or ``None``."""
    meta = get_metadata(model_id)
    return meta.context_length if meta else None


def cost_per_million(model_id: str) -> tuple[float, float] | None:
    """Convenience: return ``(input_cost, output_cost)`` per 1M tokens, or ``None``.

    Returns ``None`` only when EITHER the entry is missing OR neither
    cost field is populated. A partial entry (only input cost known)
    surfaces as ``(input, 0.0)`` so callers don't crash on the unpack.
    """
    meta = get_metadata(model_id)
    if meta is None:
        return None
    if meta.input_usd_per_million is None and meta.output_usd_per_million is None:
        return None
    return (
        meta.input_usd_per_million or 0.0,
        meta.output_usd_per_million or 0.0,
    )


def register_model(meta: ModelMetadata, *, replace: bool = False) -> None:
    """Add (or replace) a metadata entry.

    Plugins call this from ``register(api)`` to teach core about their
    models. ``replace=False`` (default) silently keeps the existing
    entry on collision so the curated defaults stay authoritative
    unless a plugin explicitly opts in to overriding them.
    """
    with _lock:
        existing = _registry.get(meta.model_id)
        if existing is not None and not replace:
            logger.debug(
                "model_metadata.register_model: %r already present; "
                "skipping (pass replace=True to override)",
                meta.model_id,
            )
            return
        _registry[meta.model_id] = meta


def list_models() -> list[ModelMetadata]:
    """Return all registered metadata entries, sorted by model_id."""
    with _lock:
        return sorted(_registry.values(), key=lambda m: m.model_id)


def reset_to_defaults() -> None:
    """Reset the registry to the curated catalog. Test-only helper.

    Production code never calls this — it would discard third-party
    plugin contributions.
    """
    with _lock:
        _registry.clear()
        _registry.update(_DEFAULT_CATALOG)


__all__ = [
    "ModelMetadata",
    "context_length",
    "cost_per_million",
    "get_metadata",
    "list_models",
    "register_model",
    "reset_to_defaults",
]
