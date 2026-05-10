"""Regression: langfuse plugin must NOT instantiate the SDK when the
configured host is unreachable.

Without the pre-flight reachability check, Langfuse(...)'s background
OTEL exporter retries the unreachable endpoint forever and spams the
user's terminal with "Connection refused" every 1-2 seconds. Saksham
hit this on 2026-05-10 right after `uv tool install --with langfuse`
made the SDK importable while his self-hosted stack at localhost:3000
wasn't running.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

LANGFUSE_PLUGIN = (
    Path(__file__).resolve().parent.parent
    / "extensions"
    / "langfuse"
    / "plugin.py"
)


def _load_plugin():
    name = "_test_langfuse_plugin"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, LANGFUSE_PLUGIN)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_is_host_reachable_returns_false_for_dead_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbound TCP port returns False — the connect() raises ConnectionRefusedError."""
    plugin = _load_plugin()
    # Port 1 is reserved + never bound on a normal system.
    assert plugin._is_host_reachable("http://127.0.0.1:1", timeout=0.3) is False


def test_is_host_reachable_returns_false_for_invalid_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _load_plugin()
    assert plugin._is_host_reachable("", timeout=0.3) is False
    assert plugin._is_host_reachable("not-a-url", timeout=0.3) is False


def test_build_client_returns_none_when_host_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline behavior: env vars + SDK both present, host dead → None."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://127.0.0.1:1")  # dead port

    plugin = _load_plugin()
    # Force the langfuse SDK import path to succeed even if not installed.
    # We never reach the constructor because the host is unreachable.
    monkeypatch.setitem(sys.modules, "langfuse", MagicMock(Langfuse=MagicMock()))

    client = plugin._build_client()
    assert client is None


def test_build_client_skips_langfuse_constructor_when_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Critical: Langfuse(...) MUST NOT be called when host is dead.
    Calling it spawns the background exporter that does the spamming."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://127.0.0.1:1")

    fake_langfuse_constructor = MagicMock()
    fake_module = MagicMock(Langfuse=fake_langfuse_constructor)
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)

    plugin = _load_plugin()
    with caplog.at_level("WARNING", logger="opencomputer.ext.langfuse"):
        plugin._build_client()

    fake_langfuse_constructor.assert_not_called()
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "not reachable" in msgs.lower(), msgs
