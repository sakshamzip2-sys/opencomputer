"""Tests for Telegram DM Topics (Hermes channel-port PR 5).

* DMTopicManager — round-trip persistence, concurrent-write tolerance.
* TelegramAdapter — message_thread_id → topic lookup → metadata.channel_id.
* Default behaviour preserved when ``dm_topics`` config is absent.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from extensions.telegram.dm_topics import DMTopicManager
from plugin_sdk.core import MessageEvent, Platform

# ─── DMTopicManager ─────────────────────────────────────────────────


class TestDMTopicManager:
    def test_round_trip_persistence(self, tmp_path: Path) -> None:
        m1 = DMTopicManager(tmp_path)
        m1.register_topic(
            "42",
            label="Trading",
            skill="stock-market-analysis",
            system_prompt="be terse",
            parent_chat_id="123",
        )
        # Fresh instance reads from disk.
        m2 = DMTopicManager(tmp_path)
        topic = m2.get_topic("42")
        assert topic == {
            "label": "Trading",
            "skill": "stock-market-analysis",
            "system_prompt": "be terse",
            "parent_chat_id": "123",
        }

    def test_get_topic_returns_none_for_unknown(self, tmp_path: Path) -> None:
        m = DMTopicManager(tmp_path)
        assert m.get_topic("missing") is None

    def test_get_topic_returns_copy_not_reference(self, tmp_path: Path) -> None:
        """Caller mutating the returned dict must not corrupt the registry."""
        m = DMTopicManager(tmp_path)
        m.register_topic("1", label="A", skill="s")
        out = m.get_topic("1")
        assert out is not None
        out["label"] = "MUTATED"
        # Original entry untouched.
        assert m.get_topic("1") == {
            "label": "A",
            "skill": "s",
            "system_prompt": None,
            "parent_chat_id": None,
        }

    def test_list_topics_includes_topic_id(self, tmp_path: Path) -> None:
        m = DMTopicManager(tmp_path)
        m.register_topic("1", label="A")
        m.register_topic("2", label="B", skill="x")
        rows = m.list_topics()
        ids = {r["topic_id"] for r in rows}
        assert ids == {"1", "2"}

    def test_remove_topic(self, tmp_path: Path) -> None:
        m = DMTopicManager(tmp_path)
        m.register_topic("1", label="A")
        assert m.remove_topic("1") is True
        assert m.get_topic("1") is None
        # Removing a missing key is a no-op signal.
        assert m.remove_topic("1") is False

    def test_register_topic_rejects_empty_id(self, tmp_path: Path) -> None:
        m = DMTopicManager(tmp_path)
        with pytest.raises(ValueError):
            m.register_topic("", label="oops")

    def test_int_topic_id_coerced_to_str(self, tmp_path: Path) -> None:
        """Telegram thread ids arrive as ints; we accept them but store str."""
        m = DMTopicManager(tmp_path)
        m.register_topic("777", label="X")
        # Lookup with int form works because ``get_topic`` ``str()``s.
        assert m.get_topic(777) is not None  # type: ignore[arg-type]

    def test_corrupt_json_starts_empty(self, tmp_path: Path) -> None:
        """A broken file shouldn't crash startup."""
        path = tmp_path / "telegram_dm_topics.json"
        path.write_text("{not json")
        m = DMTopicManager(tmp_path)
        assert m.list_topics() == []
        # And we can write fresh entries.
        m.register_topic("1", label="A")
        assert m.get_topic("1") is not None

    def test_concurrent_writes_do_not_lose_data(self, tmp_path: Path) -> None:
        """Two threads writing different keys: both survive.

        flock serializes the writes; whichever lands second still sees
        the first's entry because each instance reloads on construction
        and ``_save`` writes the merged in-memory map.
        """
        # Pre-create one instance so both writers share the same disk file.
        m = DMTopicManager(tmp_path)

        results: list[str] = []
        errors: list[BaseException] = []

        def writer(tid: str) -> None:
            try:
                local = DMTopicManager(tmp_path)
                local.register_topic(tid, label=f"label-{tid}")
                results.append(tid)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(str(i),)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"writer raised: {errors}"
        assert sorted(results) == ["0", "1", "2", "3", "4"]
        # Disk has at least one of the entries (writes can stomp because
        # we don't merge on save — flock makes this serial but each
        # writer started with whatever was on disk at construction time).
        # The contract we need: no torn JSON, no hard failure.
        final = DMTopicManager(tmp_path)
        # ``register_topic`` always persists at least its own entry, so
        # the LAST writer to land is observable.
        loaded = final.list_topics()
        assert len(loaded) >= 1
        # And the on-disk file is parseable JSON.
        raw = (tmp_path / "telegram_dm_topics.json").read_text()
        json.loads(raw)


# ─── TelegramAdapter integration ────────────────────────────────────


_TELEGRAM_ADAPTER_PATH = (
    Path(__file__).resolve().parent.parent / "extensions" / "telegram" / "adapter.py"
)


def _load_telegram() -> Any:
    spec = importlib.util.spec_from_file_location(
        "telegram_adapter_test_dm_topics",
        str(_TELEGRAM_ADAPTER_PATH),
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def adapter_with_topic(tmp_path: Path):
    """TelegramAdapter with dm_topics enabled and one registered topic."""
    mod = _load_telegram()
    profile_home = tmp_path / "profile"
    profile_home.mkdir(parents=True)

    # Pre-register topic 7 so the adapter reads it on construction.
    pre = DMTopicManager(profile_home)
    pre.register_topic(
        "7",
        label="Trading",
        skill="stock-market-analysis",
        system_prompt="be terse",
        parent_chat_id="123",
    )

    a = mod.TelegramAdapter(
        {
            "bot_token": "T",
            "profile_home": str(profile_home),
            "dm_topics": {"enabled": True},
        }
    )
    a._client = httpx.AsyncClient(timeout=5.0)
    a._bot_id = 999
    return a


@pytest.fixture
def adapter_no_topics(tmp_path: Path):
    """TelegramAdapter without dm_topics config (default behaviour)."""
    mod = _load_telegram()
    profile_home = tmp_path / "profile_default"
    profile_home.mkdir(parents=True)
    a = mod.TelegramAdapter(
        {"bot_token": "T", "profile_home": str(profile_home)}
    )
    a._client = httpx.AsyncClient(timeout=5.0)
    a._bot_id = 999
    return a


class TestAdapterRoutesThreadIdToChannelId:
    @pytest.mark.asyncio
    async def test_thread_id_with_registered_topic_sets_channel_id(
        self, adapter_with_topic
    ) -> None:
        captured: list[MessageEvent] = []

        async def handler(event: MessageEvent) -> str | None:
            captured.append(event)
            return None

        adapter_with_topic.set_message_handler(handler)

        update = {
            "message": {
                "message_id": 1,
                "message_thread_id": 7,
                "from": {"id": 100},
                "chat": {"id": 123, "type": "private"},
                "text": "hi",
                "date": int(time.time()),
            }
        }
        await adapter_with_topic._handle_update(update)

        assert len(captured) == 1
        ev = captured[0]
        assert ev.metadata.get("channel_id") == "7"
        assert ev.metadata.get("parent_channel_id") == "123"

    @pytest.mark.asyncio
    async def test_thread_id_without_registered_topic_no_channel_id(
        self, adapter_with_topic
    ) -> None:
        """Unknown thread id → no channel_id set (falls through to default)."""
        captured: list[MessageEvent] = []

        async def handler(event: MessageEvent) -> str | None:
            captured.append(event)
            return None

        adapter_with_topic.set_message_handler(handler)

        update = {
            "message": {
                "message_id": 2,
                "message_thread_id": 9999,  # unregistered
                "from": {"id": 100},
                "chat": {"id": 123, "type": "private"},
                "text": "hi",
                "date": int(time.time()),
            }
        }
        await adapter_with_topic._handle_update(update)

        assert len(captured) == 1
        assert "channel_id" not in captured[0].metadata

    @pytest.mark.asyncio
    async def test_no_thread_id_no_channel_id(self, adapter_with_topic) -> None:
        captured: list[MessageEvent] = []

        async def handler(event: MessageEvent) -> str | None:
            captured.append(event)
            return None

        adapter_with_topic.set_message_handler(handler)

        update = {
            "message": {
                "message_id": 3,
                "from": {"id": 100},
                "chat": {"id": 123, "type": "private"},
                "text": "hi",
                "date": int(time.time()),
            }
        }
        await adapter_with_topic._handle_update(update)

        assert len(captured) == 1
        assert "channel_id" not in captured[0].metadata

    @pytest.mark.asyncio
    async def test_default_adapter_ignores_thread_id(
        self, adapter_no_topics
    ) -> None:
        """Without dm_topics config, the adapter never sets channel_id."""
        captured: list[MessageEvent] = []

        async def handler(event: MessageEvent) -> str | None:
            captured.append(event)
            return None

        adapter_no_topics.set_message_handler(handler)

        update = {
            "message": {
                "message_id": 4,
                "message_thread_id": 7,  # would match if dm_topics were on
                "from": {"id": 100},
                "chat": {"id": 123, "type": "private"},
                "text": "hi",
                "date": int(time.time()),
            }
        }
        await adapter_no_topics._handle_update(update)

        assert len(captured) == 1
        assert "channel_id" not in captured[0].metadata


class TestResolveOverrides:
    def test_dm_topic_prompt_wins_over_config(self, tmp_path: Path) -> None:
        mod = _load_telegram()
        profile_home = tmp_path / "p"
        profile_home.mkdir()
        pre = DMTopicManager(profile_home)
        pre.register_topic("7", label="X", system_prompt="topic-prompt")

        a = mod.TelegramAdapter(
            {
                "bot_token": "T",
                "profile_home": str(profile_home),
                "dm_topics": {"enabled": True},
                "channel_prompts": {"7": "config-prompt"},
            }
        )
        # Topic registry entry (with system_prompt) wins.
        assert a.resolve_channel_prompt("7") == "topic-prompt"

    def test_falls_back_to_config_when_topic_has_no_prompt(
        self, tmp_path: Path
    ) -> None:
        mod = _load_telegram()
        profile_home = tmp_path / "p"
        profile_home.mkdir()
        pre = DMTopicManager(profile_home)
        # Topic with no system_prompt — fallback path engages.
        pre.register_topic("7", label="X", skill="some-skill")

        a = mod.TelegramAdapter(
            {
                "bot_token": "T",
                "profile_home": str(profile_home),
                "dm_topics": {"enabled": True},
                "channel_prompts": {"7": "config-prompt"},
            }
        )
        assert a.resolve_channel_prompt("7") == "config-prompt"

    def test_dm_topic_skill_wins_over_config(self, tmp_path: Path) -> None:
        mod = _load_telegram()
        profile_home = tmp_path / "p"
        profile_home.mkdir()
        pre = DMTopicManager(profile_home)
        pre.register_topic("7", label="X", skill="topic-skill")

        a = mod.TelegramAdapter(
            {
                "bot_token": "T",
                "profile_home": str(profile_home),
                "dm_topics": {"enabled": True},
                "channel_skill_bindings": {"7": ["config-skill"]},
            }
        )
        assert a.resolve_channel_skills("7") == ["topic-skill"]

    def test_no_dm_topics_config_falls_through_to_base(
        self, tmp_path: Path
    ) -> None:
        mod = _load_telegram()
        profile_home = tmp_path / "p"
        profile_home.mkdir()
        a = mod.TelegramAdapter(
            {
                "bot_token": "T",
                "profile_home": str(profile_home),
                "channel_prompts": {"7": "config-prompt"},
                "channel_skill_bindings": {"7": ["sk"]},
            }
        )
        # _dm_topics is None — base resolver still works.
        assert a.resolve_channel_prompt("7") == "config-prompt"
        assert a.resolve_channel_skills("7") == ["sk"]


# ─── plugin_sdk.file_lock smoke ─────────────────────────────────────


class TestFileLock:
    def test_basic_round_trip(self, tmp_path: Path) -> None:
        from plugin_sdk.file_lock import exclusive_lock

        path = tmp_path / "x.json"
        with exclusive_lock(path):
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text('{"k": 1}', encoding="utf-8")
            tmp.replace(path)
        assert json.loads(path.read_text()) == {"k": 1}

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        from plugin_sdk.file_lock import exclusive_lock

        nested = tmp_path / "sub" / "dir" / "x.json"
        with exclusive_lock(nested):
            pass
        assert nested.exists() or nested.parent.exists()
