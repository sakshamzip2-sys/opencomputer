"""Persistence — translate Layer 0/1/2 outputs into F4 user-model edges.

Mirrors :class:`opencomputer.user_model.importer.MotifImporter` shape;
each writer is idempotent via ``UserModelStore.upsert_node``. Node
persistence here does not tag edge provenance — the F4↔Honcho
cycle-prevention path applies only when edges are inserted (e.g., by
the motif importer). MVP persistence writes Identity / Goal / Preference
/ Attribute nodes only.
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
    if facts.name and facts.name.strip():
        s.upsert_node(kind="identity", value=f"name: {facts.name.strip()}", confidence=1.0)
        written += 1
    for email in facts.emails:
        e = email.strip()
        if not e:
            continue
        s.upsert_node(kind="identity", value=f"email: {e}", confidence=1.0)
        written += 1
    for phone in facts.phones:
        p = phone.strip()
        if not p:
            continue
        s.upsert_node(kind="identity", value=f"phone: {p}", confidence=1.0)
        written += 1
    if facts.github_handle and facts.github_handle.strip():
        s.upsert_node(kind="identity", value=f"github: {facts.github_handle.strip()}", confidence=1.0)
        written += 1
    if facts.city and facts.city.strip():
        s.upsert_node(kind="identity", value=f"city: {facts.city.strip()}", confidence=1.0)
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
    # Quick-interview answer keys → F4 NodeKind. NodeKind is a closed
    # literal (identity/attribute/relationship/goal/preference); we map
    # `current_concerns` to "goal" rather than adding a new "concern" kind
    # because adding a NodeKind member is a breaking SDK change.
    kind_map = {
        "current_focus": "goal",
        "current_concerns": "goal",
        "tone_preference": "preference",
        "do_not": "preference",
        "context": "attribute",
    }
    written = 0
    for question_key, answer in answers.items():
        a = (answer or "").strip()
        if not a:
            continue
        kind = kind_map.get(question_key, "attribute")
        s.upsert_node(
            kind=kind,
            value=f"{question_key}: {a}",
            confidence=1.0,
        )
        written += 1
    return written
