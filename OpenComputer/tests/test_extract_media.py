"""Tests for BaseChannelAdapter.extract_media (Hermes PR 2 Task 2.4).

Parses ``MEDIA: <path>`` and ``[[audio_as_voice]] <path>`` directives.
Whitelist-checks the extension. Returns cleaned text + ``MediaItem``
list.
"""

from __future__ import annotations

from plugin_sdk.channel_contract import BaseChannelAdapter, MediaItem
from plugin_sdk.core import Platform


class _A(BaseChannelAdapter):
    platform = Platform.CLI

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, *a, **kw):
        return None


def test_extract_media_directive_basic() -> None:
    text = "Look at this MEDIA: /tmp/foo.png and that's it"
    cleaned, items = _A({}).extract_media(text)
    assert items == [MediaItem(path="/tmp/foo.png", as_voice=False, ext="png")]
    assert "MEDIA: /tmp/foo.png" not in cleaned


def test_extract_media_audio_as_voice_directive() -> None:
    text = "[[audio_as_voice]] /tmp/note.ogg"
    cleaned, items = _A({}).extract_media(text)
    assert items == [MediaItem(path="/tmp/note.ogg", as_voice=True, ext="ogg")]


def test_extract_media_quoted_path() -> None:
    text = 'MEDIA: "/tmp/path with spaces.png"'
    cleaned, items = _A({}).extract_media(text)
    assert len(items) == 1
    assert items[0].path == "/tmp/path with spaces.png"
    assert items[0].ext == "png"


def test_extract_media_single_quoted_path() -> None:
    text = "MEDIA: '/tmp/foo.png'"
    cleaned, items = _A({}).extract_media(text)
    assert len(items) == 1
    assert items[0].path == "/tmp/foo.png"


def test_extract_media_ext_whitelist_enforced() -> None:
    text = "MEDIA: /tmp/script.exe"
    cleaned, items = _A({}).extract_media(text)
    assert items == []  # rejected; .exe not on whitelist


def test_extract_media_no_ext_rejected() -> None:
    text = "MEDIA: /tmp/no_ext_here"
    cleaned, items = _A({}).extract_media(text)
    assert items == []


def test_extract_media_empty_returns_empty() -> None:
    cleaned, items = _A({}).extract_media("")
    assert cleaned == ""
    assert items == []


def test_extract_media_multiple_directives() -> None:
    text = (
        "MEDIA: /tmp/a.png "
        "[[audio_as_voice]] /tmp/note.ogg "
        "MEDIA: /tmp/clip.mp4"
    )
    cleaned, items = _A({}).extract_media(text)
    assert len(items) == 3
    paths = {item.path for item in items}
    assert paths == {"/tmp/a.png", "/tmp/note.ogg", "/tmp/clip.mp4"}
    voice_flags = {item.path: item.as_voice for item in items}
    assert voice_flags["/tmp/note.ogg"] is True
    assert voice_flags["/tmp/a.png"] is False


def test_extract_media_item_is_frozen() -> None:
    """MediaItem is a frozen dataclass — values can't be mutated post-construction."""
    import dataclasses

    item = MediaItem(path="/tmp/x.png", as_voice=False, ext="png")
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        item.path = "/etc/passwd"  # type: ignore[misc]


def test_extract_media_pdf_extension() -> None:
    text = "MEDIA: /tmp/report.pdf"
    cleaned, items = _A({}).extract_media(text)
    assert len(items) == 1
    assert items[0].ext == "pdf"


def test_extract_media_lowercase_ext() -> None:
    """Extension is normalised to lowercase for stable whitelist check."""
    text = "MEDIA: /tmp/IMG.PNG"
    cleaned, items = _A({}).extract_media(text)
    assert len(items) == 1
    assert items[0].ext == "png"
