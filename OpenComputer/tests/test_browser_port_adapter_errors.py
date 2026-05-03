"""Tests for the 5 new typed adapter errors (Wave 4)."""

from __future__ import annotations


def test_auth_required_error_attrs():
    from extensions.browser_control._utils.errors import (
        AuthRequiredError,
        BrowserServiceError,
    )

    err = AuthRequiredError("not logged in")
    assert isinstance(err, BrowserServiceError)
    assert err.code == "auth_required"
    assert AuthRequiredError.exit_code == 77


def test_adapter_empty_result_error_attrs():
    from extensions.browser_control._utils.errors import AdapterEmptyResultError

    err = AdapterEmptyResultError("no rows")
    assert err.code == "empty_result"
    assert AdapterEmptyResultError.exit_code == 66


def test_adapter_timeout_error_attrs():
    from extensions.browser_control._utils.errors import AdapterTimeoutError

    err = AdapterTimeoutError("too slow")
    assert err.code == "timeout"
    assert AdapterTimeoutError.exit_code == 75


def test_adapter_config_error_attrs():
    from extensions.browser_control._utils.errors import AdapterConfigError

    err = AdapterConfigError("bad spec")
    assert err.code == "config"
    assert AdapterConfigError.exit_code == 78


def test_adapter_not_found_error_attrs():
    from extensions.browser_control._utils.errors import AdapterNotFoundError

    err = AdapterNotFoundError("missing")
    assert err.code == "not_found"
    assert AdapterNotFoundError.exit_code == 1


def test_instance_code_overrides_class_code():
    from extensions.browser_control._utils.errors import AuthRequiredError

    err = AuthRequiredError("x", code="custom")
    assert err.code == "custom"
