"""V2.B-T10 — doctor checks for deepening dependencies.

Verifies that the three optional-dep probes (ollama, sentence-transformers,
chromadb) report correctly through the existing doctor ``Check`` shape.

Mocks target the underlying probes by their module path so the lazy-imported
``is_*_available`` functions are intercepted before they hit the filesystem
or attempt to import optional packages.
"""

from __future__ import annotations

from unittest.mock import patch

from opencomputer.doctor import (
    check_chroma_available,
    check_embedding_available,
    check_ollama_available,
)


def test_check_ollama_available_pass_when_installed() -> None:
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=True,
    ):
        result = check_ollama_available()
    assert result.status == "pass"
    assert result.name == "ollama"


def test_check_ollama_available_fail_when_missing() -> None:
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=False,
    ):
        result = check_ollama_available()
    assert result.status == "fail"
    assert "ollama" in result.detail.lower()
    assert "brew install ollama" in result.detail


def test_check_embedding_available_pass() -> None:
    with patch(
        "opencomputer.profile_bootstrap.embedding.is_embedding_available",
        return_value=True,
    ):
        result = check_embedding_available()
    assert result.status == "pass"
    assert result.name == "sentence-transformers"


def test_check_chroma_available_pass() -> None:
    with patch(
        "opencomputer.profile_bootstrap.vector_store.is_chroma_available",
        return_value=True,
    ):
        result = check_chroma_available()
    assert result.status == "pass"
    assert result.name == "chromadb"
