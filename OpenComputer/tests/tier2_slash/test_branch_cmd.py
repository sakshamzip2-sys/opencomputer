"""Tests for /branch — session fork."""
from types import SimpleNamespace

import pytest

from opencomputer.agent.slash_commands_impl.branch_cmd import BranchCommand
from plugin_sdk.runtime_context import RuntimeContext


class _FakeDB:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list] = {}

    def get_session(self, sid):
        return self.sessions.get(sid)

    def get_messages(self, sid):
        return list(self.messages.get(sid, []))

    def create_session(
        self,
        sid,
        *,
        platform="",
        model="",
        title="",
        parent_session_id="",
        **_extra,
    ):
        """Mirror SessionDB.create_session signature.

        Phase H integration (2026-05-11) — branch_cmd now passes
        ``parent_session_id=src_sid`` so the resume picker can group
        forks under their root. The fake DB records it so tests can
        assert the lineage was wired.
        """
        self.sessions[sid] = {
            "id": sid,
            "platform": platform,
            "model": model,
            "title": title,
            "parent_session_id": parent_session_id,
        }

    def append_messages_batch(self, sid, messages):
        self.messages.setdefault(sid, []).extend(messages)


def _runtime(sid: str, db: _FakeDB) -> RuntimeContext:
    return RuntimeContext(custom={"session_id": sid, "session_db": db})


def _seed_db(db: _FakeDB, sid: str, *, title: str = "src-title", n_msgs: int = 3):
    db.sessions[sid] = {"id": sid, "platform": "cli", "model": "claude", "title": title}
    db.messages[sid] = [
        SimpleNamespace(role="user" if i % 2 == 0 else "assistant", content=f"msg {i}")
        for i in range(n_msgs)
    ]


@pytest.mark.asyncio
async def test_branch_creates_new_session():
    db = _FakeDB()
    _seed_db(db, "src-1")
    rt = _runtime("src-1", db)
    result = await BranchCommand().execute("", rt)
    # 2026-05-11: branch output is now a pi-style summary card.
    # Assert on the specific card structure (header + id row + msg
    # count row + resume hint) so a regression that omits any row
    # fails the test loudly.
    out = result.output
    assert "branch" in out.lower(), "card header must say 'branch'"
    # New session created.
    new_sids = [s for s in db.sessions if s != "src-1"]
    assert len(new_sids) == 1
    new_id = new_sids[0]
    # 8-char id prefix appears in the "id: ..." row.
    assert f"id: {new_id[:8]}" in out
    # Resume hint shows the FULL session id (so the user can copy-paste).
    assert f"oc chat --resume {new_id}" in out
    # Card uses Unicode box-drawing characters.
    assert "╭" in out and "╰" in out, "card must be visually framed"


@pytest.mark.asyncio
async def test_branch_copies_messages():
    db = _FakeDB()
    _seed_db(db, "src-1", n_msgs=5)
    rt = _runtime("src-1", db)
    await BranchCommand().execute("", rt)
    new_sids = [s for s in db.sessions if s != "src-1"]
    new_sid = new_sids[0]
    # Each message should be in the new session
    assert len(db.messages[new_sid]) == 5


@pytest.mark.asyncio
async def test_branch_with_explicit_title():
    db = _FakeDB()
    _seed_db(db, "src-1", title="original")
    rt = _runtime("src-1", db)
    result = await BranchCommand().execute("try-X", rt)
    assert "try-X" in result.output
    new_sids = [s for s in db.sessions if s != "src-1"]
    assert db.sessions[new_sids[0]]["title"] == "try-X"


@pytest.mark.asyncio
async def test_branch_default_title_appends_fork():
    db = _FakeDB()
    _seed_db(db, "src-1", title="original")
    rt = _runtime("src-1", db)
    result = await BranchCommand().execute("", rt)
    new_sids = [s for s in db.sessions if s != "src-1"]
    assert db.sessions[new_sids[0]]["title"] == "original (fork)"


@pytest.mark.asyncio
async def test_branch_default_title_when_no_source_title():
    db = _FakeDB()
    _seed_db(db, "src-1", title="")
    rt = _runtime("src-1", db)
    result = await BranchCommand().execute("", rt)
    new_sids = [s for s in db.sessions if s != "src-1"]
    assert db.sessions[new_sids[0]]["title"] == "(fork)"


@pytest.mark.asyncio
async def test_branch_inherits_platform_and_model():
    db = _FakeDB()
    _seed_db(db, "src-1")
    db.sessions["src-1"]["platform"] = "telegram"
    db.sessions["src-1"]["model"] = "claude-opus-4-7"
    rt = _runtime("src-1", db)
    await BranchCommand().execute("", rt)
    new_sids = [s for s in db.sessions if s != "src-1"]
    new = db.sessions[new_sids[0]]
    assert new["platform"] == "telegram"
    assert new["model"] == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_branch_no_session():
    rt = RuntimeContext(custom={})
    result = await BranchCommand().execute("", rt)
    assert "no active session" in result.output.lower()


@pytest.mark.asyncio
async def test_branch_source_not_found():
    db = _FakeDB()
    rt = _runtime("ghost", db)
    result = await BranchCommand().execute("", rt)
    assert "not found" in result.output.lower()


@pytest.mark.asyncio
async def test_branch_title_too_long():
    db = _FakeDB()
    _seed_db(db, "src-1")
    rt = _runtime("src-1", db)
    huge = "x" * 250
    result = await BranchCommand().execute(huge, rt)
    assert "too long" in result.output.lower()


@pytest.mark.asyncio
async def test_branch_resume_hint_in_output():
    db = _FakeDB()
    _seed_db(db, "src-1")
    rt = _runtime("src-1", db)
    result = await BranchCommand().execute("", rt)
    assert "oc chat --resume" in result.output


@pytest.mark.asyncio
async def test_branch_with_zero_messages():
    db = _FakeDB()
    _seed_db(db, "src-1", n_msgs=0)
    rt = _runtime("src-1", db)
    result = await BranchCommand().execute("", rt)
    # Card always renders, even when zero messages are copied.
    out = result.output
    assert "branch" in out.lower()
    # Plural "messages" preserved at zero (English convention: "0 messages").
    assert "0 messages copied" in out
    # Card framing intact.
    assert "╭" in out and "╰" in out
    new_sids = [s for s in db.sessions if s != "src-1"]
    assert db.messages.get(new_sids[0], []) == []


def test_metadata():
    cmd = BranchCommand()
    assert cmd.name == "branch"
    assert "fork" in cmd.description.lower() or "branch" in cmd.description.lower()


# ─── Phase H integration — fork lineage propagation ──────────────────


@pytest.mark.asyncio
async def test_branch_sets_parent_session_id_on_forked_session() -> None:
    """The /branch command must record the source session as the parent
    so the resume picker can group the fork under it in the fork-tree UI.
    Regression guard for Phase H integration (2026-05-11)."""
    db = _FakeDB()
    _seed_db(db, "src-1", title="original")
    rt = _runtime("src-1", db)
    await BranchCommand().execute("my-fork", rt)
    new_sids = [s for s in db.sessions if s != "src-1"]
    assert len(new_sids) == 1
    assert db.sessions[new_sids[0]]["parent_session_id"] == "src-1"
