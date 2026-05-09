"""B1: memory-mem0 graceful skip when another provider already registered."""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

EXT_DIR = Path(__file__).resolve().parent.parent / "extensions" / "memory-mem0"


def _load_plugin_module():
    """Load extensions/memory-mem0/plugin.py by file path (matches loader)."""
    name = "_test_memory_mem0_plugin"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, EXT_DIR / "plugin.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_register_logs_warning_and_skips_when_provider_already_registered(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When register_memory_provider raises ValueError (collision),
    plugin must log a WARNING and return — NOT propagate the exception.
    """
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "default")
    plugin = _load_plugin_module()

    api = MagicMock()
    api.register_memory_provider.side_effect = ValueError(
        "a memory provider is already registered: 'memory-honcho:self-hosted'"
    )

    with caplog.at_level(logging.WARNING, logger="memory-mem0"):
        plugin.register(api)  # MUST NOT raise

    msgs = [r.getMessage() for r in caplog.records if r.name == "memory-mem0"]
    assert any("already" in m.lower() for m in msgs), msgs
    api.register_memory_provider.assert_called_once()
