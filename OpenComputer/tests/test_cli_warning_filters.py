"""Tests for CLI startup warning filters."""
from __future__ import annotations

import warnings


def test_requests_dependency_warning_is_suppressed():
    from opencomputer import cli

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        cli._install_dependency_warning_filters()
        warnings.warn(
            "urllib3 (2.6.2) or chardet (7.4.4)/charset_normalizer "
            "(3.4.4) doesn't match a supported version!",
            Warning,
        )

    assert caught == []
