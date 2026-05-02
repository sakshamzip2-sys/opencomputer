"""Tests for AnthropicFilesClient persistent cache (SP2+SP3 integration)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest  # noqa: F401  -- pytest is required by the runner

CACHE_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "anthropic-provider" / "files_cache.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_test_anthropic_files_cache", CACHE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_hash_file_bytes_deterministic():
    module = _load_module()
    h1 = module.hash_file_bytes(b"hello world")
    h2 = module.hash_file_bytes(b"hello world")
    h3 = module.hash_file_bytes(b"different content")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # SHA-256 hex


def test_cache_get_returns_none_when_missing(tmp_path):
    module = _load_module()
    cache = module.FilesCache(tmp_path / "cache.json")
    assert cache.get("any_hash") is None


def test_cache_put_then_get_roundtrip(tmp_path):
    module = _load_module()
    cache = module.FilesCache(tmp_path / "cache.json")
    cache.put(
        "hash1",
        file_id="file_abc",
        filename="doc.pdf",
        size_bytes=1024,
    )
    entry = cache.get("hash1")
    assert entry is not None
    assert entry.file_id == "file_abc"
    assert entry.filename == "doc.pdf"
    assert entry.size_bytes == 1024
    assert entry.uploaded_at  # ISO timestamp populated


def test_cache_invalidate_removes_entry(tmp_path):
    module = _load_module()
    cache = module.FilesCache(tmp_path / "cache.json")
    cache.put("h", file_id="file_x", filename="f", size_bytes=1)
    assert cache.get("h") is not None
    cache.invalidate("h")
    assert cache.get("h") is None


def test_cache_load_handles_missing_file(tmp_path):
    module = _load_module()
    cache = module.FilesCache(tmp_path / "nonexistent.json")
    # Should not raise; just behave as empty
    assert cache.get("anything") is None


def test_cache_load_handles_malformed_json(tmp_path):
    module = _load_module()
    cache_file = tmp_path / "bad.json"
    cache_file.write_text("{ this is not valid json")
    cache = module.FilesCache(cache_file)
    # Should not raise; treat as empty + log warning
    assert cache.get("anything") is None


def test_cache_get_handles_malformed_entry(tmp_path):
    module = _load_module()
    cache_file = tmp_path / "weird.json"
    cache_file.write_text(json.dumps({"h": {"missing_required_fields": True}}))
    cache = module.FilesCache(cache_file)
    # Malformed entry -> log + return None, don't crash
    assert cache.get("h") is None


def test_cache_put_creates_parent_dir(tmp_path):
    module = _load_module()
    nested = tmp_path / "deep" / "nested" / "cache.json"
    cache = module.FilesCache(nested)
    cache.put("h", file_id="file_y", filename="f", size_bytes=1)
    assert nested.exists()
    assert cache.get("h") is not None
