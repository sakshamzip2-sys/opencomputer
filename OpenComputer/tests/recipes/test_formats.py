"""Output formatters: json, table, md."""
import json

import pytest

from opencomputer.recipes.formats import format_output


SAMPLE = [
    {"title": "First", "score": 100},
    {"title": "Second", "score": 50},
]


def test_json_formats_as_pretty_json():
    out = format_output(SAMPLE, fmt="json")
    parsed = json.loads(out)
    assert parsed == SAMPLE


def test_table_includes_headers_and_rows():
    out = format_output(SAMPLE, fmt="table")
    assert "title" in out
    assert "score" in out
    assert "First" in out
    assert "Second" in out


def test_md_formats_as_table():
    out = format_output(SAMPLE, fmt="md")
    assert "|" in out
    assert "title" in out
    assert "First" in out


def test_csv_formats_with_header():
    out = format_output(SAMPLE, fmt="csv")
    assert "title,score" in out or "score,title" in out
    assert "First,100" in out or "100,First" in out


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        format_output(SAMPLE, fmt="xml")


def test_empty_list_handled_for_each_format():
    for fmt in ("json", "table", "md", "csv"):
        out = format_output([], fmt=fmt)
        assert isinstance(out, str)
