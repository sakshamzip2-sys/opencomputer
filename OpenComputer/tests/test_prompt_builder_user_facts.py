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
