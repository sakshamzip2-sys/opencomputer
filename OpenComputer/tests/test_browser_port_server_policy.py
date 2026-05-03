"""Unit tests for ``server.policy`` — path normalization + mutation gating."""

from __future__ import annotations

from extensions.browser_control.server.policy import (
    is_persistent_browser_profile_mutation,
    normalize_browser_request_path,
)


def test_normalize_empty_returns_empty() -> None:
    assert normalize_browser_request_path("") == ""
    assert normalize_browser_request_path(None) == ""


def test_normalize_adds_leading_slash() -> None:
    assert normalize_browser_request_path("foo") == "/foo"


def test_normalize_strips_trailing_slash() -> None:
    assert normalize_browser_request_path("/foo/") == "/foo"


def test_normalize_strips_multiple_trailing_slashes() -> None:
    assert normalize_browser_request_path("/foo///") == "/foo"


def test_normalize_keeps_root() -> None:
    assert normalize_browser_request_path("/") == "/"


def test_normalize_handles_whitespace() -> None:
    assert normalize_browser_request_path("  /foo  ") == "/foo"


# ─── mutation gating ────────────────────────────────────────────────


def test_profiles_create_is_mutation() -> None:
    assert is_persistent_browser_profile_mutation("POST", "/profiles/create")


def test_reset_profile_is_mutation() -> None:
    assert is_persistent_browser_profile_mutation("POST", "/reset-profile")


def test_delete_named_profile_is_mutation() -> None:
    assert is_persistent_browser_profile_mutation("DELETE", "/profiles/foo")


def test_delete_named_profile_with_trailing_slash() -> None:
    assert is_persistent_browser_profile_mutation("DELETE", "/profiles/foo/")


def test_nested_profile_path_is_not_mutation() -> None:
    """``DELETE /profiles/foo/bar`` doesn't match the regex (slash inside)."""
    assert not is_persistent_browser_profile_mutation("DELETE", "/profiles/foo/bar")


def test_navigate_is_not_mutation() -> None:
    assert not is_persistent_browser_profile_mutation("POST", "/navigate")


def test_get_profiles_is_not_mutation() -> None:
    assert not is_persistent_browser_profile_mutation("GET", "/profiles")


def test_start_stop_not_mutation() -> None:
    assert not is_persistent_browser_profile_mutation("POST", "/start")
    assert not is_persistent_browser_profile_mutation("POST", "/stop")


def test_method_case_insensitive() -> None:
    assert is_persistent_browser_profile_mutation("post", "/profiles/create")
    assert is_persistent_browser_profile_mutation("Delete", "/profiles/foo")


def test_blank_inputs_return_false() -> None:
    assert not is_persistent_browser_profile_mutation(None, "/profiles/create")
    assert not is_persistent_browser_profile_mutation("POST", None)
