"""Persistence — translate Layer 0/1/2 outputs into F4 user-model edges.

Mirrors :class:`opencomputer.user_model.importer.MotifImporter` shape;
each writer is idempotent via ``UserModelStore.upsert_node``. The
``source`` column on every edge tags provenance for the
F4↔Honcho cycle-prevention path (Phase 4.A schema v2).
"""
from __future__ import annotations

from opencomputer.profile_bootstrap.identity_reflex import IdentityFacts
from opencomputer.user_model.store import UserModelStore


def write_identity_to_graph(
    facts: IdentityFacts,
    *,
    store: UserModelStore | None = None,
) -> int:
    """Persist :class:`IdentityFacts` as Identity nodes.

    Returns the number of nodes written/upserted (excluding edges).
    Idempotent — repeated calls re-upsert without duplicating.
    """
    s = store if store is not None else UserModelStore()
    written = 0
    if facts.name:
        s.upsert_node(kind="identity", value=f"name: {facts.name}", confidence=1.0)
        written += 1
    for email in facts.emails:
        s.upsert_node(kind="identity", value=f"email: {email}", confidence=1.0)
        written += 1
    for phone in facts.phones:
        s.upsert_node(kind="identity", value=f"phone: {phone}", confidence=1.0)
        written += 1
    if facts.github_handle:
        s.upsert_node(kind="identity", value=f"github: {facts.github_handle}", confidence=1.0)
        written += 1
    if facts.city:
        s.upsert_node(kind="identity", value=f"city: {facts.city}", confidence=1.0)
        written += 1
    return written


def write_interview_answers_to_graph(
    answers: dict[str, str],
    *,
    store: UserModelStore | None = None,
) -> int:
    """Persist Layer 1 quick-interview answers as Preference + Goal nodes.

    Each answer is stored as a node with a question-keyed prefix so the
    raw answer is recoverable. Confidence is 1.0 (user-explicit).
    Returns the number of nodes upserted.
    """
    s = store if store is not None else UserModelStore()
    kind_map = {
        "current_focus": "goal",
        "current_concerns": "goal",
        "tone_preference": "preference",
        "do_not": "preference",
        "context": "attribute",
    }
    written = 0
    for question_key, answer in answers.items():
        if not answer:
            continue
        kind = kind_map.get(question_key, "attribute")
        s.upsert_node(
            kind=kind,
            value=f"{question_key}: {answer}",
            confidence=1.0,
        )
        written += 1
    return written
