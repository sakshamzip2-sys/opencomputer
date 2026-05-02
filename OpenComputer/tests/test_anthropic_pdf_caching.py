"""Tests for SP2+SP3 PDF Files-API caching integration."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest  # noqa: F401  -- pytest is required by the runner

PROVIDER_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "anthropic-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_anthropic_provider_pdf_caching", PROVIDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _runtime(custom: dict | None = None):
    return SimpleNamespace(custom=custom or {})


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
