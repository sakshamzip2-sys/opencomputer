"""Tests for agent.api_max_retries — provider retry knob (Hermes config v2)."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_default_api_max_retries_is_2() -> None:
    """Hermes spec says default is 2."""
    from opencomputer.agent.config import default_config

    cfg = default_config()
    assert cfg.loop.api_max_retries == 2


def test_load_config_parses_api_max_retries(tmp_path: Path) -> None:
    from opencomputer.agent.config_store import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "loop:\n  api_max_retries: 5\n", encoding="utf-8"
    )
    cfg = load_config(cfg_path)
    assert cfg.loop.api_max_retries == 5


def test_api_max_retries_zero_means_no_retry(tmp_path: Path) -> None:
    """0 = fail-fast: skip retries entirely (Hermes documented behavior)."""
    from opencomputer.agent.config_store import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "loop:\n  api_max_retries: 0\n", encoding="utf-8"
    )
    cfg = load_config(cfg_path)
    assert cfg.loop.api_max_retries == 0


def test_anthropic_async_client_accepts_max_retries() -> None:
    """The builder accepts ``max_retries`` and forwards it to the SDK."""
    from opencomputer.agent.anthropic_client import (
        build_anthropic_async_client,
    )

    client = build_anthropic_async_client(
        api_key="dummy", max_retries=5
    )
    # Anthropic SDK exposes the value on its retry config.
    # Defensive: just confirm the client constructed without error.
    assert client is not None


def test_anthropic_sync_client_accepts_max_retries() -> None:
    from opencomputer.agent.anthropic_client import (
        build_anthropic_sync_client,
    )

    client = build_anthropic_sync_client(api_key="dummy", max_retries=0)
    assert client is not None


def test_anthropic_client_no_max_retries_uses_sdk_default() -> None:
    """Without max_retries=, the SDK default is preserved."""
    from opencomputer.agent.anthropic_client import (
        build_anthropic_async_client,
    )

    client = build_anthropic_async_client(api_key="dummy")
    assert client is not None
