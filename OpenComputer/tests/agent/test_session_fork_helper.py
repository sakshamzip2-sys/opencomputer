"""Pure-function tests for :func:`opencomputer.agent.session_fork.fork_session`.

The helper is shared by:

* ``oc session fork`` (CLI path) — ``opencomputer/cli_session.py::session_fork``
* ``/branch`` slash command — ``opencomputer/agent/slash_commands_impl/branch_cmd.py``

The :class:`BranchCommand` tests in ``tests/tier2_slash/test_branch_cmd.py``
exercise the slash → helper integration. This file tests the helper
directly so a regression in the helper fails its own dedicated test
case (rather than failing indirectly through ``BranchCommand``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencomputer.agent.session_fork import (
    TITLE_MAX_LEN,
    ForkResult,
    SourceSessionNotFoundError,
    _resolve_new_title,
    fork_session,
)

# ─── _resolve_new_title — pure function, no DB ─────────────────────


def test_resolve_title_user_value_wins() -> None:
    assert _resolve_new_title("ignored-source", "user-pick") == "user-pick"


def test_resolve_title_user_whitespace_falls_back_to_source() -> None:
    assert _resolve_new_title("src-title", "   ") == "src-title (fork)"


def test_resolve_title_user_none_falls_back_to_source() -> None:
    assert _resolve_new_title("src-title", None) == "src-title (fork)"


def test_resolve_title_no_source_no_user_uses_default() -> None:
    assert _resolve_new_title("", None) == "(fork)"
    assert _resolve_new_title(None, None) == "(fork)"


def test_resolve_title_user_truncated_to_cap() -> None:
    long_user = "x" * (TITLE_MAX_LEN + 50)
    out = _resolve_new_title("src", long_user)
    assert len(out) == TITLE_MAX_LEN
    assert out == "x" * TITLE_MAX_LEN


def test_resolve_title_source_plus_fork_suffix_truncated_to_cap() -> None:
    """When source title + ' (fork)' overflows, the whole result is
    truncated to ``TITLE_MAX_LEN``.

    Slightly lossy at the boundary (the '(fork)' suffix gets cut) but
    avoids ever returning a title > the cap.
    """
    long_src = "y" * TITLE_MAX_LEN
    out = _resolve_new_title(long_src, None)
    assert len(out) == TITLE_MAX_LEN


# ─── fork_session — full helper with a fake DB ─────────────────────


class _FakeDB:
    """Minimal duck-typed SessionDB stand-in.

    Matches the same shape used by ``tests/tier2_slash/test_branch_cmd.py``
    so the two test suites stay reviewable side-by-side.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list] = {}

    def get_session(self, sid: str) -> dict | None:
        return self.sessions.get(sid)

    def get_messages(self, sid: str) -> list:
        return list(self.messages.get(sid, []))

    def create_session(
        self,
        sid: str,
        *,
        platform: str = "",
        model: str = "",
        title: str = "",
        parent_session_id: str = "",
        **_extra: object,
    ) -> None:
        self.sessions[sid] = {
            "id": sid,
            "platform": platform,
            "model": model,
            "title": title,
            "parent_session_id": parent_session_id,
        }

    def append_messages_batch(self, sid: str, messages: list) -> None:
        self.messages.setdefault(sid, []).extend(messages)


def _seed(
    db: _FakeDB,
    sid: str,
    *,
    title: str = "src-title",
    platform: str = "cli",
    model: str = "claude",
    n_msgs: int = 3,
) -> None:
    db.sessions[sid] = {
        "id": sid,
        "platform": platform,
        "model": model,
        "title": title,
    }
    db.messages[sid] = [
        SimpleNamespace(
            role="user" if i % 2 == 0 else "assistant",
            content=f"msg {i}",
        )
        for i in range(n_msgs)
    ]


def test_fork_returns_forkresult() -> None:
    db = _FakeDB()
    _seed(db, "src-1")
    result = fork_session(db, "src-1")
    assert isinstance(result, ForkResult)
    assert result.messages_copied == 3
    assert result.new_session_id != "src-1"
    assert len(result.new_session_id) == 32  # uuid hex no dashes


def test_fork_copies_all_messages() -> None:
    db = _FakeDB()
    _seed(db, "src-1", n_msgs=7)
    result = fork_session(db, "src-1")
    new_msgs = db.messages[result.new_session_id]
    assert len(new_msgs) == 7
    # Verify the actual message objects were copied (not just count)
    for i, m in enumerate(new_msgs):
        assert m.content == f"msg {i}"


def test_fork_with_zero_messages_is_legal() -> None:
    """Brand-new chat with no turns can still be forked."""
    db = _FakeDB()
    _seed(db, "src-1", n_msgs=0)
    result = fork_session(db, "src-1")
    assert result.messages_copied == 0
    # New session exists but has no messages
    assert result.new_session_id in db.sessions
    assert db.messages.get(result.new_session_id, []) == []


def test_fork_inherits_platform_and_model() -> None:
    db = _FakeDB()
    _seed(db, "src-1", platform="telegram", model="claude-opus-4-7")
    result = fork_session(db, "src-1")
    new = db.sessions[result.new_session_id]
    assert new["platform"] == "telegram"
    assert new["model"] == "claude-opus-4-7"


def test_fork_default_title_appends_fork_suffix() -> None:
    db = _FakeDB()
    _seed(db, "src-1", title="my-debug")
    result = fork_session(db, "src-1")
    assert result.new_title == "my-debug (fork)"


def test_fork_explicit_title_overrides_default() -> None:
    db = _FakeDB()
    _seed(db, "src-1", title="ignored")
    result = fork_session(db, "src-1", title="try-X")
    assert result.new_title == "try-X"


def test_fork_no_source_title_falls_back_to_bare_default() -> None:
    db = _FakeDB()
    _seed(db, "src-1", title="")
    result = fork_session(db, "src-1")
    assert result.new_title == "(fork)"


def test_fork_unknown_source_raises_typed_error() -> None:
    db = _FakeDB()
    with pytest.raises(SourceSessionNotFoundError):
        fork_session(db, "ghost-id")


def test_fork_unknown_source_is_keyerror_subclass() -> None:
    """Callers can catch with the typed name OR ``KeyError``."""
    db = _FakeDB()
    with pytest.raises(KeyError):
        fork_session(db, "ghost-id")


def test_fork_default_does_not_record_parent() -> None:
    """CLI path expects no parent lineage (pre-Phase-H behaviour)."""
    db = _FakeDB()
    _seed(db, "src-1")
    result = fork_session(db, "src-1")
    new = db.sessions[result.new_session_id]
    # parent_session_id stays at the FakeDB default "" — meaning the
    # helper did NOT pass it to create_session.
    assert new["parent_session_id"] == ""


def test_fork_record_parent_true_propagates_lineage() -> None:
    """Slash-command path opts in to Phase H lineage."""
    db = _FakeDB()
    _seed(db, "src-1")
    result = fork_session(db, "src-1", record_parent=True)
    new = db.sessions[result.new_session_id]
    assert new["parent_session_id"] == "src-1"


def test_fork_record_parent_explicit_false_is_default_behaviour() -> None:
    """Belt-and-braces — ``record_parent=False`` matches the default."""
    db = _FakeDB()
    _seed(db, "src-1")
    result = fork_session(db, "src-1", record_parent=False)
    new = db.sessions[result.new_session_id]
    assert new["parent_session_id"] == ""


def test_fork_default_platform_when_source_has_empty_platform() -> None:
    """Helper falls back to ``'cli'`` when source has empty platform.

    Preserves pre-helper CLI behaviour (matched by reading the original
    inline code that did ``platform=src.get("platform", "") or "cli"``).
    """
    db = _FakeDB()
    _seed(db, "src-1", platform="")
    result = fork_session(db, "src-1")
    assert db.sessions[result.new_session_id]["platform"] == "cli"


def test_fork_long_title_silently_truncated() -> None:
    """The helper truncates over-length titles (slash command rejects
    earlier in its dispatch path — that's the slash's job, not the
    helper's). Callers that want to reject must validate before
    calling.
    """
    db = _FakeDB()
    _seed(db, "src-1")
    huge = "z" * (TITLE_MAX_LEN + 100)
    result = fork_session(db, "src-1", title=huge)
    assert len(result.new_title) == TITLE_MAX_LEN
    assert result.new_title == "z" * TITLE_MAX_LEN


def test_fork_new_session_id_is_unique_per_call() -> None:
    """Two fork() calls on the same source produce distinct new ids."""
    db = _FakeDB()
    _seed(db, "src-1")
    r1 = fork_session(db, "src-1")
    r2 = fork_session(db, "src-1")
    assert r1.new_session_id != r2.new_session_id
