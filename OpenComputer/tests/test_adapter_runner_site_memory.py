"""Tests for ``SiteMemory`` — the per-site, per-profile knowledge base."""

from __future__ import annotations

from pathlib import Path


def test_endpoints_roundtrip(tmp_path: Path):
    from extensions.adapter_runner._site_memory import SiteMemory

    mem = SiteMemory.for_site(tmp_path, "hackernews")
    mem.write_endpoint(
        "topstories",
        {
            "url": "https://hacker-news.firebaseio.com/v0/topstories.json",
            "method": "GET",
        },
    )
    data = mem.read_endpoints()
    assert "topstories" in data
    entry = data["topstories"]
    assert entry["url"].endswith("topstories.json")
    # write_endpoint stamps verified_at
    assert "verified_at" in entry


def test_field_map_roundtrip(tmp_path: Path):
    from extensions.adapter_runner._site_memory import SiteMemory

    mem = SiteMemory.for_site(tmp_path, "bilibili")
    mem.write_field("play", {"meaning": "view count"})
    data = mem.read_field_map()
    assert data["play"]["meaning"] == "view count"
    assert "verified_at" in data["play"]


def test_notes_append_only(tmp_path: Path):
    from extensions.adapter_runner._site_memory import SiteMemory

    mem = SiteMemory.for_site(tmp_path, "x")
    mem.append_note("first")
    mem.append_note("second")
    contents = mem.read_notes()
    assert "first" in contents
    assert "second" in contents


def test_verify_fixture_roundtrip(tmp_path: Path):
    from extensions.adapter_runner._site_memory import SiteMemory

    mem = SiteMemory.for_site(tmp_path, "hn")
    fixture = {"args": {"limit": 5}, "rowCount": {"min": 5}}
    mem.write_verify("top", fixture)
    loaded = mem.read_verify("top")
    assert loaded == fixture


def test_fixture_path_includes_timestamp(tmp_path: Path):
    from extensions.adapter_runner._site_memory import SiteMemory

    mem = SiteMemory.for_site(tmp_path, "hn")
    p = mem.write_fixture("top", [{"x": 1}], timestamp="20260101-000000")
    assert p.name == "top-20260101-000000.json"
    assert p.is_file()


def test_read_falls_through_to_field_map(tmp_path: Path):
    from extensions.adapter_runner._site_memory import SiteMemory

    mem = SiteMemory.for_site(tmp_path, "x")
    mem.write_field("alias", {"meaning": "v"})
    assert mem.read("alias")["meaning"] == "v"
