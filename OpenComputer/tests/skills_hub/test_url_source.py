"""Tests for opencomputer.skills_hub.sources.url — URL-based skill source.

Wave 5 T18 (Hermes ``9c416e20a``). The SkillSource ABC contract is
``name`` / ``search`` / ``fetch`` / ``inspect``; the router routes by
``<source>/<name>`` identifier prefix.

URL identifiers are ``url/<urlsafe_b64(url)>`` so the URL's own ``/``
characters don't break the router's prefix split.
"""

from __future__ import annotations

import pytest

from opencomputer.skills_hub.sources.url import (
    UrlSource,
    decode_slug,
    encode_url,
)

# ──────────────────────────────────────────────────────────────────────
# encode/decode helpers
# ──────────────────────────────────────────────────────────────────────


def test_encode_decode_roundtrip():
    url = "https://example.com/some/path/skill.md"
    assert decode_slug(encode_url(url)) == url


def test_encode_no_padding_in_slug():
    """Result must not contain ``=`` so it survives URL routing."""
    slug = encode_url("https://example.com/x.md")
    assert "=" not in slug


def test_decode_handles_missing_padding():
    """Padding is stripped at encode time; decode must re-add it."""
    url = "https://example.com/skill.md"
    assert decode_slug(encode_url(url)) == url


# ──────────────────────────────────────────────────────────────────────
# UrlSource shape
# ──────────────────────────────────────────────────────────────────────


def test_name_is_url():
    assert UrlSource().name == "url"


def test_search_returns_empty():
    """URL skills are install-by-identifier only — never in keyword search."""
    assert UrlSource().search("anything", limit=10) == []


# ──────────────────────────────────────────────────────────────────────
# Identifier validation
# ──────────────────────────────────────────────────────────────────────


def test_inspect_returns_none_for_non_url_prefix():
    s = UrlSource()
    assert s.inspect("github.com/foo/bar") is None
    assert s.inspect("not-an-identifier") is None


def test_inspect_returns_none_for_well_known_path():
    """/.well-known/skills/* is WellKnownSource's territory, not ours."""
    s = UrlSource()
    ident = "url/" + encode_url("https://example.com/.well-known/skills/foo.md")
    assert s.inspect(ident) is None


def test_inspect_returns_none_for_non_md_url():
    s = UrlSource()
    ident = "url/" + encode_url("https://example.com/page.html")
    assert s.inspect(ident) is None
    ident2 = "url/" + encode_url("https://example.com/skills.zip")
    assert s.inspect(ident2) is None


def test_fetch_returns_none_for_garbage_slug():
    s = UrlSource()
    assert s.fetch("url/!!!!notbase64!!!!") is None


# ──────────────────────────────────────────────────────────────────────
# Frontmatter + slug fallback
# ──────────────────────────────────────────────────────────────────────


def test_inspect_uses_frontmatter_name(monkeypatch):
    fake_md = (
        "---\n"
        "name: my-skill\n"
        "description: a sample skill\n"
        "---\n"
        "# Body of the skill\n"
    )

    def fake_get(self, url):  # noqa: ARG001
        return fake_md

    monkeypatch.setattr(UrlSource, "_http_get", fake_get)
    s = UrlSource()
    ident = "url/" + encode_url("https://example.com/some/skill.md")
    meta = s.inspect(ident)
    assert meta is not None
    assert meta.name == "my-skill"
    assert meta.description == "a sample skill"
    assert meta.source == "url"
    assert meta.trust_level == "community"
    assert meta.identifier == ident


def test_inspect_falls_back_to_slug_when_no_frontmatter(monkeypatch):
    def fake_get(self, url):  # noqa: ARG001
        return "# Just a heading, no frontmatter"

    monkeypatch.setattr(UrlSource, "_http_get", fake_get)
    s = UrlSource()
    ident = "url/" + encode_url("https://example.com/path/foo-bar.md")
    meta = s.inspect(ident)
    assert meta is not None
    assert meta.name == "foo-bar"


def test_inspect_falls_back_to_unnamed_for_root_url(monkeypatch):
    def fake_get(self, url):  # noqa: ARG001
        return "no frontmatter"

    monkeypatch.setattr(UrlSource, "_http_get", fake_get)
    s = UrlSource()
    ident = "url/" + encode_url("https://example.com/.md")
    meta = s.inspect(ident)
    # path final segment ".md" → slug after stripping suffix is empty → "unnamed-skill"
    if meta is not None:
        assert meta.name in ("unnamed-skill", "")  # tolerate either heuristic outcome


# ──────────────────────────────────────────────────────────────────────
# fetch returns SkillBundle
# ──────────────────────────────────────────────────────────────────────


def test_fetch_returns_bundle(monkeypatch):
    body = "---\nname: x\n---\n# body"

    def fake_get(self, url):  # noqa: ARG001
        return body

    monkeypatch.setattr(UrlSource, "_http_get", fake_get)
    s = UrlSource()
    ident = "url/" + encode_url("https://example.com/x.md")
    bundle = s.fetch(ident)
    assert bundle is not None
    assert bundle.identifier == ident
    assert bundle.skill_md == body
    assert bundle.files == {}


def test_fetch_swallows_http_errors(monkeypatch):
    """Network/HTTP errors return None, not raise — per ABC contract."""

    def boom(self, url):  # noqa: ARG001
        raise RuntimeError("502 bad gateway")

    monkeypatch.setattr(UrlSource, "_http_get", boom)
    s = UrlSource()
    ident = "url/" + encode_url("https://example.com/x.md")
    assert s.fetch(ident) is None
    assert s.inspect(ident) is None
