"""Layered Awareness MVP — prompt builder user_facts injection tests."""
from pathlib import Path

from opencomputer.agent.prompt_builder import PromptBuilder
from opencomputer.user_model.store import UserModelStore


def test_user_facts_section_rendered_when_present(tmp_path: Path):
    store = UserModelStore(tmp_path / "graph.sqlite")
    store.upsert_node(kind="identity", value="name: Saksham", confidence=1.0)
    store.upsert_node(kind="goal", value="current_focus: Ship OC v1.0", confidence=1.0)

    pb = PromptBuilder()
    facts = pb.build_user_facts(store=store)
    rendered = pb.build(user_facts=facts)
    assert "Saksham" in rendered
    assert "OC v1.0" in rendered or "v1.0" in rendered


def test_user_facts_section_absent_when_empty(tmp_path: Path):
    store = UserModelStore(tmp_path / "graph.sqlite")
    pb = PromptBuilder()
    facts_block = pb.build_user_facts(store=store)
    assert facts_block == ""  # no facts → empty


# ─── M3 — context-aware reranker integration ─────────────────────────


def test_user_facts_excludes_soft_deleted(tmp_path: Path):
    """A forgotten (soft-deleted) fact is not injected into the prompt."""
    store = UserModelStore(tmp_path / "graph.sqlite")
    store.upsert_node(kind="attribute", value="LIVEFACT", confidence=0.9)
    store.upsert_node(kind="attribute", value="GONEFACT", confidence=0.9,
                      metadata={"deleted": True})
    facts = PromptBuilder().build_user_facts(store=store)
    assert "LIVEFACT" in facts
    assert "GONEFACT" not in facts


def test_user_facts_excludes_needs_review(tmp_path: Path):
    """A migrate-flagged noise fact is not injected into the prompt."""
    store = UserModelStore(tmp_path / "graph.sqlite")
    store.upsert_node(kind="attribute", value="REALFACT", confidence=0.9)
    store.upsert_node(kind="attribute", value="NOISEFACT", confidence=0.9,
                      metadata={"needs_review": True})
    facts = PromptBuilder().build_user_facts(store=store)
    assert "REALFACT" in facts
    assert "NOISEFACT" not in facts


def test_user_facts_session_context_boosts_relevant_fact(tmp_path: Path):
    """A fact relevant to the opening message ranks ahead of an unrelated one."""
    from opencomputer.user_model.reranker import SessionContext

    store = UserModelStore(tmp_path / "graph.sqlite")
    store.upsert_node(kind="attribute", value="enjoys mountain hiking",
                      confidence=0.8)
    store.upsert_node(kind="attribute", value="writes python services",
                      confidence=0.8)
    ctx = SessionContext(recent_messages=("help me debug my python code",))
    facts = PromptBuilder().build_user_facts(store=store, session_context=ctx)
    assert facts.index("python") < facts.index("hiking")


def test_user_facts_context_free_still_ranks(tmp_path: Path):
    """With no session context the block is still produced (static ranking)."""
    store = UserModelStore(tmp_path / "graph.sqlite")
    store.upsert_node(kind="identity", value="name: Saksham", confidence=1.0)
    facts = PromptBuilder().build_user_facts(store=store)
    assert "Saksham" in facts
