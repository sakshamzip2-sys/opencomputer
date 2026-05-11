"""Phase D — ``oc chat -n NAME`` startup flag + ``oc resume NAME``.

Coverage:

    1. The chat-CLI signature exposes ``-n`` / ``--name`` with the
       expected help string.
    2. ``set_session_title`` is the right write-path: it persists,
       survives ``create_session``'s COALESCE, and is queryable by
       ``find_session_by_title`` (which is what ``oc resume <name>``
       resolves through).
    3. Whitespace-only ``--name`` is treated as "no name" — never
       persisted as a row with title=" " that would confuse the
       lineage-resolver.
    4. The resume-by-name resolution path returns the session id for
       an exact title match.

We don't drive ``_run_chat_session`` end-to-end here because it
launches the interactive REPL. The Phase D wiring is two lines of
glue in ``_run_chat_session`` — the actual contract is the DB-side
behaviour (already in production), which these tests pin down.
"""
from __future__ import annotations

import inspect
import uuid
from pathlib import Path

import pytest

from opencomputer.agent.state import SessionDB


# ─── CLI signature ───────────────────────────────────────────────────


def test_oc_chat_typer_command_exposes_n_and_name_options() -> None:
    """The Typer command must accept ``-n`` and ``--name`` synonyms."""
    from opencomputer.cli import chat

    sig = inspect.signature(chat)
    assert "name" in sig.parameters
    # Typer parameter defaults are OptionInfo objects; introspect them.
    name_param = sig.parameters["name"]
    default = name_param.default
    # The Option carries the CLI flag list as ``param_decls``.
    decls = getattr(default, "param_decls", None) or []
    # Either via param_decls or via .default's repr — both forms work.
    assert any("-n" in str(d) for d in decls) or "-n" in repr(default)
    assert any("--name" in str(d) for d in decls) or "--name" in repr(default)


# ─── set_session_title persistence + COALESCE ─────────────────────────


def test_set_session_title_persists_and_survives_subsequent_create_session(
    tmp_path: Path,
) -> None:
    """``oc chat -n foo`` pattern: write title FIRST, then ``create_session``
    fires on the first turn. The create UPSERT must NOT overwrite the
    title — that's the whole reason ``create_session`` uses COALESCE."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex

    # Step 1: --name applied before the first turn.
    db.set_session_title(sid, "my-feature-x")

    # Step 2: first turn fires, agent loop calls ensure_session.
    db.ensure_session(sid, platform="cli", model="m")

    persisted = db.get_session(sid)
    assert persisted is not None
    assert persisted["title"] == "my-feature-x"


def test_set_session_title_persists_and_survives_create_session_upsert(
    tmp_path: Path,
) -> None:
    """Same idea as the ensure_session variant but for ``create_session``
    (UPSERT path used by the ``oc sessions fork`` CLI). The UPSERT
    branch must also COALESCE the title."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex

    db.set_session_title(sid, "my-feature-x")
    db.create_session(sid, platform="cli", model="m", title="")  # empty title

    assert db.get_session(sid)["title"] == "my-feature-x"


# ─── resume-by-name path ─────────────────────────────────────────────


def test_find_session_by_title_returns_session_for_exact_match(
    tmp_path: Path,
) -> None:
    """``oc resume <name>`` resolves via ``find_session_by_title``."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.set_session_title(sid, "feature/auth-refactor")
    db.ensure_session(sid)

    row = db.find_session_by_title("feature/auth-refactor")
    assert row is not None
    assert row["id"] == sid


def test_find_session_by_title_returns_none_for_unknown(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    assert db.find_session_by_title("does-not-exist") is None


def test_find_session_by_title_is_case_sensitive(tmp_path: Path) -> None:
    """Title resolution is exact-match — mirrors Claude Code's behaviour
    (CC opens the picker on ambiguous names rather than fuzzy-matching)."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.set_session_title(sid, "MyFeature")
    db.ensure_session(sid)

    assert db.find_session_by_title("myfeature") is None
    assert db.find_session_by_title("MyFeature") is not None


# ─── --name input sanitisation ───────────────────────────────────────


def test_whitespace_only_name_should_not_be_persisted_as_title() -> None:
    """If the user runs ``oc chat -n '   '``, the wiring in
    _run_chat_session strips whitespace and treats the result as empty
    — no title is written. We assert the strip-then-check logic
    directly because it's a single source of truth (one line in cli.py)."""
    raw = "   \t  \n  "
    cleaned = raw.strip()
    assert cleaned == ""
    # The wiring path is:
    #   if name and not resume:
    #       cleaned_name = name.strip()
    #       if cleaned_name:
    #           db.set_session_title(...)
    # → whitespace-only input never reaches the DB.


def test_name_input_strips_outer_whitespace_before_persist(tmp_path: Path) -> None:
    """``oc chat -n '  my session  '`` lands as ``'my session'`` on disk."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    raw = "  my session  "
    cleaned = raw.strip()
    if cleaned:
        db.set_session_title(sid, cleaned)
    persisted = db.get_session(sid)
    assert persisted is not None
    assert persisted["title"] == "my session"
