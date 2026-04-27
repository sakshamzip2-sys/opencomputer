"""Tests for image-paste plumbing.

Real clipboard reads / TTY behavior cannot be exercised in unit tests;
these cover the deterministic helpers and the multimodal-content
conversion in the Anthropic provider.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

from opencomputer.cli_ui.input_loop import (
    IMAGE_PLACEHOLDER_RE,
    extract_image_attachments,
)
from plugin_sdk.core import Message


def test_extract_attachments_simple():
    text, paths = extract_image_attachments(
        "describe this [image: /tmp/a.png] and [image: /tmp/b.png]"
    )
    assert paths == ["/tmp/a.png", "/tmp/b.png"]
    assert "image:" not in text
    assert "describe this" in text
    assert "and" in text


def test_extract_attachments_dedup():
    text, paths = extract_image_attachments(
        "[image: /a.png] foo [image: /a.png] bar"
    )
    assert paths == ["/a.png"]


def test_extract_attachments_none():
    text, paths = extract_image_attachments("plain text, no attachments")
    assert paths == []
    assert text == "plain text, no attachments"


def test_extract_attachments_only_placeholder():
    text, paths = extract_image_attachments("[image: /foo.png]")
    assert paths == ["/foo.png"]
    assert text == ""


def test_extract_attachments_collapses_whitespace():
    text, paths = extract_image_attachments(
        "first\n\n\n[image: /x.png]\n\n\nsecond"
    )
    assert paths == ["/x.png"]
    # Triple-blank-line runs should be collapsed.
    assert "\n\n\n" not in text


def test_image_placeholder_regex():
    matches = IMAGE_PLACEHOLDER_RE.findall(
        "[image: /a.png] [image:/b.png] [image:    /c.png   ]"
    )
    assert matches == ["/a.png", "/b.png", "/c.png   "]


def test_message_supports_attachments_field():
    """plugin_sdk.Message gained an attachments field for image paths."""
    m = Message(role="user", content="hello", attachments=["/tmp/a.png"])
    assert m.attachments == ["/tmp/a.png"]
    # Backward compat: omitting the field should default to empty list.
    m2 = Message(role="user", content="plain text")
    assert m2.attachments == []


def test_anthropic_multimodal_helper_text_only_when_no_images():
    spec = importlib.util.spec_from_file_location(
        "ap_test", "extensions/anthropic-provider/provider.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    blocks = mod._build_anthropic_multimodal_content(
        text="just text", image_paths=[]
    )
    assert len(blocks) == 1
    assert blocks[0] == {"type": "text", "text": "just text"}


def test_anthropic_multimodal_helper_with_real_image(tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "ap_test2", "extensions/anthropic-provider/provider.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360000000000300010001fcdde40c0000000049454e44"
        "ae426082"
    )
    img_path = tmp_path / "sample.png"
    img_path.write_bytes(png)
    blocks = mod._build_anthropic_multimodal_content(
        text="describe this", image_paths=[str(img_path)]
    )
    assert len(blocks) == 2
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[0]["source"]["type"] == "base64"
    assert blocks[1] == {"type": "text", "text": "describe this"}


def test_anthropic_multimodal_helper_skips_missing_paths(tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "ap_test3", "extensions/anthropic-provider/provider.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    blocks = mod._build_anthropic_multimodal_content(
        text="hello",
        image_paths=[str(tmp_path / "does-not-exist.png")],
    )
    # Image was unreadable → only the text block survives.
    assert blocks == [{"type": "text", "text": "hello"}]


def test_anthropic_multimodal_helper_skips_unsupported_type(tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "ap_test4", "extensions/anthropic-provider/provider.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    txt_path = tmp_path / "not-an-image.txt"
    txt_path.write_text("hello")
    blocks = mod._build_anthropic_multimodal_content(
        text="caption this", image_paths=[str(txt_path)]
    )
    # Unsupported media type → only text remains.
    assert blocks == [{"type": "text", "text": "caption this"}]


def test_anthropic_multimodal_helper_handles_oversized(tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "ap_test5", "extensions/anthropic-provider/provider.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    big_path = tmp_path / "big.png"
    big_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 1))
    blocks = mod._build_anthropic_multimodal_content(
        text="too big", image_paths=[str(big_path)]
    )
    # Oversized image is skipped — only the text block remains.
    assert blocks == [{"type": "text", "text": "too big"}]


def test_openai_multimodal_helper_text_only_when_no_images():
    spec = importlib.util.spec_from_file_location(
        "op_test", "extensions/openai-provider/provider.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    blocks = mod._build_openai_multimodal_content(
        text="just text", image_paths=[]
    )
    assert len(blocks) == 1
    assert blocks[0] == {"type": "text", "text": "just text"}


def test_openai_multimodal_helper_with_real_image(tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "op_test2", "extensions/openai-provider/provider.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360000000000300010001fcdde40c0000000049454e44"
        "ae426082"
    )
    img_path = tmp_path / "sample.png"
    img_path.write_bytes(png)
    blocks = mod._build_openai_multimodal_content(
        text="describe this", image_paths=[str(img_path)]
    )
    assert len(blocks) == 2
    # OpenAI puts text first, then images.
    assert blocks[0] == {"type": "text", "text": "describe this"}
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_multimodal_helper_skips_oversized(tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "op_test3", "extensions/openai-provider/provider.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    big_path = tmp_path / "big.png"
    big_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (20 * 1024 * 1024 + 1))
    blocks = mod._build_openai_multimodal_content(
        text="too big", image_paths=[str(big_path)]
    )
    assert blocks == [{"type": "text", "text": "too big"}]


def test_sessiondb_persists_attachments(tmp_path: Path):
    """``Message.attachments`` round-trips through SessionDB."""
    import uuid as _uuid

    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "test.db")
    sid = str(_uuid.uuid4())
    db.create_session(sid)
    db.append_message(
        sid, Message(role="user", content="see this", attachments=["/tmp/a.png", "/tmp/b.png"])
    )
    db.append_message(sid, Message(role="assistant", content="OK"))
    msgs = db.get_messages(sid)
    assert len(msgs) == 2
    assert msgs[0].attachments == ["/tmp/a.png", "/tmp/b.png"]
    assert msgs[1].attachments == []


def test_sessiondb_handles_empty_attachments(tmp_path: Path):
    """Empty ``attachments`` list collapses to NULL in the DB and round-trips as []."""
    import uuid as _uuid

    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "test.db")
    sid = str(_uuid.uuid4())
    db.create_session(sid)
    db.append_message(sid, Message(role="user", content="plain text"))
    msgs = db.get_messages(sid)
    assert msgs[0].attachments == []


def test_clipboard_module_imports():
    """Clipboard module imports cleanly (uses only stdlib subprocess)."""
    from opencomputer.cli_ui import clipboard

    assert callable(clipboard.has_clipboard_image)
    assert callable(clipboard.save_clipboard_image)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only quick smoke")
def test_clipboard_save_returns_bool_on_macos(tmp_path: Path):
    """``save_clipboard_image`` must always return a bool, never raise.

    Whether the macOS clipboard contains an image or not is environmental;
    we just verify the function exits cleanly with a bool.
    """
    from opencomputer.cli_ui.clipboard import save_clipboard_image

    dest = tmp_path / "test.png"
    result = save_clipboard_image(dest)
    assert isinstance(result, bool)
