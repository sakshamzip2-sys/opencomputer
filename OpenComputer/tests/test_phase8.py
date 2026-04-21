"""Phase 8 tests: Discord adapter — chunking + manifest discovery."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_discord_adapter():
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "extensions" / "discord" / "adapter.py"
    spec = importlib.util.spec_from_file_location("discord_adapter_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["discord_adapter_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── Chunking (2000-char Discord limit) ────────────────────────


def test_chunk_2000_single_chunk_under_limit() -> None:
    mod = _load_discord_adapter()
    result = mod._chunk_2000("hello world", limit=2000)
    assert result == ["hello world"]


def test_chunk_2000_splits_on_line_boundary() -> None:
    mod = _load_discord_adapter()
    lines = "line1\n" * 1000  # 6000 chars total
    chunks = mod._chunk_2000(lines, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == lines
    # Should split at line boundaries, not mid-line
    for chunk in chunks:
        if chunk != chunks[-1]:
            # Chunks that aren't the last should end with a newline (clean boundary)
            assert chunk.endswith("\n") or len(chunk) == 2000


def test_chunk_2000_hard_splits_oversize_single_line() -> None:
    mod = _load_discord_adapter()
    # A single line longer than the limit — must hard-split
    giant = "x" * 5000
    chunks = mod._chunk_2000(giant, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == giant
    assert len(chunks) == 3  # 2000 + 2000 + 1000


def test_chunk_2000_roundtrip_preserves_content() -> None:
    mod = _load_discord_adapter()
    text = "\n".join(f"line {i}: " + ("x" * (i % 200)) for i in range(30))
    chunks = mod._chunk_2000(text, limit=2000)
    assert "".join(chunks) == text


# ─── Discord plugin manifest ───────────────────────────────────


def test_discord_plugin_manifest_discoverable() -> None:
    from opencomputer.plugins.discovery import discover

    repo_root = Path(__file__).resolve().parent.parent
    ext_dir = repo_root / "extensions"
    candidates = discover([ext_dir])
    ids = [c.manifest.id for c in candidates]
    assert "discord" in ids
    d = next(c for c in candidates if c.manifest.id == "discord")
    assert d.manifest.kind == "channel"
    assert d.manifest.entry == "plugin"


def test_discord_adapter_requires_token_on_instantiation() -> None:
    """Without a bot_token in config, the adapter should raise cleanly."""
    import pytest

    mod = _load_discord_adapter()
    with pytest.raises(KeyError):
        mod.DiscordAdapter(config={})


def test_discord_adapter_has_correct_platform_and_length() -> None:
    mod = _load_discord_adapter()
    from plugin_sdk.core import Platform

    adapter = mod.DiscordAdapter(config={"bot_token": "test-token-not-real"})
    assert adapter.platform == Platform.DISCORD
    assert adapter.max_message_length == 2000
