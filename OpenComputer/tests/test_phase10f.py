"""Phase 10f — Memory baseline completion.

Tests are organized by sub-phase (10f.A — 10f.J). Honcho plugin tests live in
tests/test_phase10f_honcho.py.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ─── 10f.A — MemoryManager hardening ───────────────────────────────────


class TestMemoryManagerHardening:
    """Atomic writes, locks, USER.md, replace/remove, backup/restore, stats."""

    @pytest.fixture
    def mm(self, tmp_path: Path):
        from opencomputer.agent.memory import MemoryManager

        return MemoryManager(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
            skills_path=tmp_path / "skills",
            memory_char_limit=2000,
            user_char_limit=1000,
        )

    # USER.md round-trip

    def test_read_user_returns_empty_when_missing(self, mm):
        assert mm.read_user() == ""

    def test_append_user_then_read(self, mm):
        mm.append_user("user prefers concise output")
        text = mm.read_user()
        assert "user prefers concise output" in text

    # replace / remove on declarative

    def test_replace_declarative_substitutes(self, mm):
        mm.append_declarative("the moon is made of cheese")
        assert mm.replace_declarative("cheese", "rock") is True
        assert "rock" in mm.read_declarative()
        assert "cheese" not in mm.read_declarative()

    def test_replace_declarative_returns_false_when_not_found(self, mm):
        mm.append_declarative("hello")
        assert mm.replace_declarative("nonexistent", "new") is False

    def test_remove_declarative_removes_block(self, mm):
        mm.append_declarative("line one")
        mm.append_declarative("line two")
        assert mm.remove_declarative("line one") is True
        remaining = mm.read_declarative()
        assert "line one" not in remaining
        assert "line two" in remaining

    # replace / remove on user

    def test_replace_user_substitutes(self, mm):
        mm.append_user("user likes blue")
        assert mm.replace_user("blue", "green") is True
        assert "green" in mm.read_user()

    def test_remove_user_deletes_block(self, mm):
        mm.append_user("ephemeral")
        mm.append_user("permanent")
        mm.remove_user("ephemeral")
        assert "ephemeral" not in mm.read_user()
        assert "permanent" in mm.read_user()

    # character limit enforcement

    def test_append_over_limit_raises(self, mm):
        from opencomputer.agent.memory import MemoryTooLargeError

        giant = "x" * 5000
        with pytest.raises(MemoryTooLargeError):
            mm.append_declarative(giant)

    def test_user_append_over_limit_raises(self, mm):
        from opencomputer.agent.memory import MemoryTooLargeError

        with pytest.raises(MemoryTooLargeError):
            mm.append_user("y" * 3000)

    # backup + restore

    def test_backup_and_restore(self, mm):
        mm.append_declarative("original content")
        # Next write triggers backup of the prior state.
        mm.append_declarative("newer content")
        assert Path(str(mm.declarative_path) + ".bak").exists()
        mm.restore_backup("memory")
        text = mm.read_declarative()
        assert "original content" in text
        # "newer content" was added after the backup was captured, so it goes
        # away on restore.
        assert "newer content" not in text

    def test_restore_backup_user(self, mm):
        mm.append_user("v1")
        mm.append_user("v2")
        mm.restore_backup("user")
        assert "v1" in mm.read_user()
        assert "v2" not in mm.read_user()

    # stats

    def test_stats_reports_sizes(self, mm):
        mm.append_declarative("abc")
        mm.append_user("xyz123")
        stats = mm.stats()
        assert stats["memory_chars"] >= 3
        assert stats["user_chars"] >= 6
        assert stats["memory_char_limit"] == 2000
        assert stats["user_char_limit"] == 1000

    # atomic + thread-safe writes

    def test_concurrent_appends_do_not_corrupt(self, mm):
        # Each entry is short and unique. With proper locking, every entry
        # should be readable after all threads finish.
        entries = [f"entry-{i}" for i in range(20)]

        def worker(e: str) -> None:
            mm.append_declarative(e)

        threads = [threading.Thread(target=worker, args=(e,)) for e in entries]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = mm.read_declarative()
        for e in entries:
            assert e in final, f"lost entry: {e}"

    def test_atomic_write_never_leaves_partial_file(self, mm, monkeypatch):
        # Simulate interruption: patch os.replace to raise after the tmp file
        # is created. The original MEMORY.md should remain intact.
        import os

        mm.append_declarative("original")
        original_bytes = mm.declarative_path.read_bytes()

        real_replace = os.replace

        def boom(src, dst):  # type: ignore
            raise RuntimeError("simulated interrupt")

        monkeypatch.setattr(os, "replace", boom)
        try:
            mm.append_declarative("new entry")  # should raise, but atomically
        except RuntimeError:
            pass
        monkeypatch.setattr(os, "replace", real_replace)

        # Original preserved.
        assert mm.declarative_path.read_bytes() == original_bytes


# ─── 10f.B — MemoryContext + MemoryBridge ──────────────────────────────


class TestMemoryContext:
    """Shared-deps bag passed to tools + injection sites."""

    def test_construct_with_required_fields(self, tmp_path):
        from opencomputer.agent.memory import MemoryManager
        from opencomputer.agent.memory_context import MemoryContext
        from opencomputer.agent.state import SessionDB

        mm = MemoryManager(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
            skills_path=tmp_path / "skills",
        )
        db = SessionDB(tmp_path / "sessions.db")
        ctx = MemoryContext(
            manager=mm,
            db=db,
            session_id_provider=lambda: "sess-1",
        )
        assert ctx.manager is mm
        assert ctx.db is db
        assert ctx.session_id_provider() == "sess-1"
        assert ctx.provider is None  # default

    def test_provider_optional(self, tmp_path):
        from opencomputer.agent.memory import MemoryManager
        from opencomputer.agent.memory_context import MemoryContext
        from opencomputer.agent.state import SessionDB

        mm = MemoryManager(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
            skills_path=tmp_path / "skills",
        )
        db = SessionDB(tmp_path / "sessions.db")
        sentinel = object()
        ctx = MemoryContext(
            manager=mm,
            db=db,
            session_id_provider=lambda: "s",
            provider=sentinel,  # type: ignore[arg-type]
        )
        assert ctx.provider is sentinel


class _FakeProvider:
    """Minimal fake MemoryProvider for bridge tests."""

    provider_id = "fake:test"

    def __init__(
        self,
        *,
        healthy: bool = True,
        prefetch_returns: str | None = "fake ctx",
        prefetch_raises: bool = False,
    ) -> None:
        self._healthy = healthy
        self._prefetch_returns = prefetch_returns
        self._prefetch_raises = prefetch_raises
        self.sync_turn_calls: list[tuple[str, str, int]] = []
        self.health_checks = 0
        self.prefetch_calls = 0

    async def health_check(self) -> bool:
        self.health_checks += 1
        return self._healthy

    async def prefetch(self, query: str, turn_index: int) -> str | None:
        self.prefetch_calls += 1
        if self._prefetch_raises:
            raise RuntimeError("provider blew up")
        return self._prefetch_returns

    async def sync_turn(self, user: str, assistant: str, turn_index: int) -> None:
        self.sync_turn_calls.append((user, assistant, turn_index))


class TestMemoryBridge:
    """Exception-safe orchestrator around optional MemoryProvider."""

    def _make_ctx(self, tmp_path, provider=None):
        from opencomputer.agent.memory import MemoryManager
        from opencomputer.agent.memory_context import MemoryContext
        from opencomputer.agent.state import SessionDB

        mm = MemoryManager(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
            skills_path=tmp_path / "skills",
        )
        return MemoryContext(
            manager=mm,
            db=SessionDB(tmp_path / "sessions.db"),
            session_id_provider=lambda: "s",
            provider=provider,
        )

    def test_no_provider_is_no_op(self, tmp_path):
        from opencomputer.agent.memory_bridge import MemoryBridge

        bridge = MemoryBridge(self._make_ctx(tmp_path))
        assert asyncio.run(bridge.prefetch("q", turn_index=0)) is None
        # sync_turn does nothing, returns cleanly
        asyncio.run(bridge.sync_turn("u", "a", turn_index=0))
        # check_health returns True (nothing to fail)
        assert asyncio.run(bridge.check_health()) is True

    def test_provider_prefetch_forwards(self, tmp_path):
        from opencomputer.agent.memory_bridge import MemoryBridge

        provider = _FakeProvider(prefetch_returns="hello")
        bridge = MemoryBridge(self._make_ctx(tmp_path, provider=provider))
        result = asyncio.run(bridge.prefetch("q", turn_index=3))
        assert result == "hello"
        assert provider.prefetch_calls == 1

    def test_provider_sync_turn_forwards(self, tmp_path):
        from opencomputer.agent.memory_bridge import MemoryBridge

        provider = _FakeProvider()
        bridge = MemoryBridge(self._make_ctx(tmp_path, provider=provider))
        asyncio.run(bridge.sync_turn("user msg", "asst msg", turn_index=2))
        assert provider.sync_turn_calls == [("user msg", "asst msg", 2)]

    def test_health_check_failure_disables_provider(self, tmp_path):
        from opencomputer.agent.memory_bridge import MemoryBridge

        provider = _FakeProvider(healthy=False)
        bridge = MemoryBridge(self._make_ctx(tmp_path, provider=provider))
        assert asyncio.run(bridge.check_health()) is False
        # After failed health check, prefetch short-circuits to None
        assert asyncio.run(bridge.prefetch("q", turn_index=0)) is None

    def test_three_consecutive_failures_disable_provider(self, tmp_path):
        from opencomputer.agent.memory_bridge import MemoryBridge

        provider = _FakeProvider(prefetch_raises=True)
        bridge = MemoryBridge(self._make_ctx(tmp_path, provider=provider))
        # first three prefetch calls raise internally but return None
        for i in range(3):
            result = asyncio.run(bridge.prefetch("q", turn_index=i))
            assert result is None
        # 4th call should NOT reach provider (disabled)
        calls_before = provider.prefetch_calls
        assert asyncio.run(bridge.prefetch("q", turn_index=3)) is None
        assert provider.prefetch_calls == calls_before, "provider should be disabled"

    def test_sync_turn_exception_is_swallowed(self, tmp_path):
        from opencomputer.agent.memory_bridge import MemoryBridge

        class BoomProvider(_FakeProvider):
            async def sync_turn(self, user, assistant, turn_index):
                raise RuntimeError("sync failed")

        bridge = MemoryBridge(self._make_ctx(tmp_path, provider=BoomProvider()))
        # Must NOT raise.
        asyncio.run(bridge.sync_turn("u", "a", turn_index=0))


# ─── 10f.C — PromptBuilder base-prompt injection ───────────────────────


class TestPromptBuilderMemoryInjection:
    """PromptBuilder renders declarative memory + user profile into the base."""

    def test_memory_block_rendered_when_present(self):
        from opencomputer.agent.prompt_builder import PromptBuilder

        out = PromptBuilder().build(declarative_memory="Saksham prefers concise output.")
        assert "<memory>" in out
        assert "</memory>" in out
        assert "Saksham prefers concise output." in out

    def test_user_profile_block_rendered_when_present(self):
        from opencomputer.agent.prompt_builder import PromptBuilder

        out = PromptBuilder().build(user_profile="User works in Mumbai timezone.")
        assert "<user-profile>" in out
        assert "User works in Mumbai timezone." in out

    def test_no_memory_blocks_when_empty(self):
        from opencomputer.agent.prompt_builder import PromptBuilder

        out = PromptBuilder().build()
        assert "<memory>" not in out
        assert "<user-profile>" not in out

    def test_both_blocks_rendered_together(self):
        from opencomputer.agent.prompt_builder import PromptBuilder

        out = PromptBuilder().build(
            declarative_memory="fact-1",
            user_profile="pref-1",
        )
        assert "<memory>" in out
        assert "fact-1" in out
        assert "<user-profile>" in out
        assert "pref-1" in out
        # memory block comes before user-profile block (highest salience first)
        assert out.index("<memory>") < out.index("<user-profile>")

    def test_over_limit_memory_is_truncated_from_top(self):
        from opencomputer.agent.prompt_builder import PromptBuilder

        # Give 1000 chars of "line-XXX\n" entries; limit to 200. Older lines
        # (lower XXX numbers) should be dropped; newer ones preserved.
        lines = [f"line-{i:03d}" for i in range(100)]
        big = "\n".join(lines)
        out = PromptBuilder().build(declarative_memory=big, memory_char_limit=200)
        # Truncation marker appears when content was cut.
        assert "[earlier entries truncated]" in out
        # Recent entries survive
        assert "line-099" in out
        # Earliest entries are gone
        assert "line-000" not in out

    def test_under_limit_content_not_truncated(self):
        from opencomputer.agent.prompt_builder import PromptBuilder

        out = PromptBuilder().build(declarative_memory="short", memory_char_limit=4000)
        assert "[earlier entries truncated]" not in out
        assert "short" in out

    def test_existing_skills_arg_still_works(self):
        """Backward compatibility — existing callers with only skills= must work."""
        from opencomputer.agent.prompt_builder import PromptBuilder

        out = PromptBuilder().build()  # no args at all
        assert "OpenComputer" in out  # the base template content still renders


# ─── 10f.F — MemoryProvider ABC + InjectionContext.turn_index ──────────


class TestMemoryProviderABC:
    def test_imports_from_public_sdk(self):
        from plugin_sdk import MemoryProvider

        assert MemoryProvider is not None

    def test_minimal_subclass_instantiates(self):
        from plugin_sdk.core import ToolCall, ToolResult
        from plugin_sdk.memory import MemoryProvider

        class _Stub(MemoryProvider):
            provider_id = "stub:test"

            def tool_schemas(self):
                return []

            async def handle_tool_call(self, call: ToolCall) -> ToolResult:
                return ToolResult(tool_call_id=call.id, content="ok", is_error=False)

            async def prefetch(self, query: str, turn_index: int) -> str | None:
                return None

            async def sync_turn(self, user: str, assistant: str, turn_index: int):
                return None

            async def health_check(self) -> bool:
                return True

        s = _Stub()
        assert s.provider_id == "stub:test"
        assert s.tool_schemas() == []
        assert asyncio.run(s.health_check()) is True

    def test_default_lifecycle_methods_are_no_op(self):
        from plugin_sdk.memory import MemoryProvider

        class _Stub(MemoryProvider):
            provider_id = "stub:test"

            def tool_schemas(self):
                return []

            async def handle_tool_call(self, call):
                pass

            async def prefetch(self, query, turn_index):
                return None

            async def sync_turn(self, user, assistant, turn_index):
                pass

            async def health_check(self):
                return True

        # on_session_start and on_session_end have default no-op implementations
        asyncio.run(_Stub().on_session_start("s1"))
        asyncio.run(_Stub().on_session_end("s1"))

    def test_provider_priority_default(self):
        from plugin_sdk.memory import MemoryProvider

        class _Stub(MemoryProvider):
            provider_id = "stub:test"

            def tool_schemas(self):
                return []

            async def handle_tool_call(self, call):
                pass

            async def prefetch(self, q, t):
                return None

            async def sync_turn(self, u, a, t):
                pass

            async def health_check(self):
                return True

        assert _Stub().provider_priority == 100


class TestInjectionContextTurnIndex:
    def test_turn_index_default_zero(self):
        from plugin_sdk.injection import InjectionContext
        from plugin_sdk.runtime_context import RuntimeContext

        ctx = InjectionContext(
            messages=(),
            runtime=RuntimeContext(plan_mode=False, yolo_mode=False),
            session_id="s",
        )
        assert ctx.turn_index == 0

    def test_turn_index_settable(self):
        from plugin_sdk.injection import InjectionContext
        from plugin_sdk.runtime_context import RuntimeContext

        ctx = InjectionContext(
            messages=(),
            runtime=RuntimeContext(plan_mode=False, yolo_mode=False),
            session_id="s",
            turn_index=7,
        )
        assert ctx.turn_index == 7


# ─── 10f.G — PluginAPI.register_memory_provider ────────────────────────


class TestPluginAPIMemoryProvider:
    def _make_api(self):
        from opencomputer.plugins.loader import PluginAPI

        return PluginAPI(
            tool_registry=MagicMock(),
            hook_engine=MagicMock(),
            provider_registry={},
            channel_registry={},
            injection_engine=MagicMock(),
        )

    def test_register_stores_provider(self):
        from plugin_sdk.memory import MemoryProvider

        class _Stub(MemoryProvider):
            provider_id = "stub:one"

            def tool_schemas(self):
                return []

            async def handle_tool_call(self, call):
                pass

            async def prefetch(self, q, t):
                return None

            async def sync_turn(self, u, a, t):
                pass

            async def health_check(self):
                return True

        api = self._make_api()
        assert api.memory_provider is None
        p = _Stub()
        api.register_memory_provider(p)
        assert api.memory_provider is p

    def test_second_registration_raises(self):
        from plugin_sdk.memory import MemoryProvider

        class _Stub(MemoryProvider):
            provider_id = "stub:two"

            def tool_schemas(self):
                return []

            async def handle_tool_call(self, call):
                pass

            async def prefetch(self, q, t):
                return None

            async def sync_turn(self, u, a, t):
                pass

            async def health_check(self):
                return True

        api = self._make_api()
        api.register_memory_provider(_Stub())
        with pytest.raises(ValueError, match="already registered"):
            api.register_memory_provider(_Stub())

    def test_non_memory_provider_rejected(self):
        api = self._make_api()
        with pytest.raises(TypeError, match="MemoryProvider"):
            api.register_memory_provider("not a provider")  # type: ignore[arg-type]


# ─── 10f.D — Memory tool ────────────────────────────────────────────────


class TestMemoryTool:
    @pytest.fixture
    def ctx(self, tmp_path):
        from opencomputer.agent.memory import MemoryManager
        from opencomputer.agent.memory_context import MemoryContext
        from opencomputer.agent.state import SessionDB

        mm = MemoryManager(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
            skills_path=tmp_path / "skills",
            memory_char_limit=2000,
            user_char_limit=1000,
        )
        return MemoryContext(
            manager=mm,
            db=SessionDB(tmp_path / "sessions.db"),
            session_id_provider=lambda: "test",
        )

    def _call(self, tool, **args):
        from plugin_sdk.core import ToolCall

        return asyncio.run(
            tool.execute(ToolCall(id="tc-1", name="Memory", arguments=args))
        )

    def test_schema_exposes_name_and_enums(self, ctx):
        from opencomputer.tools.memory_tool import MemoryTool

        s = MemoryTool(ctx).schema
        assert s.name == "Memory"
        props = s.parameters["properties"]
        assert set(props["action"]["enum"]) == {"add", "replace", "remove", "read"}
        assert set(props["target"]["enum"]) == {"memory", "user"}

    def test_add_memory(self, ctx):
        from opencomputer.tools.memory_tool import MemoryTool

        r = self._call(
            MemoryTool(ctx),
            action="add",
            target="memory",
            content="Saksham prefers concise output",
        )
        assert r.is_error is False
        assert "Saksham prefers concise output" in ctx.manager.read_declarative()

    def test_add_user(self, ctx):
        from opencomputer.tools.memory_tool import MemoryTool

        r = self._call(
            MemoryTool(ctx),
            action="add",
            target="user",
            content="user's timezone is Asia/Kolkata",
        )
        assert r.is_error is False
        assert "Asia/Kolkata" in ctx.manager.read_user()

    def test_replace_memory(self, ctx):
        from opencomputer.tools.memory_tool import MemoryTool

        ctx.manager.append_declarative("the moon is cheese")
        r = self._call(
            MemoryTool(ctx),
            action="replace",
            target="memory",
            old="cheese",
            new="rock",
        )
        assert r.is_error is False
        assert "rock" in ctx.manager.read_declarative()

    def test_replace_returns_error_when_not_found(self, ctx):
        from opencomputer.tools.memory_tool import MemoryTool

        r = self._call(
            MemoryTool(ctx),
            action="replace",
            target="memory",
            old="nonexistent",
            new="z",
        )
        assert r.is_error is True
        assert "not found" in r.content.lower()

    def test_remove_memory(self, ctx):
        from opencomputer.tools.memory_tool import MemoryTool

        ctx.manager.append_declarative("temporary note")
        r = self._call(
            MemoryTool(ctx),
            action="remove",
            target="memory",
            content="temporary note",
        )
        assert r.is_error is False
        assert "temporary note" not in ctx.manager.read_declarative()

    def test_read_memory(self, ctx):
        from opencomputer.tools.memory_tool import MemoryTool

        ctx.manager.append_declarative("entry-one")
        ctx.manager.append_user("pref-one")
        r_mem = self._call(MemoryTool(ctx), action="read", target="memory")
        r_usr = self._call(MemoryTool(ctx), action="read", target="user")
        assert r_mem.is_error is False and "entry-one" in r_mem.content
        assert r_usr.is_error is False and "pref-one" in r_usr.content

    def test_over_limit_returns_error_not_exception(self, ctx):
        from opencomputer.tools.memory_tool import MemoryTool

        r = self._call(
            MemoryTool(ctx),
            action="add",
            target="memory",
            content="x" * 5000,
        )
        assert r.is_error is True
        assert "limit" in r.content.lower() or "too large" in r.content.lower()

    def test_invalid_action_returns_error(self, ctx):
        from opencomputer.tools.memory_tool import MemoryTool

        r = self._call(MemoryTool(ctx), action="nuke", target="memory")
        assert r.is_error is True
        assert "nuke" in r.content or "unknown" in r.content.lower()

    def test_invalid_target_returns_error(self, ctx):
        from opencomputer.tools.memory_tool import MemoryTool

        r = self._call(
            MemoryTool(ctx), action="add", target="wrong", content="x"
        )
        assert r.is_error is True


# ─── 10f.E — SessionSearch tool + SessionDB.search_messages() ──────────


class TestSessionDBSearchMessages:
    def test_search_messages_returns_full_content(self, tmp_path):
        from opencomputer.agent.state import SessionDB
        from plugin_sdk.core import Message

        db = SessionDB(tmp_path / "s.db")
        db.create_session("sess-1", platform="cli", model="opus")
        db.append_message(
            "sess-1", Message(role="user", content="tell me about python decorators")
        )
        db.append_message(
            "sess-1", Message(role="assistant", content="decorators wrap functions")
        )
        rows = db.search_messages("python", limit=5)
        assert len(rows) >= 1
        # Result rows include full message content, not just snippet.
        match = next(r for r in rows if "python" in r["content"])
        assert match["session_id"] == "sess-1"
        assert match["role"] in {"user", "assistant"}
        assert "timestamp" in match

    def test_search_messages_empty_query_returns_empty(self, tmp_path):
        from opencomputer.agent.state import SessionDB

        db = SessionDB(tmp_path / "s.db")
        assert db.search_messages("", limit=5) == []

    def test_search_messages_limit_respected(self, tmp_path):
        from opencomputer.agent.state import SessionDB
        from plugin_sdk.core import Message

        db = SessionDB(tmp_path / "s.db")
        db.create_session("s", platform="cli", model="opus")
        for i in range(20):
            db.append_message(
                "s", Message(role="user", content=f"query topic alpha {i}")
            )
        rows = db.search_messages("alpha", limit=3)
        assert len(rows) == 3


class TestSessionSearchTool:
    @pytest.fixture
    def ctx(self, tmp_path):
        from opencomputer.agent.memory import MemoryManager
        from opencomputer.agent.memory_context import MemoryContext
        from opencomputer.agent.state import SessionDB
        from plugin_sdk.core import Message

        db = SessionDB(tmp_path / "s.db")
        db.create_session("s1", platform="cli", model="opus")
        db.append_message(
            "s1", Message(role="user", content="how does asyncio work?")
        )
        db.append_message(
            "s1",
            Message(
                role="assistant",
                content="asyncio manages cooperative concurrency via event loop",
            ),
        )
        mm = MemoryManager(
            declarative_path=tmp_path / "M.md",
            user_path=tmp_path / "U.md",
            skills_path=tmp_path / "skills",
        )
        return MemoryContext(manager=mm, db=db, session_id_provider=lambda: "s1")

    def _call(self, tool, **args):
        from plugin_sdk.core import ToolCall

        return asyncio.run(
            tool.execute(ToolCall(id="tc", name="SessionSearch", arguments=args))
        )

    def test_schema_has_query_param(self, ctx):
        from opencomputer.tools.session_search_tool import SessionSearchTool

        s = SessionSearchTool(ctx).schema
        assert s.name == "SessionSearch"
        assert "query" in s.parameters["properties"]

    def test_finds_matching_messages(self, ctx):
        from opencomputer.tools.session_search_tool import SessionSearchTool

        r = self._call(SessionSearchTool(ctx), query="asyncio")
        assert r.is_error is False
        assert "asyncio" in r.content

    def test_empty_query_returns_error(self, ctx):
        from opencomputer.tools.session_search_tool import SessionSearchTool

        r = self._call(SessionSearchTool(ctx), query="")
        assert r.is_error is True

    def test_no_matches_is_not_an_error(self, ctx):
        from opencomputer.tools.session_search_tool import SessionSearchTool

        r = self._call(SessionSearchTool(ctx), query="entirelyunknownword42")
        # No match is a valid empty result, not an error.
        assert r.is_error is False
        assert "no matches" in r.content.lower()

    def test_limit_defaults_to_10_and_max_50(self, ctx):
        from opencomputer.tools.session_search_tool import SessionSearchTool

        s = SessionSearchTool(ctx).schema
        props = s.parameters["properties"]
        assert props["limit"]["default"] == 10
        assert props["limit"]["maximum"] == 50


# ─── 10f.H — AgentLoop wiring ──────────────────────────────────────────


class TestAgentLoopWiring:
    """Integration: AgentLoop builds MemoryContext + bridge and wires tools."""

    def _loop(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock

        from opencomputer.agent.config import (
            Config,
            LoopConfig,
            MemoryConfig,
            ModelConfig,
            SessionConfig,
        )
        from opencomputer.agent.loop import AgentLoop
        from plugin_sdk.core import Message
        from plugin_sdk.provider_contract import ProviderResponse, Usage

        cfg = Config(
            model=ModelConfig(
                provider="mock",
                model="mock",
                max_tokens=1024,
                temperature=0.0,
            ),
            loop=LoopConfig(max_iterations=2, parallel_tools=False),
            session=SessionConfig(db_path=tmp_path / "s.db"),
            memory=MemoryConfig(
                declarative_path=tmp_path / "M.md",
                user_path=tmp_path / "U.md",
                skills_path=tmp_path / "skills",
                memory_char_limit=500,
                user_char_limit=250,
            ),
        )
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=ProviderResponse(
                message=Message(role="assistant", content="ok"),
                stop_reason="end_turn",
                usage=Usage(10, 3),
            )
        )
        return AgentLoop(provider=provider, config=cfg)

    def test_memory_context_is_built(self, tmp_path):
        loop = self._loop(tmp_path)
        assert hasattr(loop, "memory_context")
        assert loop.memory_context.manager is loop.memory
        assert loop.memory_context.db is loop.db

    def test_memory_bridge_is_built(self, tmp_path):
        from opencomputer.agent.memory_bridge import MemoryBridge

        loop = self._loop(tmp_path)
        assert hasattr(loop, "memory_bridge")
        assert isinstance(loop.memory_bridge, MemoryBridge)

    def test_memory_manager_has_user_path_and_limits(self, tmp_path):
        loop = self._loop(tmp_path)
        assert loop.memory.user_path == tmp_path / "U.md"
        assert loop.memory.memory_char_limit == 500
        assert loop.memory.user_char_limit == 250

    def test_memory_tool_registered_in_global_registry(self, tmp_path):
        from opencomputer.tools.registry import registry

        self._loop(tmp_path)
        # The loop's __init__ registered the tool.
        assert "Memory" in registry.names()

    def test_session_search_tool_registered(self, tmp_path):
        from opencomputer.tools.registry import registry

        self._loop(tmp_path)
        assert "SessionSearch" in registry.names()

    def test_system_prompt_includes_memory_and_user(self, tmp_path):
        import asyncio

        loop = self._loop(tmp_path)
        # Seed memory + user files
        loop.memory.append_declarative("distinctive-memory-string")
        loop.memory.append_user("distinctive-user-string")

        result = asyncio.run(loop.run_conversation("hello"))

        # The system prompt passed to provider.complete should contain both
        # memory and user-profile blocks.
        call_args = loop.provider.complete.call_args
        system = call_args.kwargs.get("system") or call_args.args[1]
        assert "distinctive-memory-string" in system
        assert "distinctive-user-string" in system


# ─── 10f.I — opencomputer memory CLI ────────────────────────────────────


class TestMemoryCLI:
    """Typer CLI commands: show/edit/search/stats/prune/restore."""

    def _runner(self):
        from typer.testing import CliRunner

        return CliRunner()

    def _isolated_home(self, tmp_path, monkeypatch):
        """Point OPENCOMPUTER_HOME at tmp_path so CLI doesn't touch real files."""
        monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
        # Ensure config module picks up the new home
        import importlib

        from opencomputer.agent import config as cfg_mod

        importlib.reload(cfg_mod)

    def test_show_empty_memory(self, tmp_path, monkeypatch):
        from opencomputer.cli_memory import memory_app

        self._isolated_home(tmp_path, monkeypatch)
        result = self._runner().invoke(memory_app, ["show"])
        assert result.exit_code == 0
        assert "empty" in result.stdout.lower()

    def test_show_populated_memory(self, tmp_path, monkeypatch):
        from opencomputer.cli_memory import memory_app

        self._isolated_home(tmp_path, monkeypatch)
        # Seed MEMORY.md directly
        (tmp_path / "MEMORY.md").write_text("distinctive memory line\n")
        result = self._runner().invoke(memory_app, ["show"])
        assert result.exit_code == 0
        assert "distinctive memory line" in result.stdout

    def test_show_user_flag(self, tmp_path, monkeypatch):
        from opencomputer.cli_memory import memory_app

        self._isolated_home(tmp_path, monkeypatch)
        (tmp_path / "USER.md").write_text("user preference X\n")
        result = self._runner().invoke(memory_app, ["show", "--user"])
        assert result.exit_code == 0
        assert "user preference X" in result.stdout

    def test_stats_output(self, tmp_path, monkeypatch):
        from opencomputer.cli_memory import memory_app

        self._isolated_home(tmp_path, monkeypatch)
        (tmp_path / "MEMORY.md").write_text("hello world\n")
        result = self._runner().invoke(memory_app, ["stats"])
        assert result.exit_code == 0
        # Stats output mentions both files and a char count
        out = result.stdout
        assert "MEMORY.md" in out
        assert "USER.md" in out
        assert "chars" in out

    def test_search_no_matches(self, tmp_path, monkeypatch):
        from opencomputer.cli_memory import memory_app

        self._isolated_home(tmp_path, monkeypatch)
        result = self._runner().invoke(memory_app, ["search", "neverfound"])
        assert result.exit_code == 0
        assert "no match" in result.stdout.lower()

    def test_restore_without_backup_errors(self, tmp_path, monkeypatch):
        from opencomputer.cli_memory import memory_app

        self._isolated_home(tmp_path, monkeypatch)
        result = self._runner().invoke(memory_app, ["restore"])
        assert result.exit_code == 1
        assert "no backup" in result.stdout.lower()
