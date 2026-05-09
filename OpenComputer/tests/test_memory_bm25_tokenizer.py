"""Tokenizer unit tests for BM25Index._tokenize.

Covers v1 tokenizer contract: lowercase, ascii-alphanumeric word split, no stopwords,
no stemming.  These are the tokens that go into the BM25 corpus.
"""

from opencomputer.agent.memory_index import BM25Index


def test_tokenize_basic_lowercase_alpha() -> None:
    assert BM25Index._tokenize("Hello World") == ["hello", "world"]


def test_tokenize_strips_punctuation() -> None:
    assert BM25Index._tokenize("Hello, World!") == ["hello", "world"]


def test_tokenize_keeps_digits() -> None:
    assert BM25Index._tokenize("v1.1 plan-3") == ["v1", "1", "plan", "3"]


def test_tokenize_empty_string() -> None:
    assert BM25Index._tokenize("") == []


def test_tokenize_only_punctuation() -> None:
    assert BM25Index._tokenize("!!! ??? ...") == []


def test_tokenize_unicode_dropped_from_ascii_class() -> None:
    # The v1 tokenizer is intentionally ascii-only; non-ascii letters drop.
    # If real-use shows missed hits we can revisit; for now document the contract.
    assert BM25Index._tokenize("café résumé") == ["caf", "r", "sum"]


def test_tokenize_mixed_case_normalized() -> None:
    assert BM25Index._tokenize("PostgreSQL is GREAT") == ["postgresql", "is", "great"]


def test_tokenize_emoji_dropped() -> None:
    assert BM25Index._tokenize("ship it 🚀 today") == ["ship", "it", "today"]
