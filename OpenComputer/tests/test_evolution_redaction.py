"""Tests for opencomputer.evolution.redaction (P-14 secondary regex sweep).

Five patterns are exercised individually (hit + miss cases) plus a couple of
integration cases that combine patterns or run the metadata-mapping helper.
"""

from __future__ import annotations

import pytest

from opencomputer.evolution.redaction import (
    PATTERN_NAMES,
    empty_counts,
    merge_counts,
    redact,
    redact_metadata,
)

# ---------------------------------------------------------------------------
# 0. Module surface
# ---------------------------------------------------------------------------


def test_pattern_names_stable_order():
    """PATTERN_NAMES is a public, stable contract — keep this order.

    The original P-14 pattern set sat in slots 0-4. P-16 appended five
    more secret-token patterns AT THE END so existing trajectory
    files (which serialise the dict by name, not position) keep
    parsing — and reading code that asserts names is in a known
    range still works.
    """
    assert PATTERN_NAMES == (
        "api_key",
        "file_path",
        "email",
        "ip",
        "bearer_token",
        "slack_token",
        "telegram_token",
        "anthropic_key",
        "openai_key",
        "aws_akid",
    )


def test_empty_counts_zero_for_each_pattern():
    counts = empty_counts()
    assert set(counts.keys()) == set(PATTERN_NAMES)
    assert all(v == 0 for v in counts.values())


# ---------------------------------------------------------------------------
# 1. API key pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_counter",
    [
        # The OpenAI-shaped sk- key now lands in the more specific
        # ``openai_key`` slot (P-16). Older trajectories that included
        # an ``sk-...`` will tally under ``openai_key`` going forward.
        ("use sk-abcdef0123456789abcdef0123 to call", "openai_key"),
        # The legacy generic patterns still feed the ``api_key`` slot.
        ("anthropic-ABCDEF0123456789012345xyz", "api_key"),
        ("github_pat_11ABCDEF0123456789012345", "api_key"),
        ("header: ghp_abcdefghij0123456789ABCDEF", "api_key"),
        ("ghs_abcdefghij0123456789ABCDEF", "api_key"),
    ],
)
def test_redact_api_key_hit(raw, expected_counter):
    out, counts = redact(raw)
    # Either label proves the secret was scrubbed; assert exact counter
    # placement so a future re-shuffle of pattern order doesn't quietly
    # move the count to a different slot.
    assert counts[expected_counter] == 1
    assert counts["api_key"] + counts["openai_key"] == 1
    if expected_counter == "api_key":
        assert "<API_KEY_REDACTED>" in out
    else:
        assert "<OPENAI_KEY_REDACTED>" in out


def test_redact_api_key_miss_short_string():
    """Strings shorter than the 20-char threshold should not trigger the API-key rule."""
    out, counts = redact("sk-short")
    assert out == "sk-short"
    assert counts["api_key"] == 0


def test_redact_api_key_miss_no_prefix():
    """Generic alphanumeric blobs without a known prefix should not match."""
    out, counts = redact("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert out == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert counts["api_key"] == 0


# ---------------------------------------------------------------------------
# 2. File path pattern
# ---------------------------------------------------------------------------


def test_redact_file_path_hit():
    raw = "opened /Users/saksham/Vscode/foo.py"
    out, counts = redact(raw)
    assert "/Users/REDACTED/" in out
    assert "/Users/saksham/" not in out
    assert counts["file_path"] == 1
    # tail is preserved
    assert "Vscode/foo.py" in out


def test_redact_file_path_multiple_hits():
    raw = "opened /Users/alice/a.py and /Users/bob/b.py"
    out, counts = redact(raw)
    assert counts["file_path"] == 2
    assert "/Users/alice/" not in out
    assert "/Users/bob/" not in out


def test_redact_file_path_miss_relative():
    raw = "opened ./foo.py"
    out, counts = redact(raw)
    assert out == "opened ./foo.py"
    assert counts["file_path"] == 0


def test_redact_file_path_miss_other_root():
    """Only ``/Users/<name>/`` is targeted — system paths are passed through."""
    raw = "ran /usr/local/bin/foo --flag"
    out, counts = redact(raw)
    assert out == "ran /usr/local/bin/foo --flag"
    assert counts["file_path"] == 0


# ---------------------------------------------------------------------------
# 3. Email pattern
# ---------------------------------------------------------------------------


def test_redact_email_hit():
    raw = "ping me at sakriarchit@gmail.com please"
    out, counts = redact(raw)
    assert "<EMAIL_REDACTED>" in out
    assert "sakriarchit@gmail.com" not in out
    assert counts["email"] == 1


def test_redact_email_miss_no_tld():
    raw = "user@host"  # no TLD
    out, counts = redact(raw)
    assert out == "user@host"
    assert counts["email"] == 0


# ---------------------------------------------------------------------------
# 4. IP pattern
# ---------------------------------------------------------------------------


def test_redact_ip_hit():
    raw = "remote = 192.168.1.42"
    out, counts = redact(raw)
    assert "<IP_REDACTED>" in out
    assert "192.168.1.42" not in out
    assert counts["ip"] == 1


def test_redact_ip_skips_loopback():
    raw = "bind 127.0.0.1; remote 0.0.0.0"
    out, counts = redact(raw)
    # both safe addresses preserved
    assert "127.0.0.1" in out
    assert "0.0.0.0" in out
    assert counts["ip"] == 0


def test_redact_ip_mixed():
    raw = "bind 127.0.0.1; remote 8.8.8.8"
    out, counts = redact(raw)
    assert "127.0.0.1" in out
    assert "<IP_REDACTED>" in out
    assert "8.8.8.8" not in out
    assert counts["ip"] == 1


# ---------------------------------------------------------------------------
# 5. Bearer token pattern
# ---------------------------------------------------------------------------


def test_redact_bearer_hit():
    raw = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
    out, counts = redact(raw)
    assert "Bearer <REDACTED>" in out
    assert "eyJhbGciOiJIUzI1NiJ9.payload.sig" not in out
    assert counts["bearer_token"] == 1


def test_redact_bearer_miss_no_token():
    raw = "auth flow uses bearer scheme but value missing"
    out, counts = redact(raw)
    assert out == "auth flow uses bearer scheme but value missing"
    assert counts["bearer_token"] == 0


# ---------------------------------------------------------------------------
# 6. Combined redaction
# ---------------------------------------------------------------------------


def test_redact_all_patterns_at_once():
    raw = (
        "user=alice@example.com pulled file /Users/alice/foo.py "
        "with key sk-abcdef0123456789abcdef0123 "
        "from 8.8.8.8 using Bearer abc123-def"
    )
    out, counts = redact(raw)
    assert "<EMAIL_REDACTED>" in out
    assert "/Users/REDACTED/" in out
    # P-16: ``sk-...`` now lands in the more-specific openai_key slot.
    assert "<OPENAI_KEY_REDACTED>" in out
    assert "<IP_REDACTED>" in out
    assert "Bearer <REDACTED>" in out
    assert counts == {
        "api_key": 0,
        "file_path": 1,
        "email": 1,
        "ip": 1,
        "bearer_token": 1,
        "slack_token": 0,
        "telegram_token": 0,
        "anthropic_key": 0,
        "openai_key": 1,
        "aws_akid": 0,
    }


def test_redact_clean_string_no_changes():
    raw = "tool ran successfully in 2.3 seconds"
    out, counts = redact(raw)
    assert out == raw
    assert counts == empty_counts()


# ---------------------------------------------------------------------------
# 7. redact_metadata + merge_counts
# ---------------------------------------------------------------------------


def test_redact_metadata_skips_non_strings():
    """Non-string values (int/float/list/dict/None) are passed through unchanged."""
    md = {
        "duration_seconds": 1.5,
        "exit_code": 0,
        "tags": ["a", "b"],
        "nested": {"k": "v"},
        "missing": None,
        "file_path": "/Users/alice/foo.py",
    }
    out, counts = redact_metadata(md)
    assert out["duration_seconds"] == 1.5
    assert out["exit_code"] == 0
    # lists and dicts pass through (not recursively redacted by design — they
    # cannot reach storage with PII because metadata is leaf-typed in practice
    # and string values >200 chars are rejected at TrajectoryEvent construction)
    assert out["tags"] == ["a", "b"]
    assert out["nested"] == {"k": "v"}
    assert out["missing"] is None
    # Only the file_path string was rewritten
    assert "/Users/REDACTED/" in out["file_path"]
    assert counts["file_path"] == 1
    assert counts["api_key"] == 0


def test_merge_counts_sums_each_pattern():
    a = {"api_key": 1, "file_path": 0, "email": 2, "ip": 0, "bearer_token": 1}
    b = {"api_key": 0, "file_path": 3, "email": 1, "ip": 1, "bearer_token": 0}
    total = merge_counts(a, b)
    assert total == {
        "api_key": 1,
        "file_path": 3,
        "email": 3,
        "ip": 1,
        "bearer_token": 1,
        # P-16 added pattern names default to 0 — partial dicts that
        # predate them still merge cleanly.
        "slack_token": 0,
        "telegram_token": 0,
        "anthropic_key": 0,
        "openai_key": 0,
        "aws_akid": 0,
    }


def test_merge_counts_handles_partial_dicts():
    """Counter dicts missing a pattern key default to 0."""
    a = {"api_key": 5}  # only one key present
    total = merge_counts(a)
    assert total["api_key"] == 5
    # Missing keys default to 0 in the merged result.
    assert total["file_path"] == 0
    assert total["email"] == 0
    assert total["ip"] == 0
    assert total["bearer_token"] == 0
