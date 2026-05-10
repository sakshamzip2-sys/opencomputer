"""AgentLoop integrates the v18 compaction counter.

After a :class:`CompactionResult` with ``did_compact=True`` lands, the
loop must:

  1. Bump ``sessions.compactions_count`` for the active session via
     :meth:`SessionDB.increment_compaction_count`.
  2. Mirror the new value into ``runtime.custom["session_compactions"]``
     so the in-flight ``/usage`` and ``/context`` slash commands surface
     it on the very next turn (without another DB read).

We test the dedicated helper directly (``AgentLoop._record_compaction``)
to avoid the heavy ``run_conversation`` plumbing, and additionally
verify the helper is actually wired into both compaction-completion call
sites so a future refactor can't silently drop the integration.

Spec: ``docs/superpowers/specs/2026-05-10-cc-usage-context-visibility-design.md`` §4.3.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.config import Config, LoopConfig
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage
from plugin_sdk.runtime_context import RuntimeContext


class _NoOpProvider(BaseProvider):
    """Bare provider — never gets called by these tests."""

    async def complete(self, **kwargs):
        return ProviderResponse(
            message=Message(role="assistant", content=""),
            stop_reason="end_turn",
            usage=Usage(input_tokens=0, output_tokens=0),
        )

    async def stream_complete(self, **kwargs):
        if False:
            yield


def _make_loop(tmp_path: Path) -> AgentLoop:
    cfg = Config(
        loop=LoopConfig(max_iterations=1, parallel_tools=False),
        session=type(Config().session)(db_path=tmp_path / "s.db"),  # type: ignore[call-arg]
    )
    return AgentLoop(
        provider=_NoOpProvider(),
        config=cfg,
        db=SessionDB(tmp_path / "s.db"),
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )


def _fresh_loop(tmp_path: Path) -> AgentLoop:
    """``_make_loop`` + a fresh ``RuntimeContext`` so the
    module-shared ``DEFAULT_RUNTIME_CONTEXT.custom`` dict can't leak
    between test cases. ``run_conversation`` performs the equivalent
    reset on every real-world entry; tests bypass that path."""
    loop = _make_loop(tmp_path)
    loop._runtime = RuntimeContext()  # fresh empty custom dict per test
    return loop


def test_record_compaction_bumps_counter(tmp_path: Path) -> None:
    loop = _fresh_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli", model="claude-opus-4-7")
    loop._current_session_id = sid

    loop._record_compaction()

    summary = loop.db.session_usage_summary(sid)
    assert summary is not None
    assert summary.compactions_count == 1


def test_record_compaction_writes_runtime_custom(tmp_path: Path) -> None:
    loop = _fresh_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli", model="claude-opus-4-7")
    loop._current_session_id = sid

    loop._record_compaction()

    assert loop._runtime is not None
    assert loop._runtime.custom.get("session_compactions") == 1


def test_record_compaction_is_idempotent_on_subsequent_calls(tmp_path: Path) -> None:
    loop = _fresh_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli", model="m")
    loop._current_session_id = sid

    loop._record_compaction()
    loop._record_compaction()
    loop._record_compaction()

    summary = loop.db.session_usage_summary(sid)
    assert summary is not None
    assert summary.compactions_count == 3
    assert loop._runtime.custom["session_compactions"] == 3


def test_record_compaction_no_session_id_is_noop(tmp_path: Path) -> None:
    """Empty session id: no DB write, no runtime.custom write, no raise."""
    loop = _fresh_loop(tmp_path)
    loop._current_session_id = ""
    loop._record_compaction()
    # No row to inspect; key not set on runtime.
    assert "session_compactions" not in loop._runtime.custom


def test_record_compaction_unknown_session_is_noop(tmp_path: Path) -> None:
    """Session id whose row doesn't exist: counter stays 0, no runtime write."""
    loop = _fresh_loop(tmp_path)
    loop._current_session_id = "nonexistent-session"
    loop._record_compaction()
    # The helper's contract: when increment returned 0 (no row), do NOT
    # surface a fake counter into runtime.custom. /context and /usage
    # then read the absence of the key correctly.
    assert "session_compactions" not in loop._runtime.custom


def test_loop_calls_record_compaction_at_both_did_compact_sites() -> None:
    """Source-level guard: AgentLoop must invoke _record_compaction() at
    each post-compaction success branch. Greps the source for the
    method call inside the two ``if ...did_compact:`` blocks. Future
    refactors that move the counter elsewhere should update this test
    rather than silently drop the integration.
    """
    src = Path(__file__).parent.parent / "opencomputer" / "agent" / "loop.py"
    text = src.read_text(encoding="utf-8")
    # The helper must be defined.
    assert "def _record_compaction(self)" in text
    # The helper must be invoked. Two ``did_compact`` post-handlers exist
    # (one proactive, one reactive on CONTEXT_FULL retry); both should
    # call the helper. We assert >=2 invocations.
    invocations = text.count("self._record_compaction()")
    assert invocations >= 2, (
        f"Expected >=2 calls to self._record_compaction() — found {invocations}"
    )


def test_record_compaction_survives_db_error(tmp_path: Path, monkeypatch) -> None:
    """A DB error in increment_compaction_count must not propagate.
    SessionDB.increment_compaction_count already swallows + returns 0;
    the loop helper must not crash either."""
    loop = _fresh_loop(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli", model="m")
    loop._current_session_id = sid

    def boom(_self, _sid: str) -> int:
        raise RuntimeError("simulated DB outage")

    # Patch on the instance's class so the bound method changes.
    monkeypatch.setattr(
        type(loop.db),
        "increment_compaction_count",
        boom,
    )

    # Should not raise. Returns nothing.
    loop._record_compaction()
    # Counter stayed at 0 (the patch raised so no write happened).
    assert "session_compactions" not in loop._runtime.custom
