"""Unit tests for browser-port `_utils/` (Wave 0a).

Covers: atomic_write_{text,bytes,json} (incl. fsync verification),
url_pattern.match (exact/glob/substring), safe_filename.sanitize,
trash.move_to_trash (mocked send2trash), errors.BrowserServiceError.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest
from extensions.browser_control._utils import (
    BrowserServiceError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    move_to_trash,
    sanitize_filename,
    url_match,
)
from extensions.browser_control._utils import atomic_write as atomic_write_module

# ─── atomic_write ──────────────────────────────────────────────────────


def test_atomic_write_text_writes_content(tmp_path):
    target = tmp_path / "hello.txt"
    atomic_write_text(target, "hello world")
    assert target.read_text() == "hello world"


def test_atomic_write_bytes_writes_content(tmp_path):
    target = tmp_path / "blob.bin"
    payload = b"\x00\x01\x02binary"
    atomic_write_bytes(target, payload)
    assert target.read_bytes() == payload


def test_atomic_write_json_pretty_with_trailing_newline(tmp_path):
    target = tmp_path / "data.json"
    atomic_write_json(target, {"a": 1, "b": [1, 2, 3]})
    raw = target.read_text()
    assert raw.endswith("\n")
    assert json.loads(raw) == {"a": 1, "b": [1, 2, 3]}


def test_atomic_write_creates_missing_parent_dirs(tmp_path):
    target = tmp_path / "deeply" / "nested" / "out.txt"
    atomic_write_text(target, "ok")
    assert target.read_text() == "ok"


def test_atomic_write_calls_fsync(tmp_path):
    target = tmp_path / "fsynced.txt"
    real_fsync = os.fsync
    calls = []

    def spy_fsync(fd):
        calls.append(fd)
        real_fsync(fd)

    with patch.object(atomic_write_module.os, "fsync", spy_fsync):
        atomic_write_text(target, "synced")

    assert calls, "atomic_write must call os.fsync before rename"
    assert target.read_text() == "synced"


def test_atomic_write_cleans_up_tmp_on_failure(tmp_path):
    target = tmp_path / "fail.txt"
    boom = OSError("disk full")

    def explode(_fd):
        raise boom

    with patch.object(atomic_write_module.os, "fsync", explode):
        with pytest.raises(OSError, match="disk full"):
            atomic_write_text(target, "nope")

    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".browser-port-tmp-")]
    assert leftovers == [], f"tmp file leaked: {leftovers}"


# ─── url_pattern ───────────────────────────────────────────────────────


def test_url_match_exact_normalizes_trailing_slash():
    assert url_match("https://example.com", "https://example.com/", mode="exact")
    assert url_match("https://example.com/", "https://example.com", mode="exact")
    assert not url_match("https://example.com/x", "https://example.com", mode="exact")


def test_url_match_glob_supports_star():
    assert url_match("https://*.example.com/*", "https://api.example.com/v1/x", mode="glob")
    assert url_match("*", "anything-at-all", mode="glob")
    assert not url_match("https://*.example.com", "https://example.com", mode="glob")


def test_url_match_glob_does_not_treat_question_mark_as_wildcard():
    # `?` is a literal — OpenClaw claimed otherwise but never implemented it,
    # so we keep `?` as exact-character match. Query strings with `?` work fine.
    assert url_match("https://x.com/?q=1", "https://x.com/?q=1", mode="glob")
    assert not url_match("https://x.com/?", "https://x.com/!", mode="glob")


def test_url_match_substring():
    assert url_match("example.com", "https://example.com/path", mode="substring")
    assert not url_match("example.org", "https://example.com/path", mode="substring")


def test_url_match_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown url_pattern mode"):
        url_match("x", "y", mode="regex")  # type: ignore[arg-type]


# ─── safe_filename ─────────────────────────────────────────────────────


def test_sanitize_strips_path_components():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("C:\\Users\\evil\\drop.txt") == "drop.txt"


def test_sanitize_strips_control_chars():
    raw = "report\x00\x01\nfinal\x7f.pdf"
    out = sanitize_filename(raw)
    assert "\x00" not in out
    assert "\n" not in out
    assert "\x7f" not in out
    assert out.endswith(".pdf")


def test_sanitize_preserves_unicode():
    assert sanitize_filename("résumé.pdf") == "résumé.pdf"


def test_sanitize_falls_back_when_empty_or_dotty():
    assert sanitize_filename("") == "untitled"
    assert sanitize_filename("   ") == "untitled"
    assert sanitize_filename("..") == "untitled"
    assert sanitize_filename(".") == "untitled"


def test_sanitize_caps_length_preserving_extension():
    long_stem = "a" * 500
    out = sanitize_filename(f"{long_stem}.pdf", max_len=50)
    assert len(out) == 50
    assert out.endswith(".pdf")


def test_sanitize_caps_length_when_no_useful_extension():
    out = sanitize_filename("a" * 500, max_len=20)
    assert out == "a" * 20


# ─── trash ─────────────────────────────────────────────────────────────


def test_move_to_trash_delegates_to_send2trash(tmp_path):
    victim = tmp_path / "doomed.txt"
    victim.write_text("bye")
    with patch("extensions.browser_control._utils.trash.send2trash") as mock:
        move_to_trash(victim)
    mock.assert_called_once_with(str(victim))


def test_move_to_trash_accepts_pathlike(tmp_path):
    victim = tmp_path / "doomed.txt"
    victim.write_text("bye")
    with patch("extensions.browser_control._utils.trash.send2trash") as mock:
        move_to_trash(str(victim))
    mock.assert_called_once_with(str(victim))


# ─── errors ────────────────────────────────────────────────────────────


def test_browser_service_error_basic():
    err = BrowserServiceError("nope", status=500, code="X")
    assert str(err) == "nope"
    assert err.status == 500
    assert err.code == "X"


def test_browser_service_error_from_response_429_uses_static_hint():
    err = BrowserServiceError.from_response(429, {"error": {"message": "leaked-internal"}})
    assert err.status == 429
    assert "rate limit" in str(err).lower()
    assert "leaked-internal" not in str(err), "must NOT reflect upstream body for 429"


def test_browser_service_error_from_response_404_uses_body_message():
    err = BrowserServiceError.from_response(404, {"error": {"message": "tab not found", "code": "E_TAB"}})
    assert err.status == 404
    assert err.code == "E_TAB"
    assert str(err) == "tab not found"


def test_browser_service_error_from_response_falls_back_to_top_message():
    err = BrowserServiceError.from_response(400, {"message": "bad request"})
    assert str(err) == "bad request"


def test_browser_service_error_from_response_falls_back_to_status_string():
    err = BrowserServiceError.from_response(500, None)
    assert str(err) == "HTTP 500"
    assert err.status == 500
