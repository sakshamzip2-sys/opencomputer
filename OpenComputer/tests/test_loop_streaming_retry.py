"""Integration test: AgentLoop streaming branch retries on overloaded_error.

Drives ``AgentLoop._run_one_step`` with a provider whose first
``stream_complete`` invocation raises ``overloaded_error`` (pre-first-byte)
and whose second invocation succeeds. Verifies:

  * the loop transparently retries (provider is called twice);
  * the user-facing ``stream_callback`` sees only one copy of the answer
    text (no duplication from a partial first attempt);
  * the supplied ``retry_callback`` receives a single inter-attempt
    :class:`RetryStatus` with ``error_kind="overloaded"``.

This is the production rollup test that demonstrates the asymmetry
fixed by ``opencomputer.agent.stream_retry`` — without the wrapper, the
529 propagates as an APIStatusError and the user sees a stack trace
in their terminal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent import stream_retry as _stream_retry_mod
from opencomputer.agent.config import (
    Config,
    LoopConfig,
    MemoryConfig,
    ModelConfig,
    SessionConfig,
)
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.state import SessionDB
from opencomputer.agent.stream_retry import RetryPolicy, RetryStatus
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)

# Per-call script: each entry is either an exception (raise on first
# __anext__) or a list of StreamEvent (yield then end).
_SCRIPT = [
    RuntimeError(
        "{'type': 'error', 'error': {'type': 'overloaded_error', "
        "'message': 'Overloaded'}, 'request_id': 'req_test1'}"
    ),
    [
        StreamEvent(kind="text_delta", text="hello"),
        StreamEvent(
            kind="done",
            response=ProviderResponse(
                message=Message(role="assistant", content="hello"),
                stop_reason="end_turn",
                usage=Usage(input_tokens=10, output_tokens=2),
            ),
        ),
    ],
]


class _FlakyStreamingProvider(BaseProvider):
    """Provider whose ``stream_complete`` follows the module-level script."""

    name = "flaky-stub"
    default_model = "stub-model-1"

    def __init__(self) -> None:
        self.script: list = list(_SCRIPT)
        self.calls = 0

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        # Non-streaming path isn't exercised here — return a sensible
        # default so any orthogonal code that probes the provider
        # (e.g. token counting) doesn't choke.
        return ProviderResponse(
            message=Message(role="assistant", content=""),
            stop_reason="end_turn",
            usage=Usage(),
        )

    async def stream_complete(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        if not self.script:
            raise RuntimeError(
                "FlakyStreamingProvider script exhausted — test bug"
            )
        behavior = self.script.pop(0)
        if isinstance(behavior, BaseException):
            raise behavior
        for ev in behavior:
            yield ev

    async def count_tokens(self, **kwargs: Any) -> int:
        return 0


@pytest.fixture
def fast_policy(monkeypatch: pytest.MonkeyPatch) -> RetryPolicy:
    """Replace the agent loop's stream-retry policy with a zero-delay one.

    The loop reads ``DEFAULT_POLICY`` from :mod:`opencomputer.agent.stream_retry`
    each turn, so monkey-patching the module attribute is sufficient.
    """
    fast = RetryPolicy(
        max_attempts=4,
        base_delay_seconds=0.0,
        cap_delay_seconds=0.0,
        jitter_ratio=0.0,
    )
    monkeypatch.setattr(
        "opencomputer.agent.stream_retry.DEFAULT_POLICY", fast
    )
    return fast


@pytest.mark.asyncio
async def test_overloaded_pre_first_byte_is_transparently_retried(
    tmp_path: Path, fast_policy: RetryPolicy
) -> None:
    """End-to-end: 529 on attempt 1 → retry → success on attempt 2.

    Asserts the user-visible side effects:
      * stream_callback receives "hello" exactly once;
      * provider.stream_complete is called twice (initial + 1 retry);
      * retry_callback observes one inter-attempt status with kind
        "overloaded".
    """
    assert fast_policy.cap_delay_seconds == 0.0  # sanity: zero-delay
    # The agent loop deref's DEFAULT_POLICY via the module each turn,
    # so monkey-patching the module attribute (in the fast_policy
    # fixture) flows through. Verify here so a regression where the
    # loop captures the import-time symbol is caught loudly.
    assert _stream_retry_mod.DEFAULT_POLICY.cap_delay_seconds == 0.0

    db = SessionDB(tmp_path / "retry.db")
    cfg = Config(
        model=ModelConfig(model="stub-model-1", max_tokens=4096),
        loop=LoopConfig(),
        session=SessionConfig(db_path=tmp_path / "retry.db"),
        memory=MemoryConfig(),
    )
    provider = _FlakyStreamingProvider()
    loop = AgentLoop(config=cfg, provider=provider, db=db)
    db.ensure_session("retry-session", platform="cli", model="stub-model-1")
    loop._current_session_id = "retry-session"  # noqa: SLF001

    seen_text: list[str] = []
    retry_events: list[RetryStatus] = []

    def _on_chunk(text: str) -> None:
        seen_text.append(text)

    def _on_retry(status: RetryStatus) -> None:
        retry_events.append(status)

    out = await loop._run_one_step(  # noqa: SLF001
        messages=[Message(role="user", content="hi")],
        system="you are a stub",
        session_id="retry-session",
        stream_callback=_on_chunk,
        retry_callback=_on_retry,
    )

    # Provider exercised twice: first raise, second success.
    assert provider.calls == 2
    # User saw the answer exactly once — no duplication from a phantom
    # first attempt.
    assert "".join(seen_text) == "hello"
    # The retry surface fired once between attempts with the right tag.
    assert len(retry_events) == 1
    assert retry_events[0].error_kind == "overloaded"
    assert retry_events[0].attempt == 1
    assert retry_events[0].next_attempt == 2
    assert retry_events[0].exhausted is False
    # Step completed normally.
    assert out.stop_reason.value == "end_turn"


@pytest.mark.asyncio
async def test_overloaded_exhaustion_propagates(
    tmp_path: Path, fast_policy: RetryPolicy
) -> None:
    """All attempts overloaded → final error reaches the caller."""
    db = SessionDB(tmp_path / "exhaust.db")
    cfg = Config(
        model=ModelConfig(model="stub-model-1", max_tokens=4096),
        loop=LoopConfig(),
        session=SessionConfig(db_path=tmp_path / "exhaust.db"),
        memory=MemoryConfig(),
    )

    class _AlwaysOverloaded(BaseProvider):
        name = "always-overloaded"
        default_model = "stub-model-1"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, **kwargs: Any) -> ProviderResponse:
            return ProviderResponse(
                message=Message(role="assistant", content=""),
                stop_reason="end_turn",
                usage=Usage(),
            )

        async def stream_complete(
            self, **kwargs: Any
        ) -> AsyncIterator[StreamEvent]:
            self.calls += 1
            raise RuntimeError("HTTP 529 overloaded_error: still down")
            if False:  # pragma: no cover — make this an async gen
                yield  # type: ignore[unreachable]

        async def count_tokens(self, **kwargs: Any) -> int:
            return 0

    provider = _AlwaysOverloaded()
    loop = AgentLoop(config=cfg, provider=provider, db=db)
    db.ensure_session("exhaust-session", platform="cli", model="stub-model-1")
    loop._current_session_id = "exhaust-session"  # noqa: SLF001

    retry_events: list[RetryStatus] = []

    with pytest.raises(RuntimeError, match="overloaded_error"):
        await loop._run_one_step(  # noqa: SLF001
            messages=[Message(role="user", content="hi")],
            system="you are a stub",
            session_id="exhaust-session",
            stream_callback=lambda _: None,
            retry_callback=retry_events.append,
        )

    # All four attempts were tried.
    assert provider.calls == fast_policy.max_attempts
    # 3 inter-attempt + 1 exhausted-final = 4 callbacks.
    assert len(retry_events) == fast_policy.max_attempts
    assert retry_events[-1].exhausted is True


@pytest.mark.asyncio
async def test_non_transient_pre_stream_failure_does_not_retry(
    tmp_path: Path, fast_policy: RetryPolicy
) -> None:
    """Auth errors are NOT transient — must propagate after one attempt."""
    db = SessionDB(tmp_path / "auth.db")
    cfg = Config(
        model=ModelConfig(model="stub-model-1", max_tokens=4096),
        loop=LoopConfig(),
        session=SessionConfig(db_path=tmp_path / "auth.db"),
        memory=MemoryConfig(),
    )

    class _AuthErrorProvider(BaseProvider):
        name = "auth-error"
        default_model = "stub-model-1"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, **kwargs: Any) -> ProviderResponse:
            return ProviderResponse(
                message=Message(role="assistant", content=""),
                stop_reason="end_turn",
                usage=Usage(),
            )

        async def stream_complete(
            self, **kwargs: Any
        ) -> AsyncIterator[StreamEvent]:
            self.calls += 1
            raise RuntimeError(
                "HTTP 401 authentication_error: invalid x-api-key"
            )
            if False:  # pragma: no cover
                yield  # type: ignore[unreachable]

        async def count_tokens(self, **kwargs: Any) -> int:
            return 0

    provider = _AuthErrorProvider()
    loop = AgentLoop(config=cfg, provider=provider, db=db)
    db.ensure_session("auth-session", platform="cli", model="stub-model-1")
    loop._current_session_id = "auth-session"  # noqa: SLF001

    retry_events: list[RetryStatus] = []

    with pytest.raises(RuntimeError, match="authentication_error"):
        await loop._run_one_step(  # noqa: SLF001
            messages=[Message(role="user", content="hi")],
            system="you are a stub",
            session_id="auth-session",
            stream_callback=lambda _: None,
            retry_callback=retry_events.append,
        )

    # Only one attempt — auth errors are not transient.
    assert provider.calls == 1
    # No retry status events (no retry attempted).
    assert retry_events == []
