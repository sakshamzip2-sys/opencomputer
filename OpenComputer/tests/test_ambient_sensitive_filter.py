"""tests/test_ambient_sensitive_filter.py"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from extensions.ambient_sensors.foreground import ForegroundSnapshot
from extensions.ambient_sensors.sensitive_apps import (
    _DEFAULT_PATTERNS,
    is_sensitive,
    load_user_overrides,
)


def _snap(app: str, title: str = "") -> ForegroundSnapshot:
    return ForegroundSnapshot(app_name=app, window_title=title, bundle_id="", platform="linux")


@pytest.mark.parametrize("name", ["1Password", "Bitwarden", "KeePassXC", "Dashlane"])
def test_password_managers_default_sensitive(name):
    assert is_sensitive(_snap(name)) is True


@pytest.mark.parametrize("name", [
    "Chase Mobile", "HDFC Bank", "Robinhood", "Coinbase", "Zerodha", "Schwab",
])
def test_banking_default_sensitive(name):
    assert is_sensitive(_snap(name)) is True


@pytest.mark.parametrize("name", ["MyChart", "Teladoc", "Healow"])
def test_healthcare_default_sensitive(name):
    assert is_sensitive(_snap(name)) is True


def test_non_sensitive_app_returns_false():
    assert is_sensitive(_snap("Code")) is False
    assert is_sensitive(_snap("Safari", title="github.com — Safari")) is False
    assert is_sensitive(_snap("Terminal")) is False


def test_title_pattern_match():
    """Sensitive when ONLY the window title matches (app name is benign)."""
    assert is_sensitive(_snap("Safari", title="Chase Bank — Account Summary")) is True
    assert is_sensitive(_snap("Firefox", title="Private Browsing")) is True


def test_user_override_extends_default(tmp_path):
    override = tmp_path / "sensitive_apps.txt"
    override.write_text("(?i)MyCustomApp\n# comment ignored\n\n(?i)another-secret-tool\n")
    user_patterns = load_user_overrides(override)
    assert any("MyCustomApp" in p for p in user_patterns)
    assert any("another-secret-tool" in p for p in user_patterns)
    assert all("comment" not in p for p in user_patterns)


def test_user_override_missing_file_returns_empty(tmp_path):
    assert load_user_overrides(tmp_path / "missing.txt") == []


def test_user_override_blank_lines_skipped(tmp_path):
    p = tmp_path / "sensitive_apps.txt"
    p.write_text("\n  \n\t\n(?i)real\n   \n")
    patterns = load_user_overrides(p)
    assert patterns == ["(?i)real"]


def test_extra_patterns_match():
    """is_sensitive() respects extra_patterns kwarg."""
    snap = _snap("MyCustomApp")
    assert is_sensitive(snap) is False  # default list doesn't include this
    assert is_sensitive(snap, extra_patterns=["(?i)MyCustomApp"]) is True


def test_default_patterns_are_compilable():
    """Every default pattern must be a valid regex."""
    for pat in _DEFAULT_PATTERNS:
        re.compile(pat)


def test_malformed_user_pattern_is_skipped_not_raised():
    """Bad user regex shouldn't crash the filter."""
    snap = _snap("Code")
    # This is_sensitive call should NOT raise
    result = is_sensitive(snap, extra_patterns=["[invalid(regex"])
    # Code isn't sensitive by default, malformed pattern was skipped
    assert result is False


def test_returns_only_bool_never_raw_data():
    """The filter's contract: returns bool, never the matched pattern or value."""
    result = is_sensitive(_snap("1Password"))
    assert isinstance(result, bool)
