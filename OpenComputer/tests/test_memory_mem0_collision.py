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
    # MEM0_API_KEY must be set for register() to reach the register-provider
    # call; without it the plugin short-circuits silently (M1.B1 follow-up).
    monkeypatch.setenv("MEM0_API_KEY", "fake-test-key")
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


def test_register_silently_skips_when_no_credentials(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When neither MEM0_API_KEY nor MEM0_BASE_URL is set, the plugin must
    return without calling register_memory_provider AND without logging
    any warning. Closes the residual warning-spam Saksham flagged when
    his .env had no MEM0_API_KEY (2026-05-10)."""
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    monkeypatch.delenv("MEM0_BASE_URL", raising=False)
    plugin = _load_plugin_module()

    api = MagicMock()

    with caplog.at_level(logging.WARNING, logger="memory-mem0"):
        plugin.register(api)

    api.register_memory_provider.assert_not_called()
    msgs = [r.getMessage() for r in caplog.records if r.name == "memory-mem0"]
    assert not msgs, f"expected silent skip, got log lines: {msgs}"
