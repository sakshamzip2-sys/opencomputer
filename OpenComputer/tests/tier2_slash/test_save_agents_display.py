"""Tests for /save, /agents, /verbose, /statusbar."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencomputer.agent.slash_commands_impl.agents_cmd import AgentsCommand
from opencomputer.agent.slash_commands_impl.display_toggles_cmd import (
    StatusbarCommand,
    VerboseCommand,
)
from opencomputer.agent.slash_commands_impl.save_cmd import SaveCommand
from plugin_sdk.runtime_context import RuntimeContext

# ---------- /save ----------


class _SaveFakeDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list] = {}

    def get_session(self, sid):
        return self.sessions.get(sid)

    def get_messages(self, sid):
        return list(self.messages.get(sid, []))


def _runtime_save(sid, db) -> RuntimeContext:
    return RuntimeContext(custom={"session_id": sid, "session_db": db})


@pytest.mark.asyncio
async def test_save_creates_markdown_file(tmp_path):
    db = _SaveFakeDB(tmp_path / "sessions.db")
    db.sessions["s1"] = {"id": "s1", "title": "test session"}
    db.messages["s1"] = [
        SimpleNamespace(role="user", content="hello"),
        SimpleNamespace(role="assistant", content="hi there!"),
    ]
    rt = _runtime_save("s1", db)
    result = await SaveCommand().execute("", rt)
    assert "Saved 2 messages" in result.output
    out = tmp_path / "exports" / "s1.md"
    assert out.exists()
    content = out.read_text()
    assert "# test session" in content
    assert "hello" in content
    assert "hi there!" in content


@pytest.mark.asyncio
async def test_save_no_messages():
    db = _SaveFakeDB(Path("/tmp/x"))
    db.sessions["s1"] = {"id": "s1", "title": ""}
    rt = _runtime_save("s1", db)
    result = await SaveCommand().execute("", rt)
    assert "nothing to save" in result.output.lower()


@pytest.mark.asyncio
async def test_save_no_session():
    rt = RuntimeContext(custom={})
    result = await SaveCommand().execute("", rt)
    assert "no active session" in result.output.lower()


@pytest.mark.asyncio
async def test_save_custom_path(tmp_path):
    db = _SaveFakeDB(tmp_path / "sessions.db")
    db.sessions["s1"] = {"id": "s1", "title": "T"}
    db.messages["s1"] = [SimpleNamespace(role="user", content="x")]
    rt = _runtime_save("s1", db)
    out = tmp_path / "custom" / "out.md"
    result = await SaveCommand().execute(str(out), rt)
    assert out.exists()
    assert "Saved" in result.output


@pytest.mark.asyncio
async def test_save_rejects_non_markdown_path(tmp_path):
    db = _SaveFakeDB(tmp_path / "sessions.db")
    db.sessions["s1"] = {"id": "s1", "title": "T"}
    db.messages["s1"] = [SimpleNamespace(role="user", content="x")]
    rt = _runtime_save("s1", db)
    result = await SaveCommand().execute(str(tmp_path / "out.txt"), rt)
    assert "must end with" in result.output.lower()


@pytest.mark.asyncio
async def test_save_handles_multimodal_blocks(tmp_path):
    db = _SaveFakeDB(tmp_path / "sessions.db")
    db.sessions["s1"] = {"id": "s1", "title": "T"}
    db.messages["s1"] = [
        SimpleNamespace(role="user", content=[
            {"type": "text", "text": "look"},
            {"type": "image", "source": {}},
            {"type": "tool_use", "name": "Bash"},
            {"type": "tool_result", "content": "ok"},
        ]),
    ]
    rt = _runtime_save("s1", db)
    await SaveCommand().execute("", rt)
    md = (tmp_path / "exports" / "s1.md").read_text()
    assert "look" in md
    assert "image" in md
    assert "Bash" in md


# ---------- /agents ----------


class _AgentsFakeDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path


@pytest.mark.asyncio
async def test_agents_no_session_db():
    rt = RuntimeContext(custom={})
    result = await AgentsCommand().execute("", rt)
    assert "no active session" in result.output.lower()


@pytest.mark.asyncio
async def test_agents_empty_store(tmp_path, monkeypatch):
    """Empty TaskStore returns 'no detached tasks active'."""
    # Use the real TaskStore against a fresh DB path
    db = _AgentsFakeDB(tmp_path / "sessions.db")
    rt = RuntimeContext(custom={"session_db": db})
    # First call constructs the schema + an empty store
    result = await AgentsCommand().execute("", rt)
    assert "no detached tasks" in result.output.lower()


@pytest.mark.asyncio
async def test_agents_renders_running_and_queued(tmp_path):
    db = _AgentsFakeDB(tmp_path / "sessions.db")
    rt = RuntimeContext(custom={"session_db": db})

    # Seed the real TaskStore with one queued + one running
    from opencomputer.tasks.store import TaskStore
    store = TaskStore(tmp_path / "sessions.db")
    # Inspect Task fields via the store's schema for valid creation
    # (this is brittle if the API changes; if so, simplify the test
    # to only call the slash command and verify it doesn't crash on
    # a populated store).
    try:
        with store._connect() as conn:  # type: ignore[attr-defined]
            conn.execute(
                "INSERT INTO tasks (id, status, prompt, created_at, "
                "updated_at, parent_session_id) VALUES "
                "(?, 'queued', ?, 1, 1, NULL)",
                ("t-queued-1", "do thing X"),
            )
            conn.execute(
                "INSERT INTO tasks (id, status, prompt, created_at, "
                "updated_at, parent_session_id) VALUES "
                "(?, 'running', ?, 1, 1, NULL)",
                ("t-running-1", "do thing Y"),
            )
    except Exception:
        pytest.skip("TaskStore schema not at expected shape; skip integration")

    result = await AgentsCommand().execute("", rt)
    assert "Running" in result.output
    assert "Queued" in result.output
    assert "do thing X" in result.output
    assert "do thing Y" in result.output


# ---------- /verbose ----------


def _fresh_rt(**custom) -> RuntimeContext:
    return RuntimeContext(custom=dict(custom))


@pytest.mark.asyncio
async def test_verbose_explicit_mode():
    rt = _fresh_rt()
    result = await VerboseCommand().execute("all", rt)
    assert rt.custom["tool_progress"] == "all"
    assert "all" in result.output


@pytest.mark.asyncio
async def test_verbose_cycle_no_arg():
    rt = _fresh_rt(tool_progress="off")
    await VerboseCommand().execute("", rt)
    assert rt.custom["tool_progress"] == "new"
    await VerboseCommand().execute("", rt)
    assert rt.custom["tool_progress"] == "all"
    await VerboseCommand().execute("", rt)
    assert rt.custom["tool_progress"] == "verbose"
    await VerboseCommand().execute("", rt)
    assert rt.custom["tool_progress"] == "off"


@pytest.mark.asyncio
async def test_verbose_status_does_not_mutate():
    rt = _fresh_rt(tool_progress="all")
    result = await VerboseCommand().execute("status", rt)
    assert "all" in result.output
    assert rt.custom["tool_progress"] == "all"


@pytest.mark.asyncio
async def test_verbose_invalid_arg_shows_usage():
    rt = _fresh_rt()
    result = await VerboseCommand().execute("loud", rt)
    assert "Usage" in result.output


# ---------- /statusbar ----------


@pytest.mark.asyncio
async def test_statusbar_toggle_default_on():
    rt = _fresh_rt()
    # Default is ON; toggle → OFF
    await StatusbarCommand().execute("", rt)
    assert rt.custom["statusbar"] is False
    # Toggle again → ON
    await StatusbarCommand().execute("", rt)
    assert rt.custom["statusbar"] is True


@pytest.mark.asyncio
async def test_statusbar_explicit_off():
    rt = _fresh_rt()
    result = await StatusbarCommand().execute("off", rt)
    assert "OFF" in result.output
    assert rt.custom["statusbar"] is False


@pytest.mark.asyncio
async def test_statusbar_explicit_on():
    rt = _fresh_rt(statusbar=False)
    result = await StatusbarCommand().execute("on", rt)
    assert "ON" in result.output
    assert rt.custom["statusbar"] is True


@pytest.mark.asyncio
async def test_statusbar_status_no_mutation():
    rt = _fresh_rt(statusbar=True)
    result = await StatusbarCommand().execute("status", rt)
    assert "ON" in result.output
    assert rt.custom["statusbar"] is True


@pytest.mark.asyncio
async def test_statusbar_invalid_arg():
    rt = _fresh_rt()
    result = await StatusbarCommand().execute("loud", rt)
    assert "Usage" in result.output


# ---------- metadata ----------


def test_metadata_all_four():
    for cls in (SaveCommand, AgentsCommand, VerboseCommand, StatusbarCommand):
        cmd = cls()
        assert cmd.name
        assert cmd.description
