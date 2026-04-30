"""Tests for the sensitive-app filter — inline regex list, no cross-
plugin import dependency. Mirrors the ambient-sensors denylist in
content but not in code path.
"""
from __future__ import annotations


def test_is_app_sensitive_password_manager_matches():
    from extensions.screen_awareness.sensitive_apps import is_app_sensitive

    assert is_app_sensitive("1Password 7") is True
    assert is_app_sensitive("Bitwarden") is True


def test_is_app_sensitive_banking_matches():
    from extensions.screen_awareness.sensitive_apps import is_app_sensitive

    assert is_app_sensitive("Chase — Online Banking") is True


def test_is_app_sensitive_safe_app_returns_false():
    from extensions.screen_awareness.sensitive_apps import is_app_sensitive

    assert is_app_sensitive("Visual Studio Code") is False
    assert is_app_sensitive("iTerm2") is False


def test_is_app_sensitive_empty_string_returns_false():
    from extensions.screen_awareness.sensitive_apps import is_app_sensitive

    assert is_app_sensitive("") is False


def test_filter_returns_only_bool_no_diagnostics():
    """Contract: filter returns bool ONLY. Never returns the matched
    pattern, never logs the match. Privacy-by-construction."""
    import inspect

    from extensions.screen_awareness import sensitive_apps

    src = inspect.getsource(sensitive_apps)
    # No logging that could leak app names
    assert "matched_pattern" not in src
    assert "_log.info(" not in src
    assert "_log.debug(" not in src
