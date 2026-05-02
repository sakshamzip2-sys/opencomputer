"""Tests for SP2+SP3 PDF Files-API caching integration."""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

PROVIDER_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "anthropic-provider" / "provider.py"
)
FILES_CACHE_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "anthropic-provider" / "files_cache.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_anthropic_provider_pdf_caching", PROVIDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _load_files_cache_module():
    """Load files_cache.py under the SAME synthetic name the provider uses.

    Provider (under test) lazy-loads ``extensions_anthropic_provider_files_cache``.
    Tests put bytes through ``hash_file_bytes`` so the cache key matches —
    we therefore reuse the same module object the provider will see, which
    means returning the synthetic-name module if already loaded.
    """
    name = "extensions_anthropic_provider_files_cache"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, FILES_CACHE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _runtime(custom: dict | None = None):
    return SimpleNamespace(custom=custom or {})


def _make_minimal_pdf() -> bytes:
    """Smallest valid-looking PDF for tests (mirrors test_anthropic_provider_pdf)."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        b"<< /Type /Page >>\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


# --- _resolve_anthropic_files_cache_enabled -------------------------------


def test_resolve_files_cache_enabled_default_false(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", raising=False)
    module = _load_provider_module()
    assert module._resolve_anthropic_files_cache_enabled(_runtime()) is False
    assert module._resolve_anthropic_files_cache_enabled(None) is False


def test_resolve_files_cache_enabled_env(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", "1")
    module = _load_provider_module()
    assert module._resolve_anthropic_files_cache_enabled(_runtime()) is True
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", "true")
    assert module._resolve_anthropic_files_cache_enabled(_runtime()) is True
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", "0")
    assert module._resolve_anthropic_files_cache_enabled(_runtime()) is False


def test_resolve_files_cache_enabled_runtime(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", raising=False)
    module = _load_provider_module()
    assert module._resolve_anthropic_files_cache_enabled(
        _runtime({"anthropic_files_cache": True})
    ) is True
    assert module._resolve_anthropic_files_cache_enabled(
        _runtime({"anthropic_files_cache": False})
    ) is False


def test_resolve_files_cache_enabled_runtime_overrides_env(monkeypatch):
    """Runtime flag wins over env var (per spec resolution order)."""
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", "1")
    module = _load_provider_module()
    # Runtime explicitly False overrides env True
    assert module._resolve_anthropic_files_cache_enabled(
        _runtime({"anthropic_files_cache": False})
    ) is False


# --- _build_anthropic_pdf_block (async, with optional cache+client) -------


@pytest.mark.asyncio
async def test_pdf_block_uses_base64_when_cache_or_client_none(tmp_path):
    """Default behavior preserved: no cache/client kwargs → SP2 base64 block."""
    module = _load_provider_module()
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_make_minimal_pdf())

    block = await module._build_anthropic_pdf_block(pdf)
    assert block is not None
    assert block["type"] == "document"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_pdf_block_cache_hit_uses_file_id(tmp_path):
    """Cache hit returns a file_id-based block; no upload call is made."""
    module = _load_provider_module()
    cache_module = _load_files_cache_module()

    pdf = tmp_path / "doc.pdf"
    pdf_bytes = _make_minimal_pdf()
    pdf.write_bytes(pdf_bytes)

    cache = cache_module.FilesCache(tmp_path / "cache.json")
    cache.put(
        cache_module.hash_file_bytes(pdf_bytes),
        file_id="file_cached",
        filename="doc.pdf",
        size_bytes=len(pdf_bytes),
    )

    fake_client = MagicMock()
    fake_client.upload = AsyncMock()  # should NOT be called

    block = await module._build_anthropic_pdf_block(
        pdf, cache=cache, client=fake_client
    )
    assert block == {
        "type": "document",
        "source": {"type": "file", "file_id": "file_cached"},
    }
    fake_client.upload.assert_not_called()


@pytest.mark.asyncio
async def test_pdf_block_cache_miss_uploads_then_uses_file_id(tmp_path):
    """Cache miss → upload → cache populated → file_id block returned.

    Subsequent build with the same cache + bytes hits the cache (no
    second upload), proving the put() persisted.
    """
    module = _load_provider_module()
    cache_module = _load_files_cache_module()

    pdf = tmp_path / "doc.pdf"
    pdf_bytes = _make_minimal_pdf()
    pdf.write_bytes(pdf_bytes)
    cache = cache_module.FilesCache(tmp_path / "cache.json")

    fake_metadata = MagicMock()
    fake_metadata.id = "file_uploaded_xyz"
    fake_metadata.filename = "doc.pdf"
    fake_metadata.size_bytes = len(pdf_bytes)
    fake_client = MagicMock()
    fake_client.upload = AsyncMock(return_value=fake_metadata)

    block = await module._build_anthropic_pdf_block(
        pdf, cache=cache, client=fake_client
    )
    fake_client.upload.assert_awaited_once_with(pdf)
    assert block == {
        "type": "document",
        "source": {"type": "file", "file_id": "file_uploaded_xyz"},
    }

    # Second call with the same cache + same bytes → cache hit, no upload.
    fake_client.upload.reset_mock()
    block2 = await module._build_anthropic_pdf_block(
        pdf, cache=cache, client=fake_client
    )
    assert block2["source"]["file_id"] == "file_uploaded_xyz"
    fake_client.upload.assert_not_called()


@pytest.mark.asyncio
async def test_pdf_block_upload_failure_falls_back_to_base64(tmp_path, caplog):
    """If upload raises, caller still gets a base64 block + WARNING logged."""
    module = _load_provider_module()
    cache_module = _load_files_cache_module()

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_make_minimal_pdf())
    cache = cache_module.FilesCache(tmp_path / "cache.json")

    fake_client = MagicMock()
    fake_client.upload = AsyncMock(side_effect=RuntimeError("network down"))

    with caplog.at_level(logging.WARNING):
        block = await module._build_anthropic_pdf_block(
            pdf, cache=cache, client=fake_client
        )

    assert block is not None
    assert block["type"] == "document"
    assert block["source"]["type"] == "base64"
    assert any(
        "fall" in r.message.lower() and "base64" in r.message.lower()
        for r in caplog.records
    )
