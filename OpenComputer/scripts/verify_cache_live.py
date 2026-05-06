"""Live end-to-end verification of the prompt-caching split-system fix.

Sends 2 turns to the real Anthropic API and asserts that turn 2's
``cache_read_input_tokens`` is non-zero — proving the cache prefix
matched across turns even when the per-turn injection differed.

Usage:
    export ANTHROPIC_API_KEY=...
    cd /Users/saksham/.config/superpowers/worktrees/opencomputer/prompt-caching/OpenComputer
    .venv/bin/python scripts/verify_cache_live.py

Cost: 2 small Opus calls (~10k input tokens cached on turn 2). Real
billed usage but tiny — under 5 cents at current Opus pricing.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_provider():
    spec = importlib.util.spec_from_file_location(
        "_anth_live_verify", REPO / "extensions/anthropic-provider/provider.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_anth_live_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


async def _call_with_retry(coro_factory, *, max_attempts: int = 5):
    """Call ``coro_factory()`` and retry on Anthropic 429s with backoff.

    Concurrent Claude Code sessions sharing the same API key commonly
    trigger transient 429s on the very first call. Backoff: 5s, 15s,
    45s, 90s.
    """
    import asyncio as _asyncio

    delays = [5, 15, 45, 90]
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            is_429 = name in ("RateLimitError", "APIStatusError") or "429" in str(exc)
            if not is_429 or attempt == max_attempts - 1:
                raise
            wait = delays[min(attempt, len(delays) - 1)]
            print(f"  [429 received — backing off {wait}s, attempt {attempt + 2}/{max_attempts}]")
            await _asyncio.sleep(wait)
            last_exc = exc
    if last_exc is not None:
        raise last_exc


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set in environment.", file=sys.stderr)
        return 1

    mod = _load_provider()
    provider = mod.AnthropicProvider()

    from plugin_sdk import Message

    # Big base prompt to clear Opus's 4096-token cache threshold.
    # Bumped large enough to definitively pass the threshold (Opus
    # min_cache_tokens=4096, ~16KB at 4 chars/token).
    big_base = (
        "You are a careful assistant who answers briefly. " * 30
        + "Reference data: " + "x" * 18000
    )

    print(f"Base prompt: {len(big_base)} chars (~{len(big_base) // 4} tokens)")
    print()

    # Allow overriding the model via env var. Default to Sonnet 4.6 because
    # Opus rate limits are tight when Claude Code shares the same key.
    # Sonnet 4.6 cache threshold is 2048 tokens — our 18KB base clears it.
    model = os.environ.get("OC_VERIFY_MODEL", "claude-sonnet-4-6")
    print(f"Model: {model}")
    print()

    # Turn 1: prime the cache.
    print("Turn 1: priming the cache...")
    resp1 = await _call_with_retry(lambda: provider.complete(
        model=model,
        messages=[Message(role="user", content="Say 'hi' once.")],
        base_system=big_base,
        injected_system="",
        session_id="verify-cache-live",
        max_tokens=20,
    ))
    u1 = resp1.usage
    cache_write_1 = getattr(u1, "cache_creation_input_tokens", 0) or 0
    cache_read_1 = getattr(u1, "cache_read_input_tokens", 0) or 0
    print(f"  input={u1.input_tokens}  output={u1.output_tokens}")
    print(f"  cache_write={cache_write_1}  cache_read={cache_read_1}")
    print(f"  reply: {resp1.message.content!r}")
    print()

    # Turn 2: same base, DIFFERENT injection (the volatile content that
    # used to bust the cache pre-fix).
    print("Turn 2: same base, NEW injection — verifying cache hit...")
    resp2 = await _call_with_retry(lambda: provider.complete(
        model=model,
        messages=[
            Message(role="user", content="Say 'hi' once."),
            Message(role="assistant", content=resp1.message.content),
            Message(role="user", content="Now say 'bye' once."),
        ],
        base_system=big_base,
        injected_system="Per-turn reminder that varies between calls.",
        session_id="verify-cache-live",
        max_tokens=20,
    ))
    u2 = resp2.usage
    cache_write_2 = getattr(u2, "cache_creation_input_tokens", 0) or 0
    cache_read_2 = getattr(u2, "cache_read_input_tokens", 0) or 0
    print(f"  input={u2.input_tokens}  output={u2.output_tokens}")
    print(f"  cache_write={cache_write_2}  cache_read={cache_read_2}")
    print(f"  reply: {resp2.message.content!r}")
    print()

    if cache_read_2 > 0:
        print(f"✅ CACHE HIT confirmed on turn 2: {cache_read_2} tokens read from cache.")
        print("   The split-system fix is working live — the per-turn injection")
        print("   change did NOT bust the cached prefix.")
        return 0
    else:
        print("❌ CACHE MISS on turn 2: cache_read_input_tokens=0")
        print("   The base prompt was not retrieved from cache. Check that the")
        print("   model + base_system are large enough (Opus needs ≥4096 tokens).")
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
