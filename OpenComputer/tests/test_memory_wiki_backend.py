"""Tests for extensions/memory-wiki/backend.py (C.2 MVP)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_backend():
    name = "memory_wiki_backend_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "memory-wiki"
        / "backend.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── slugify / validate_slug ──────────────────────────────────────────


def test_slugify_basic():
    mod = _load_backend()
    assert mod.slugify("My First Note") == "my-first-note"
    assert mod.slugify("  spaces!  ") == "spaces"
    assert mod.slugify("") == "untitled"
    assert mod.slugify("!@#$") == "untitled"


def test_validate_slug_accepts_lowercase_and_dashes():
    mod = _load_backend()
    assert mod.validate_slug("hello")
    assert mod.validate_slug("foo-bar_2026")
    assert not mod.validate_slug("UPPER")
    assert not mod.validate_slug("with spaces")
    assert not mod.validate_slug("")


# ─── extract_wikilinks ────────────────────────────────────────────────


def test_extract_wikilinks_finds_targets_dedupes():
    mod = _load_backend()
    body = "see [[alpha]] and [[beta]] and again [[alpha]]"
    out = mod.extract_wikilinks(body)
    assert out == ["alpha", "beta"]


def test_extract_wikilinks_empty_body():
    mod = _load_backend()
    assert mod.extract_wikilinks("") == []


# ─── WikiMemoryBackend roundtrip ──────────────────────────────────────


def test_add_then_read_roundtrip(tmp_path: Path):
    mod = _load_backend()
    b = mod.WikiMemoryBackend(root=tmp_path / "wiki1")

    slug = b.add(title="Hello", body="hi there", tags=("greeting",))
    assert slug == "hello"

    note = b.read(slug)
    assert note is not None
    assert note.title == "Hello"
    assert note.body.strip() == "hi there"
    assert note.tags == ("greeting",)
    assert note.created_at > 0


def test_add_collision_appends_suffix(tmp_path: Path):
    mod = _load_backend()
    b = mod.WikiMemoryBackend(root=tmp_path / "wiki2")

    a = b.add(title="Note", body="first")
    c = b.add(title="Note", body="second")
    assert a == "note"
    assert c == "note-2"


def test_search_finds_body_substring(tmp_path: Path):
    mod = _load_backend()
    b = mod.WikiMemoryBackend(root=tmp_path / "wiki3")

    b.add(title="Apple", body="apple is a fruit")
    b.add(title="Carrot", body="carrots are vegetables")

    out = b.search("vegetable")
    assert out == ["carrot"]


def test_backlinks_computed_on_add(tmp_path: Path):
    mod = _load_backend()
    b = mod.WikiMemoryBackend(root=tmp_path / "wiki4")

    b.add(title="Target", body="a leaf note")
    b.add(title="Source", body="see [[target]] for context")

    assert b.backlinks("target") == ["source"]
    assert b.backlinks("source") == []


def test_delete_cleans_backlinks(tmp_path: Path):
    mod = _load_backend()
    b = mod.WikiMemoryBackend(root=tmp_path / "wiki5")

    b.add(title="Target", body="a")
    b.add(title="Source", body="see [[target]]")

    assert b.backlinks("target") == ["source"]

    assert b.delete("source") is True
    assert b.backlinks("target") == []


def test_delete_returns_false_when_missing(tmp_path: Path):
    mod = _load_backend()
    b = mod.WikiMemoryBackend(root=tmp_path / "wiki6")
    assert b.delete("ghost") is False


def test_list_slugs_sorted(tmp_path: Path):
    mod = _load_backend()
    b = mod.WikiMemoryBackend(root=tmp_path / "wiki7")
    b.add(title="Charlie", body="x")
    b.add(title="Alpha", body="x")
    b.add(title="Bravo", body="x")
    assert b.list_slugs() == ["alpha", "bravo", "charlie"]


def test_invalid_explicit_slug_raises(tmp_path: Path):
    import pytest

    mod = _load_backend()
    b = mod.WikiMemoryBackend(root=tmp_path / "wiki8")
    with pytest.raises(ValueError):
        b.add(title="Bad", body="x", slug="UPPER")
