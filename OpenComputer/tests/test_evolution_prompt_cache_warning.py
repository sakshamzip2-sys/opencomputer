"""PR-7: tests for caching compatibility warning on prompt-evolution proposals."""
from __future__ import annotations

import pytest


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


def _edit_system_prompt_insight():
    from opencomputer.evolution.reflect import Insight
    return Insight(
        observation="Add personality cue to system prompt",
        evidence_refs=(1, 2),
        action_type="edit_prompt",
        payload={"target": "system", "diff_hint": "Add 'Be concise.' to the system prompt."},
        confidence=0.85,
    )


def _edit_tool_spec_insight():
    from opencomputer.evolution.reflect import Insight
    return Insight(
        observation="Tool description unclear",
        evidence_refs=(1,),
        action_type="edit_prompt",
        payload={"target": "tool_spec", "diff_hint": "Clarify Read tool's `limit` parameter."},
        confidence=0.7,
    )


def _edit_other_insight():
    from opencomputer.evolution.reflect import Insight
    return Insight(
        observation="Hypothetical other target",
        evidence_refs=(1,),
        action_type="edit_prompt",
        payload={"target": "user_block", "diff_hint": "Adjust user-facing block."},
        confidence=0.6,
    )


def test_propose_flags_cache_warning_for_system_target(isolated_home):
    from opencomputer.evolution.prompt_evolution import PromptEvolver
    pe = PromptEvolver()
    p = pe.propose(_edit_system_prompt_insight(), active_session_id="sess-1")
    assert p.cache_invalidation_warning is True


def test_propose_flags_cache_warning_for_tool_spec_target(isolated_home):
    from opencomputer.evolution.prompt_evolution import PromptEvolver
    pe = PromptEvolver()
    p = pe.propose(_edit_tool_spec_insight(), active_session_id="sess-1")
    assert p.cache_invalidation_warning is True


def test_propose_no_warning_for_other_target(isolated_home):
    from opencomputer.evolution.prompt_evolution import PromptEvolver
    pe = PromptEvolver()
    p = pe.propose(_edit_other_insight(), active_session_id="sess-1")
    assert p.cache_invalidation_warning is False


def test_propose_no_warning_when_no_active_session(isolated_home):
    """Without an active_session_id, we can't predict cache invalidation."""
    from opencomputer.evolution.prompt_evolution import PromptEvolver
    pe = PromptEvolver()
    p = pe.propose(_edit_system_prompt_insight())  # no active_session_id
    assert p.cache_invalidation_warning is False


def test_warning_persists_across_list(isolated_home):
    """The flag round-trips through SQLite."""
    from opencomputer.evolution.prompt_evolution import PromptEvolver
    pe = PromptEvolver()
    p = pe.propose(_edit_system_prompt_insight(), active_session_id="sess-1")
    listed = pe.list_all()
    assert any(x.id == p.id and x.cache_invalidation_warning for x in listed)


def test_migration_003_idempotent(isolated_home):
    """Applying migrations twice is a no-op."""
    from opencomputer.evolution.storage import apply_pending, init_db
    conn = init_db()
    versions = apply_pending(conn)
    assert versions == []  # already applied; no new versions
    conn.close()
