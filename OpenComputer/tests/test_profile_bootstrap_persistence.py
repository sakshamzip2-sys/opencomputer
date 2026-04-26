"""Layered Awareness MVP — translate Layer 0/1/2 outputs into F4 user-model edges.

Persistence helpers are idempotent (upsert by ``(kind, value)``). These tests
use a real ``UserModelStore`` rather than a mock so the SQLite schema +
upsert semantics are exercised end-to-end.
"""
from pathlib import Path

import pytest

from opencomputer.profile_bootstrap.identity_reflex import IdentityFacts
from opencomputer.profile_bootstrap.persistence import (
    write_identity_to_graph,
    write_interview_answers_to_graph,
)
from opencomputer.user_model.store import UserModelStore


@pytest.fixture
def store(tmp_path: Path) -> UserModelStore:
    return UserModelStore(tmp_path / "graph.sqlite")


def test_write_identity_creates_name_node(store):
    facts = IdentityFacts(name="Saksham", emails=("a@b.com",))
    write_identity_to_graph(facts, store=store)
    rows = store.list_nodes(kinds=("identity",))
    names = {n.value for n in rows}
    assert "name: Saksham" in names


def test_write_identity_creates_email_nodes(store):
    facts = IdentityFacts(emails=("a@b.com", "c@d.com"))
    write_identity_to_graph(facts, store=store)
    rows = store.list_nodes(kinds=("identity",))
    emails = {n.value for n in rows}
    assert "email: a@b.com" in emails
    assert "email: c@d.com" in emails


def test_write_identity_idempotent(store):
    facts = IdentityFacts(name="Saksham")
    write_identity_to_graph(facts, store=store)
    write_identity_to_graph(facts, store=store)
    rows = store.list_nodes(kinds=("identity",))
    matching = [n for n in rows if "Saksham" in n.value]
    assert len(matching) == 1  # upsert, not duplicate


def test_write_interview_creates_preference_nodes(store):
    answers = {
        "current_focus": "Shipping OpenComputer v1.0",
        "tone_preference": "concise and action-first",
        "do_not": "never send emails without confirmation",
    }
    n = write_interview_answers_to_graph(answers, store=store)
    nodes = store.list_nodes()
    values = {x.value for x in nodes}
    assert any("OpenComputer" in v for v in values)
    assert any("concise" in v for v in values)
    assert n >= 3


def test_write_identity_skips_whitespace_only_fields(store):
    """Whitespace-only values shouldn't create nodes like 'name:   '."""
    facts = IdentityFacts(
        name="   ",
        emails=("   ", "ok@e.com"),
    )
    write_identity_to_graph(facts, store=store)
    rows = store.list_nodes(kinds=("identity",))
    values = {n.value for n in rows}
    assert "name:    " not in values
    assert "email: ok@e.com" in values
    assert "email:    " not in values


def test_write_interview_skips_whitespace_only_answers(store):
    """Whitespace-only answers shouldn't create nodes."""
    answers = {"current_focus": "   ", "tone_preference": "concise"}
    n = write_interview_answers_to_graph(answers, store=store)
    nodes = {x.value for x in store.list_nodes()}
    assert any("concise" in v for v in nodes)
    assert not any("current_focus:    " in v for v in nodes)
    assert n == 1  # only the non-whitespace one
