"""Tests for the memory-doctor observability rows added 2026-05-10.

Closes the gap that hid the silent BM25-only degradation when the
active provider doesn't support embeddings (e.g. Anthropic without
VOYAGE_API_KEY). The user's audit found no surface to detect this —
``oc memory doctor`` now reports a ``bm25-only`` row with the actual
provider error message, so the fix hint surfaces in the user's terminal
instead of buried in DEBUG logs.

Tests cover:

1. ``_doctor_active_memory_row`` — disabled by default, flips to
   "enabled" when ``memory.active_memory_enabled=true``.
2. ``_doctor_vector_retrieval_row`` — three states (disabled / active /
   bm25-only) driven by config flag and provider probe outcome.
3. ``oc memory doctor`` end-to-end via Typer CliRunner — both new rows
   appear in the table output.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

# ─── _doctor_active_memory_row ───────────────────────────────────────


def test_active_memory_row_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.agent.config import Config
    from opencomputer.cli_memory import _doctor_active_memory_row

    with patch("opencomputer.cli_memory.load_config", return_value=Config()):
        status, detail = _doctor_active_memory_row()
    assert status == "disabled"
    assert "active_memory_enabled" in detail


def test_active_memory_row_enabled_shows_top_n(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace as _dc_replace

    from opencomputer.agent.config import Config
    from opencomputer.cli_memory import _doctor_active_memory_row

    cfg = Config()
    new_mem = _dc_replace(
        cfg.memory, active_memory_enabled=True, active_memory_top_n=5
    )
    new_cfg = _dc_replace(cfg, memory=new_mem)

    with patch("opencomputer.cli_memory.load_config", return_value=new_cfg):
        status, detail = _doctor_active_memory_row()
    assert status == "enabled"
    assert "top_n=5" in detail


def test_active_memory_row_handles_config_error() -> None:
    from opencomputer.cli_memory import _doctor_active_memory_row

    def _boom():
        raise RuntimeError("simulated config error")

    with patch("opencomputer.cli_memory.load_config", side_effect=_boom):
        status, detail = _doctor_active_memory_row()
    assert status == "disabled"
    assert "config read error" in detail


# ─── _doctor_vector_retrieval_row ────────────────────────────────────


def test_vector_retrieval_row_disabled_when_config_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace as _dc_replace

    from opencomputer.agent.config import Config
    from opencomputer.cli_memory import _doctor_vector_retrieval_row

    cfg = Config()
    new_mem = _dc_replace(cfg.memory, memory_md_retrieval_enabled=False)
    new_cfg = _dc_replace(cfg, memory=new_mem)

    with patch("opencomputer.cli_memory.load_config", return_value=new_cfg):
        status, detail = _doctor_vector_retrieval_row()

    assert status == "disabled"
    assert "memory_md_retrieval_enabled" in detail


def test_vector_retrieval_row_active_when_provider_supports_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider exposes embed() and the empty-list probe succeeds → active."""
    from unittest.mock import AsyncMock, MagicMock

    from opencomputer.cli_memory import _doctor_vector_retrieval_row
    from plugin_sdk.embeddings import EmbeddingBatch

    mock_provider = MagicMock()
    mock_provider.embed = AsyncMock(
        return_value=EmbeddingBatch(
            vectors=[],
            dimensionality=1024,
            model_id="test",
            cost_estimate_usd=0.0,
            prompt_tokens=0,
        )
    )

    with patch("opencomputer.cli._resolve_provider", return_value=mock_provider):
        status, detail = _doctor_vector_retrieval_row()
    assert status == "active"
    assert "hybrid" in detail.lower()


def test_vector_retrieval_row_bm25_only_when_embed_unsupported() -> None:
    """Provider raises EmbeddingsUnsupportedError → bm25-only with the actual hint."""
    from unittest.mock import AsyncMock, MagicMock

    from opencomputer.cli_memory import _doctor_vector_retrieval_row
    from plugin_sdk.embeddings import EmbeddingsUnsupportedError

    mock_provider = MagicMock()
    mock_provider.embed = AsyncMock(
        side_effect=EmbeddingsUnsupportedError(
            "anthropic provider requires VOYAGE_API_KEY for embeddings"
        )
    )

    with patch("opencomputer.cli._resolve_provider", return_value=mock_provider):
        status, detail = _doctor_vector_retrieval_row()
    assert status == "bm25-only"
    # The actual provider error message must surface so the user gets
    # the actionable hint (set VOYAGE_API_KEY) directly.
    assert "VOYAGE_API_KEY" in detail


def test_vector_retrieval_row_bm25_only_when_no_embed_method() -> None:
    """Provider has no embed() attribute at all → bm25-only with hint."""
    from unittest.mock import MagicMock

    from opencomputer.cli_memory import _doctor_vector_retrieval_row

    # Use spec=[] to make hasattr(provider, 'embed') return False
    mock_provider = MagicMock(spec=[])

    with patch("opencomputer.cli._resolve_provider", return_value=mock_provider):
        status, detail = _doctor_vector_retrieval_row()
    assert status == "bm25-only"
    assert "embed" in detail.lower()


def test_vector_retrieval_row_bm25_only_when_provider_unresolvable() -> None:
    """Provider plugin not registered → bm25-only with helpful message."""
    from opencomputer.cli_memory import _doctor_vector_retrieval_row

    def _boom(_provider_name):
        raise RuntimeError("provider 'mythical' not registered")

    with patch("opencomputer.cli._resolve_provider", side_effect=_boom):
        status, detail = _doctor_vector_retrieval_row()
    assert status == "bm25-only"
    assert "not resolvable" in detail


# ─── End-to-end ──────────────────────────────────────────────────────


def test_memory_doctor_table_includes_new_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`oc memory doctor` output table contains both new rows."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from typer.testing import CliRunner

    from opencomputer.cli import app

    r = CliRunner().invoke(app, ["memory", "doctor"])
    assert r.exit_code == 0, r.output
    flat = " ".join(r.output.split())
    assert "active_memory" in flat, f"missing active_memory row:\n{r.output}"
    assert "vector_retrieval" in flat, (
        f"missing vector_retrieval row:\n{r.output}"
    )
