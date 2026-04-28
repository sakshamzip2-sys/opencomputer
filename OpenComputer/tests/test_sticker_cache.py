"""Tests for plugin_sdk.sticker_cache.StickerCache and Telegram wiring (PR 3a.5).

Covers the persistence/LRU contract of the cache itself and the
``_handle_update`` short-circuit path that injects cached descriptions
as ``[sticker: <description>]`` text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from extensions.telegram.adapter import TelegramAdapter
from plugin_sdk.sticker_cache import StickerCache

# ─── StickerCache contract ─────────────────────────────────────────


class TestStickerCacheCore:
    def test_get_unknown_returns_none(self, tmp_path: Path) -> None:
        c = StickerCache(tmp_path)
        assert c.get("nope") is None

    def test_put_then_get_returns_value(self, tmp_path: Path) -> None:
        c = StickerCache(tmp_path)
        c.put("uid1", "smiling cat")
        assert c.get("uid1") == "smiling cat"

    def test_put_persists_across_instances(self, tmp_path: Path) -> None:
        c1 = StickerCache(tmp_path)
        c1.put("uid1", "smiling cat")
        # New instance pointed at same path picks up the entry.
        c2 = StickerCache(tmp_path)
        assert c2.get("uid1") == "smiling cat"

    def test_lru_bound_enforced(self, tmp_path: Path) -> None:
        c = StickerCache(tmp_path, max_entries=3)
        c.put("a", "A")
        c.put("b", "B")
        c.put("c", "C")
        c.put("d", "D")  # evicts 'a' (oldest)
        assert c.get("a") is None
        assert c.get("b") == "B"
        assert c.get("c") == "C"
        assert c.get("d") == "D"

    def test_get_promotes_recency(self, tmp_path: Path) -> None:
        c = StickerCache(tmp_path, max_entries=3)
        c.put("a", "A")
        c.put("b", "B")
        c.put("c", "C")
        # Touch 'a' so 'b' becomes oldest.
        assert c.get("a") == "A"
        c.put("d", "D")  # evicts 'b' now
        assert c.get("a") == "A"
        assert c.get("b") is None
        assert c.get("c") == "C"
        assert c.get("d") == "D"

    def test_corrupt_json_returns_empty(self, tmp_path: Path) -> None:
        """A malformed sticker_descriptions.json must NOT raise on load."""
        (tmp_path / "sticker_descriptions.json").write_text("{not json")
        c = StickerCache(tmp_path)
        assert c.get("anything") is None

    def test_atomic_save_uses_tmp_then_rename(self, tmp_path: Path) -> None:
        """After put(), the canonical file exists and decodes back."""
        c = StickerCache(tmp_path)
        c.put("uid", "desc")
        path = tmp_path / "sticker_descriptions.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert ["uid", "desc"] in data

    def test_max_entries_default_is_5000(self, tmp_path: Path) -> None:
        c = StickerCache(tmp_path)
        assert c._max == 5000


# ─── _handle_update wiring ─────────────────────────────────────────


def _make_adapter(profile_home: Path) -> TelegramAdapter:
    a = TelegramAdapter({
        "bot_token": "test",
        "profile_home": str(profile_home),
    })
    a._bot_id = 42
    a._bot_username = "hermes_bot"
    return a


def _sticker_update(
    *,
    file_id: str = "FID1",
    file_unique_id: str = "UID1",
    is_animated: bool = False,
    is_video: bool = False,
    emoji: str = "😀",
) -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 999},
            "chat": {"id": 100, "type": "private"},
            "date": 0,
            "sticker": {
                "file_id": file_id,
                "file_unique_id": file_unique_id,
                "is_animated": is_animated,
                "is_video": is_video,
                "emoji": emoji,
                "set_name": "TestPack",
                "width": 512,
                "height": 512,
            },
        },
    }


class TestHandleUpdateStickerWiring:
    @pytest.mark.asyncio
    async def test_cache_hit_injects_description_as_text(
        self, tmp_path: Path,
    ) -> None:
        a = _make_adapter(tmp_path)
        # Pre-seed the cache with a known description.
        a._sticker_cache.put("UID-CAT", "smiling cat sticker")

        delivered: list[Any] = []

        async def handler(ev: Any) -> None:
            delivered.append(ev)

        a._message_handler = handler  # type: ignore[assignment]

        await a._handle_update(_sticker_update(file_unique_id="UID-CAT"))
        assert len(delivered) == 1
        ev = delivered[0]
        # Cache hit means we injected the description as text, NOT as
        # an attachment.
        assert "[sticker: smiling cat sticker]" in ev.text
        assert ev.attachments == []

    @pytest.mark.asyncio
    async def test_cache_miss_falls_through_to_attachment(
        self, tmp_path: Path,
    ) -> None:
        a = _make_adapter(tmp_path)
        delivered: list[Any] = []

        async def handler(ev: Any) -> None:
            delivered.append(ev)

        a._message_handler = handler  # type: ignore[assignment]

        await a._handle_update(_sticker_update(file_id="FX", file_unique_id="UID-NEW"))
        assert len(delivered) == 1
        ev = delivered[0]
        # No cached description: surface as attachment for downstream
        # vision describe (out of scope here).
        assert "telegram:FX" in ev.attachments
        # attachment_meta carries sticker-specific fields.
        meta = ev.metadata.get("attachment_meta") or []
        assert any(m.get("type") == "sticker" for m in meta)
        # Text remains empty (or just whatever was already there) —
        # we don't inject anything on miss.
        assert "[sticker:" not in (ev.text or "")

    @pytest.mark.asyncio
    async def test_animated_sticker_handled(self, tmp_path: Path) -> None:
        a = _make_adapter(tmp_path)
        delivered: list[Any] = []

        async def handler(ev: Any) -> None:
            delivered.append(ev)

        a._message_handler = handler  # type: ignore[assignment]

        await a._handle_update(_sticker_update(
            file_id="ANIM", file_unique_id="UID-ANIM", is_animated=True,
        ))
        assert len(delivered) == 1
        ev = delivered[0]
        meta = ev.metadata.get("attachment_meta") or []
        anim = next(m for m in meta if m.get("type") == "sticker")
        assert anim["is_animated"] is True
        assert anim["is_video"] is False
        assert anim["file_unique_id"] == "UID-ANIM"

    @pytest.mark.asyncio
    async def test_video_sticker_handled(self, tmp_path: Path) -> None:
        a = _make_adapter(tmp_path)
        delivered: list[Any] = []

        async def handler(ev: Any) -> None:
            delivered.append(ev)

        a._message_handler = handler  # type: ignore[assignment]

        await a._handle_update(_sticker_update(
            file_id="VID", file_unique_id="UID-VID", is_video=True,
        ))
        assert len(delivered) == 1
        ev = delivered[0]
        meta = ev.metadata.get("attachment_meta") or []
        vid = next(m for m in meta if m.get("type") == "sticker")
        assert vid["is_video"] is True

    @pytest.mark.asyncio
    async def test_cache_hit_short_circuits_no_extra_attachment(
        self, tmp_path: Path,
    ) -> None:
        """Cache-hit path must NOT also append the sticker as attachment."""
        a = _make_adapter(tmp_path)
        a._sticker_cache.put("UID-CACHED", "thumbs up")

        delivered: list[Any] = []

        async def handler(ev: Any) -> None:
            delivered.append(ev)

        a._message_handler = handler  # type: ignore[assignment]

        await a._handle_update(_sticker_update(file_unique_id="UID-CACHED"))
        ev = delivered[0]
        # No telegram:FID attachment when cache short-circuits.
        assert all("telegram:" not in a_ for a_ in ev.attachments)


class TestStickerCachePersistence:
    def test_put_via_tmp_path_and_reload(self, tmp_path: Path) -> None:
        """Verify the explicit persistence-via-tmp_path round-trip."""
        c = StickerCache(tmp_path, max_entries=10)
        c.put("uid-x", "purple star")
        c.put("uid-y", "green heart")
        # Round-trip: a fresh cache instance sees both.
        c2 = StickerCache(tmp_path, max_entries=10)
        assert c2.get("uid-x") == "purple star"
        assert c2.get("uid-y") == "green heart"
