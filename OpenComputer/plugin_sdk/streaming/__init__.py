"""Streaming primitives for channel adapters (OpenClaw 1.A port).

Provides:
  - ``BlockChunker``: paragraph-aware streaming chunker with humanDelay.
  - ``Block``: immutable unit emitted by the chunker.
  - ``wrap_stream_callback``: helper that wraps a raw text-delta callback
    in chunker logic, so callers (gateway dispatch, channel adapters) can
    opt in by passing the wrapped callback to ``AgentLoop.run_conversation``.
"""
from __future__ import annotations

from plugin_sdk.streaming.block_chunker import (
    Block,
    BlockChunker,
    wrap_stream_callback,
)

__all__ = ["Block", "BlockChunker", "wrap_stream_callback"]
