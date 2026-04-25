"""Tests for opencomputer.release.version (date-versioned releases)."""
from __future__ import annotations

import re

import pytest

from opencomputer.release.version import (
    current_version,
    parse_date_version,
    today_version,
)


def test_current_version_is_date_format():
    assert re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}", current_version())


def test_parse_date_version_round_trip():
    assert parse_date_version("2026.4.26") == (2026, 4, 26)


def test_parse_date_version_no_zero_padding_required():
    assert parse_date_version("2026.1.5") == (2026, 1, 5)


@pytest.mark.parametrize("bad", ["1.2.3-rc1", "v2026.4.26", "2026/4/26", "2026.04", "abc"])
def test_parse_date_version_rejects_non_date(bad: str):
    with pytest.raises(ValueError):
        parse_date_version(bad)


def test_parse_date_version_rejects_invalid_calendar_dates():
    with pytest.raises(ValueError):
        parse_date_version("2026.13.1")  # no month 13
    with pytest.raises(ValueError):
        parse_date_version("2026.2.30")  # no Feb 30


def test_today_version_matches_format():
    v = today_version()
    assert re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}", v)
    parse_date_version(v)
