"""M3 T3.2 — tests for the pure-Python BM25 used by the reranker.

BM25 scores each candidate node value against the current session's
recent messages, so facts relevant to *this* conversation rise.
"""
from __future__ import annotations


def test_tokenize_lowercases_and_splits() -> None:
    """Tokeniser lowercases and splits on non-alphanumerics."""
    from opencomputer.user_model.reranker import _tokenize

    assert _tokenize("Prefers Wednesday 14:00") == [
        "prefers", "wednesday", "14", "00",
    ]


def test_tokenize_drops_stopwords() -> None:
    """Common stopwords are dropped so they don't dominate the match."""
    from opencomputer.user_model.reranker import _tokenize

    toks = _tokenize("the user is working on the project")
    assert "the" not in toks
    assert "is" not in toks
    assert "user" in toks and "project" in toks


def test_bm25_empty_query_is_all_zero() -> None:
    """No query → no signal → every document scores 0."""
    from opencomputer.user_model.reranker import bm25_scores

    assert bm25_scores("", ["doc one", "doc two"]) == [0.0, 0.0]


def test_bm25_returns_one_score_per_document() -> None:
    """Output length matches the document count."""
    from opencomputer.user_model.reranker import bm25_scores

    scores = bm25_scores("python", ["a", "b", "c"])
    assert len(scores) == 3


def test_bm25_matching_document_scores_higher() -> None:
    """A document sharing query terms outscores one that doesn't."""
    from opencomputer.user_model.reranker import bm25_scores

    scores = bm25_scores(
        "learning python", ["uses python daily", "enjoys hiking trips"]
    )
    assert scores[0] > scores[1]


def test_bm25_no_shared_terms_scores_zero() -> None:
    """A document with no query term overlap scores exactly 0."""
    from opencomputer.user_model.reranker import bm25_scores

    scores = bm25_scores("elephant", ["uses python", "learns rust"])
    assert scores == [0.0, 0.0]


def test_bm25_handles_empty_documents() -> None:
    """An empty document list / empty docs do not crash."""
    from opencomputer.user_model.reranker import bm25_scores

    assert bm25_scores("python", []) == []
    assert bm25_scores("python", ["", "  "]) == [0.0, 0.0]
