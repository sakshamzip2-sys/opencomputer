"""LLM-mediated recall synthesis (Round 4 Item 1).

Pinned to the user's brief: "think of something better than strict
keyword pattern matching". Hermes uses FTS5 → LLM summarisation
(Gemini Flash); we port the pattern using Haiku 4.5. Every recall
returns BOTH a synthesised answer (if available) AND the raw
candidates — synthesis is additive, never substitutive.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

# ─── synthesizer module unit tests ────────────────────────────────────


def _candidate(kind="episodic", text="kubernetes deployment failed", **k):
    from opencomputer.agent.recall_synthesizer import RecallCandidate

    return RecallCandidate(
        kind=kind,
        id=k.get("id", "1"),
        session_id=k.get("session_id", "abcd1234"),
        turn_index=k.get("turn_index", 5),
        text=text,
    )


def test_synthesize_skips_when_too_few_candidates() -> None:
    """<3 candidates → return None (raw is short enough; LLM call wasted)."""
    from opencomputer.agent.recall_synthesizer import synthesize_recall

    out = synthesize_recall("k8s?", [_candidate(), _candidate()])
    assert out is None


def test_synthesize_skips_when_explicit_false() -> None:
    """synthesize=False → never call LLM, even with many candidates."""
    from opencomputer.agent.recall_synthesizer import synthesize_recall

    candidates = [_candidate(id=str(i)) for i in range(5)]
    out = synthesize_recall("k8s?", candidates, synthesize=False)
    assert out is None


def test_synthesize_skips_on_env_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPENCOMPUTER_RECALL_SYNTHESIS=0 disables process-wide."""
    from opencomputer.agent.recall_synthesizer import synthesize_recall

    monkeypatch.setenv("OPENCOMPUTER_RECALL_SYNTHESIS", "0")
    candidates = [_candidate(id=str(i)) for i in range(5)]
    out = synthesize_recall("k8s?", candidates)
    assert out is None


def test_synthesize_calls_provider_and_returns_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: ≥3 candidates, fake provider returns valid citation."""
    from opencomputer.agent.recall_synthesizer import synthesize_recall

    monkeypatch.delenv("OPENCOMPUTER_RECALL_SYNTHESIS", raising=False)

    fake = _make_fake_provider("On 2026-04-12 you asked about kubernetes [1].")
    candidates = [_candidate(id=str(i)) for i in range(5)]

    out = synthesize_recall("when did I ask about kubernetes?", candidates, provider=fake)
    assert out is not None
    assert "kubernetes" in out
    assert "[1]" in out


def test_synthesize_rejects_out_of_range_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM tries to cite [99] — defence rejects, returns None so caller
    falls back to raw FTS5 (never shows hallucinated citation)."""
    from opencomputer.agent.recall_synthesizer import synthesize_recall

    monkeypatch.delenv("OPENCOMPUTER_RECALL_SYNTHESIS", raising=False)

    fake = _make_fake_provider("Found in [99].")
    candidates = [_candidate(id=str(i)) for i in range(5)]  # only 5 candidates

    out = synthesize_recall("?", candidates, provider=fake)
    assert out is None, "out-of-range citation must be rejected"


def test_synthesize_accepts_no_citations() -> None:
    """LLM honestly says 'no matching memory' — that's a valid answer
    (no citations needed, no hallucination)."""
    from opencomputer.agent.recall_synthesizer import synthesize_recall

    fake = _make_fake_provider("No matching memory found in the candidates.")
    candidates = [_candidate(id=str(i)) for i in range(5)]

    out = synthesize_recall("?", candidates, provider=fake)
    assert out is not None
    assert "no matching memory" in out.lower()


def test_synthesize_returns_none_when_provider_raises() -> None:
    """Provider down (network blip, auth failure, …) → return None.
    Caller never sees the exception."""
    from opencomputer.agent.recall_synthesizer import synthesize_recall

    class BoomProvider:
        async def complete(self, **_: Any) -> Any:
            raise RuntimeError("synthetic network failure")

    candidates = [_candidate(id=str(i)) for i in range(5)]
    out = synthesize_recall("?", candidates, provider=BoomProvider())
    assert out is None


def test_synthesize_returns_none_for_empty_response() -> None:
    """LLM returns empty string → None (treat as failure, not as 'no answer')."""
    from opencomputer.agent.recall_synthesizer import synthesize_recall

    fake = _make_fake_provider("   ")  # whitespace only
    candidates = [_candidate(id=str(i)) for i in range(5)]
    out = synthesize_recall("?", candidates, provider=fake)
    assert out is None


# ─── citation guard unit tests ────────────────────────────────────────


def test_citations_in_range_rejects_zero() -> None:
    from opencomputer.agent.recall_synthesizer import _citations_are_in_range

    assert _citations_are_in_range("Foo [0]", 5) is False


def test_citations_in_range_rejects_too_high() -> None:
    from opencomputer.agent.recall_synthesizer import _citations_are_in_range

    assert _citations_are_in_range("Foo [6]", 5) is False


def test_citations_in_range_accepts_valid() -> None:
    from opencomputer.agent.recall_synthesizer import _citations_are_in_range

    assert _citations_are_in_range("Foo [1] and [3].", 5) is True


def test_citations_in_range_accepts_no_citations() -> None:
    from opencomputer.agent.recall_synthesizer import _citations_are_in_range

    assert _citations_are_in_range("No matches.", 5) is True


# ─── helper ──────────────────────────────────────────────────────────


def _make_fake_provider(canned_text: str):
    """Return a fake provider whose async complete() returns ``canned_text``."""

    @dataclass
    class _FakeMessage:
        content: str

    @dataclass
    class _FakeResponse:
        message: _FakeMessage

    class FakeProvider:
        async def complete(self, *, messages, model, max_tokens, **_) -> _FakeResponse:
            return _FakeResponse(message=_FakeMessage(content=canned_text))

    return FakeProvider()
