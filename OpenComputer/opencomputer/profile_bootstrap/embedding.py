"""BGE-small embedding helper for Layer 3 deepening.

Uses ``sentence-transformers`` (optional dep, install via
``pip install opencomputer[deepening]``) with the BGE-small-en model
(33MB, MIT license). The model loads once and is cached for the
process lifetime — first call is slow, subsequent calls are fast.

Embeddings are 384-dim float vectors suitable for Chroma's default
distance metrics.
"""
from __future__ import annotations

from typing import Any

_DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"

_cached_model: Any | None = None


class EmbeddingUnavailableError(RuntimeError):
    """Raised when sentence-transformers isn't installed."""


def _import_sentence_transformers() -> Any:
    """Indirect import so tests can patch easily."""
    import sentence_transformers  # type: ignore[import-not-found]
    return sentence_transformers


def is_embedding_available() -> bool:
    """Cheap probe — only checks that the package is importable."""
    try:
        _import_sentence_transformers()
        return True
    except ImportError:
        return False


def _get_model(model_name: str = _DEFAULT_MODEL_NAME) -> Any:
    """Load + cache the SentenceTransformer model. First call ~1s + downloads."""
    global _cached_model
    if _cached_model is not None:
        return _cached_model
    st = _import_sentence_transformers()
    _cached_model = st.SentenceTransformer(model_name)
    return _cached_model


def embed_texts(
    texts: list[str], *, model_name: str = _DEFAULT_MODEL_NAME,
) -> list[list[float]]:
    """Embed a batch of strings. Returns list of 384-dim float vectors.

    Empty input returns ``[]`` without loading the model.
    """
    if not texts:
        return []
    try:
        _import_sentence_transformers()
    except ImportError as exc:
        raise EmbeddingUnavailableError(
            "sentence-transformers not installed; "
            "install via 'pip install opencomputer[deepening]'"
        ) from exc

    model = _get_model(model_name)
    raw = model.encode(texts)
    return [list(map(float, vec)) for vec in raw]
