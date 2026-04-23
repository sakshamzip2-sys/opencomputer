"""Follow-up #28 — cap prefetch output with recency truncation.

If Honcho (or any external memory provider) returns a 10 KB recall blob
on turn 1, that same 10 KB rides along on every subsequent turn — context
window bloat + prefix-cache churn. The bridge now truncates anything
larger than :data:`MAX_PREFETCH_CHARS` by keeping the *tail* (most recent
content) and prepending an ``[…earlier recall truncated…]`` marker.

Design rules:
  * Strings shorter than the cap pass through unchanged.
  * Oversize strings keep ``MAX_PREFETCH_CHARS - 40`` chars of the tail
    (leaves room for the marker + ellipsis without exceeding the budget
    by more than a small, bounded amount).
  * The truncation must fire in the success path — the cap does not
    apply to ``None`` returned from exception paths or the cron guard.
  * The existing cron/flush guard is untouched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from opencomputer.agent.memory_bridge import MAX_PREFETCH_CHARS, MemoryBridge
from plugin_sdk.runtime_context import RuntimeContext


class _FakeMemoryContext:
    """Minimal stand-in for ``MemoryContext``. Bridge only reads
    ``.provider`` and ``._failure_state`` off it."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self._failure_state: dict[str, Any] = {}


class _ProviderReturning:
    """A fake provider whose ``prefetch`` returns a pre-baked string."""

    provider_id = "fake-prefetch-cap"

    def __init__(self, result: str | None) -> None:
        self._result = result

    async def prefetch(self, query: str, turn_index: int) -> str | None:
        return self._result


class _ProviderRaising:
    """A fake provider whose ``prefetch`` always raises."""

    provider_id = "fake-prefetch-raise"

    async def prefetch(self, query: str, turn_index: int) -> str | None:
        raise RuntimeError("provider blew up")


@pytest.mark.asyncio
async def test_prefetch_returns_short_output_unchanged() -> None:
    """Short provider output (well under the cap) is returned verbatim."""
    bridge = MemoryBridge(_FakeMemoryContext(_ProviderReturning("short text")))
    result = await bridge.prefetch("q", turn_index=0)
    assert result == "short text"


@pytest.mark.asyncio
async def test_prefetch_truncates_long_output_to_cap_with_marker() -> None:
    """Output longer than MAX_PREFETCH_CHARS is truncated with a marker.

    The result must stay close to the budget (marker adds ~30 chars — we
    allow +40 to cover any wording tweaks to the marker string) and must
    include the truncation marker.
    """
    provider = _ProviderReturning("x" * 3000)
    bridge = MemoryBridge(_FakeMemoryContext(provider))
    result = await bridge.prefetch("q", turn_index=0)

    assert result is not None
    assert "[…earlier recall truncated…]" in result
    # Budget: tail takes MAX_PREFETCH_CHARS - 40 chars + marker ~30.
    # Allow a small, bounded overhead for the marker.
    assert len(result) <= MAX_PREFETCH_CHARS + 40, (
        f"truncated result length {len(result)} exceeds cap+marker budget"
    )


@pytest.mark.asyncio
async def test_prefetch_cap_preserves_most_recent_content() -> None:
    """Truncation preserves the TAIL (most recent content), not the head.

    The assumption is that later content in the provider's returned string
    is more semantically recent (e.g. more recent turns). Verify by putting
    a unique ``EARLIEST_OLD_MARKER`` at the front of a >3000-char blob and
    a unique ``LATEST_NEW_MARKER`` at the end; after truncation the OLD
    marker must be gone and the NEW one must survive.
    """
    earliest_marker = "EARLIEST_OLD_MARKER_ZZZZZ"
    latest_marker = "LATEST_NEW_MARKER_YYYYY"
    # Put the EARLIEST marker at position 0; fill the middle with neutral
    # filler so the truncation cut point definitely drops it. The tail
    # window is MAX_PREFETCH_CHARS - 40 = 1960 chars, so with 3000 total
    # chars, anything before position ~1040 is dropped.
    filler = "." * (3000 - len(earliest_marker) - len(latest_marker))
    big = earliest_marker + filler + latest_marker
    assert len(big) > MAX_PREFETCH_CHARS  # sanity

    provider = _ProviderReturning(big)
    bridge = MemoryBridge(_FakeMemoryContext(provider))
    result = await bridge.prefetch("q", turn_index=0)

    assert result is not None
    # Latest marker survives — tail is preserved.
    assert latest_marker in result
    # Earliest marker is gone — head was dropped.
    assert earliest_marker not in result
    # Truncation marker is prepended.
    assert "[…earlier recall truncated…]" in result
    # Result ends with the preserved tail (latest marker is within the
    # final portion of the returned string).
    assert result.rstrip().endswith(latest_marker)


@pytest.mark.asyncio
async def test_prefetch_cap_applies_after_provider_exceptions_are_handled() -> None:
    """If the provider raises, bridge returns None; the cap does not apply
    to None (you can't truncate what isn't there)."""
    bridge = MemoryBridge(_FakeMemoryContext(_ProviderRaising()))
    result = await bridge.prefetch("q", turn_index=0)
    assert result is None


@pytest.mark.asyncio
async def test_prefetch_cap_does_not_affect_cron_guard() -> None:
    """The cron/flush guard short-circuits to None BEFORE the cap runs.

    If a provider would return a huge string but the runtime is cron, we
    should still get None — the guard must win, and the cap must not
    accidentally convert None into a truncated empty string or similar.
    """
    provider_mock = AsyncMock()
    # If the guard is doing its job, prefetch is never called on the provider.
    provider_mock.prefetch = AsyncMock(return_value="x" * 10_000)

    bridge = MemoryBridge(_FakeMemoryContext(provider_mock))
    result = await bridge.prefetch(
        "q", turn_index=0, runtime=RuntimeContext(agent_context="cron")
    )

    assert result is None
    # Provider's prefetch must not have been called at all.
    provider_mock.prefetch.assert_not_awaited()
