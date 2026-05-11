"""Phase M — PR URL paste detection in the resume picker search box.

Coverage:

    1. ``_extract_pr_number_from_url`` recognises:
        * https://github.com/owner/repo/pull/123
        * https://github.acme-corp.com/owner/repo/pull/4567   (Enterprise)
        * https://gitlab.com/group/proj/-/merge_requests/89
        * https://bitbucket.org/owner/repo/pull-requests/12
    2. Returns ``None`` for:
        * empty / whitespace / non-string input
        * plain text that contains no URL
        * malformed URLs (no number in path)
    3. Handles surrounding whitespace + trailing slashes.
"""
from __future__ import annotations

from opencomputer.cli_ui.resume_picker import _extract_pr_number_from_url

# ─── Positive cases ──────────────────────────────────────────────────


def test_github_dot_com_url() -> None:
    assert (
        _extract_pr_number_from_url(
            "https://github.com/sakshamzip2-sys/opencomputer/pull/123"
        )
        == 123
    )


def test_github_enterprise_arbitrary_host() -> None:
    assert (
        _extract_pr_number_from_url(
            "https://github.acme-corp.com/team/repo/pull/4567"
        )
        == 4567
    )


def test_gitlab_dot_com_url() -> None:
    assert (
        _extract_pr_number_from_url(
            "https://gitlab.com/group/proj/-/merge_requests/89"
        )
        == 89
    )


def test_self_hosted_gitlab() -> None:
    assert (
        _extract_pr_number_from_url(
            "https://git.acme.com/group/proj/-/merge_requests/12"
        )
        == 12
    )


def test_bitbucket_cloud_url() -> None:
    assert (
        _extract_pr_number_from_url(
            "https://bitbucket.org/owner/repo/pull-requests/12"
        )
        == 12
    )


def test_http_scheme_also_matches() -> None:
    """Plain http (no s) is rare in 2026 but defensively still parses."""
    assert (
        _extract_pr_number_from_url(
            "http://github.com/owner/repo/pull/1"
        )
        == 1
    )


# ─── Whitespace + surrounding text ───────────────────────────────────


def test_leading_whitespace_stripped() -> None:
    assert (
        _extract_pr_number_from_url(
            "   https://github.com/owner/repo/pull/42"
        )
        == 42
    )


def test_trailing_whitespace_stripped() -> None:
    assert (
        _extract_pr_number_from_url(
            "https://github.com/owner/repo/pull/42   "
        )
        == 42
    )


def test_trailing_slash_after_number_does_not_match() -> None:
    """``.../pull/123/files`` extracts 123, NOT 123/files (regex is sane)."""
    assert (
        _extract_pr_number_from_url(
            "https://github.com/owner/repo/pull/123/files"
        )
        == 123
    )


def test_query_string_after_number() -> None:
    assert (
        _extract_pr_number_from_url(
            "https://github.com/owner/repo/pull/123?foo=bar"
        )
        == 123
    )


def test_fragment_after_number() -> None:
    assert (
        _extract_pr_number_from_url(
            "https://github.com/owner/repo/pull/123#discussion_r1"
        )
        == 123
    )


def test_url_embedded_in_text() -> None:
    """Even if the user pastes ``Check out https://...pull/99`` we recognise it."""
    assert (
        _extract_pr_number_from_url(
            "Check out https://github.com/o/r/pull/99 please"
        )
        == 99
    )


# ─── Negative cases ──────────────────────────────────────────────────


def test_empty_string_returns_none() -> None:
    assert _extract_pr_number_from_url("") is None


def test_whitespace_only_returns_none() -> None:
    assert _extract_pr_number_from_url("   \t\n  ") is None


def test_none_input_returns_none() -> None:
    assert _extract_pr_number_from_url(None) is None  # type: ignore[arg-type]


def test_non_string_input_returns_none() -> None:
    assert _extract_pr_number_from_url(42) is None  # type: ignore[arg-type]
    assert _extract_pr_number_from_url(["a", "b"]) is None  # type: ignore[arg-type]


def test_plain_text_returns_none() -> None:
    assert _extract_pr_number_from_url("just some search text") is None


def test_partial_url_no_number() -> None:
    assert _extract_pr_number_from_url("https://github.com/owner/repo/pull/") is None


def test_url_with_non_numeric_pr() -> None:
    assert (
        _extract_pr_number_from_url("https://github.com/owner/repo/pull/foo")
        is None
    )


def test_github_issue_url_does_not_match_pr_pattern() -> None:
    """``/issues/123`` is NOT a PR — we don't want to falsely resolve."""
    assert (
        _extract_pr_number_from_url(
            "https://github.com/owner/repo/issues/123"
        )
        is None
    )


def test_only_path_without_host_returns_none() -> None:
    assert _extract_pr_number_from_url("/owner/repo/pull/42") is None


def test_first_url_in_text_wins() -> None:
    """Multiple PR URLs in one paste — first match wins (deterministic)."""
    text = (
        "https://github.com/o/r/pull/1 vs "
        "https://github.com/o/r/pull/2"
    )
    assert _extract_pr_number_from_url(text) == 1


# ─── Cross-platform priority (GitHub before GitLab before Bitbucket) ─


def test_github_matched_before_gitlab() -> None:
    """If a paste somehow contained both, GitHub takes priority by
    list order in _PR_URL_PATTERNS."""
    text = (
        "https://github.com/o/r/pull/77 and "
        "https://gitlab.com/g/p/-/merge_requests/88"
    )
    assert _extract_pr_number_from_url(text) == 77
