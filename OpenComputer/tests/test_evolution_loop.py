"""Tests for ``ProceduralMemoryLoop`` orchestrator (Phase 5.B-3)."""

from __future__ import annotations

import pytest

from opencomputer.evolution.procedural_memory_loop import ProceduralMemoryLoop
from opencomputer.evolution.store import (
    approved_dir,
    archive_dir,
    ensure_dirs,
    quarantine_dir,
)

_GOOD_DRAFT = """---
name: pytest-rerun
description: Use when pytest fails repeatedly to re-run failures fast
---

# Pytest Rerun
## When to use
- pytest failed multiple times
## Steps
1. Run `pytest -lf -x`
"""


class _FakeProvider:
    def __init__(self, return_text: str = ""):
        self.return_text = return_text
        self.calls = 0

    async def complete(self, prompt: str) -> str:
        self.calls += 1
        return self.return_text


class _AllowGate:
    """Stand-in for F1 ConsentGate that always allows."""

    def check(self, claim, *, scope, session_id):
        return type("D", (), {"allow": True, "reason": "test"})()


class _DenyGate:
    def check(self, claim, *, scope, session_id):
        return type("D", (), {"allow": False, "reason": "no grant"})()


# ---------- Wiring smoke test ----------


@pytest.mark.asyncio
async def test_below_threshold_no_synthesis(tmp_path):
    loop = ProceduralMemoryLoop(
        home=tmp_path,
        provider=_FakeProvider(_GOOD_DRAFT),
        consent_gate=_AllowGate(),
    )
    for _ in range(2):
        loop.observe("Bash", {"command": "pytest"}, error=True)
    written = await loop.maybe_propose_drafts()
    assert written == []
    assert loop.synthesizer.provider.calls == 0


@pytest.mark.asyncio
async def test_threshold_reached_drafts_one(tmp_path):
    loop = ProceduralMemoryLoop(
        home=tmp_path,
        provider=_FakeProvider(_GOOD_DRAFT),
        consent_gate=_AllowGate(),
    )
    for _ in range(3):
        loop.observe("Bash", {"command": "pytest"}, error=True)
    written = await loop.maybe_propose_drafts()
    assert len(written) == 1
    assert (quarantine_dir(tmp_path) / "pytest-rerun" / "SKILL.md").exists()


# ---------- Consent gate ----------


@pytest.mark.asyncio
async def test_consent_denied_skips_synthesis(tmp_path):
    fake = _FakeProvider(_GOOD_DRAFT)
    loop = ProceduralMemoryLoop(
        home=tmp_path,
        provider=fake,
        consent_gate=_DenyGate(),
    )
    for _ in range(3):
        loop.observe("Bash", {"command": "pytest"}, error=True)
    written = await loop.maybe_propose_drafts()
    assert written == []
    assert fake.calls == 0  # provider never called


@pytest.mark.asyncio
async def test_no_gate_means_test_only_default_allow(tmp_path):
    """Passing consent_gate=None bypasses the gate (test convenience)."""
    loop = ProceduralMemoryLoop(
        home=tmp_path,
        provider=_FakeProvider(_GOOD_DRAFT),
        consent_gate=None,
    )
    for _ in range(3):
        loop.observe("Bash", {"command": "pytest"}, error=True)
    written = await loop.maybe_propose_drafts()
    assert len(written) == 1


# ---------- Rate limiter ----------


@pytest.mark.asyncio
async def test_rate_limit_blocks_after_first_draft(tmp_path):
    fake = _FakeProvider(_GOOD_DRAFT)
    loop = ProceduralMemoryLoop(
        home=tmp_path,
        provider=fake,
        consent_gate=_AllowGate(),
    )
    # First draft — succeeds
    for _ in range(3):
        loop.observe("Bash", {"command": "pytest"}, error=True)
    first = await loop.maybe_propose_drafts()
    assert len(first) == 1

    # Second pattern in same session — still under per-day cap of 1
    for _ in range(3):
        loop.observe("Bash", {"command": "git"}, error=True)
    second = await loop.maybe_propose_drafts()
    assert second == []  # rate-limited (per-day cap = 1)


# ---------- Archive suppression ----------


@pytest.mark.asyncio
async def test_archived_pattern_not_re_proposed(tmp_path):
    """If a draft was previously discarded, don't propose the same pattern again."""
    ensure_dirs(tmp_path)
    # Simulate a discarded draft with pattern marker
    archived = archive_dir(tmp_path) / "pytest-rerun"
    archived.mkdir(parents=True)
    (archived / ".pattern_key").write_text("bash:pytest:fail\n")

    fake = _FakeProvider(_GOOD_DRAFT)
    loop = ProceduralMemoryLoop(
        home=tmp_path,
        provider=fake,
        consent_gate=_AllowGate(),
    )
    for _ in range(3):
        loop.observe("Bash", {"command": "pytest"}, error=True)
    written = await loop.maybe_propose_drafts()
    assert written == []
    assert fake.calls == 0  # short-circuited before LLM call


def test_mark_archived_pattern_writes_marker(tmp_path):
    ensure_dirs(tmp_path)
    (archive_dir(tmp_path) / "x").mkdir(parents=True)
    loop = ProceduralMemoryLoop(
        home=tmp_path, provider=_FakeProvider(""), consent_gate=None
    )
    loop.mark_archived_pattern("x", "bash:foo:fail")
    marker = archive_dir(tmp_path) / "x" / ".pattern_key"
    assert marker.exists()
    assert marker.read_text().strip() == "bash:foo:fail"


# ---------- Defensive errors ----------


@pytest.mark.asyncio
async def test_synthesis_error_does_not_propagate(tmp_path):
    """A bad LLM output is logged + skipped; loop returns cleanly."""
    fake = _FakeProvider("")  # empty → SynthesisError
    loop = ProceduralMemoryLoop(
        home=tmp_path,
        provider=fake,
        consent_gate=_AllowGate(),
    )
    for _ in range(3):
        loop.observe("Bash", {"command": "pytest"}, error=True)
    written = await loop.maybe_propose_drafts()
    assert written == []  # nothing written, no exception either


@pytest.mark.asyncio
async def test_unexpected_provider_exception_does_not_propagate(tmp_path):
    class _BoomProvider:
        async def complete(self, p):
            raise RuntimeError("provider boom")

    loop = ProceduralMemoryLoop(
        home=tmp_path,
        provider=_BoomProvider(),
        consent_gate=_AllowGate(),
    )
    for _ in range(3):
        loop.observe("Bash", {"command": "pytest"}, error=True)
    written = await loop.maybe_propose_drafts()
    assert written == []
