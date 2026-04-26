"""Spotlight `mdfind` subprocess wrapper tests."""
from unittest.mock import patch

from opencomputer.profile_bootstrap.spotlight import (
    SpotlightHit,
    is_spotlight_available,
    spotlight_query,
)


def test_is_spotlight_available_returns_false_without_binary():
    with patch(
        "opencomputer.profile_bootstrap.spotlight.shutil.which",
        return_value=None,
    ):
        assert is_spotlight_available() is False


def test_spotlight_query_returns_empty_when_unavailable():
    with patch(
        "opencomputer.profile_bootstrap.spotlight.is_spotlight_available",
        return_value=False,
    ):
        hits = spotlight_query("foo")
    assert hits == []


def test_spotlight_query_parses_mdfind_output():
    fake_stdout = (
        "/Users/saksham/Documents/notes.md\n"
        "/Users/saksham/Documents/draft.txt\n"
    )
    with patch(
        "opencomputer.profile_bootstrap.spotlight.is_spotlight_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.spotlight.subprocess.run",
    ) as run:
        run.return_value.stdout = fake_stdout
        run.return_value.returncode = 0
        hits = spotlight_query("budget")
    assert len(hits) == 2
    assert hits[0].path == "/Users/saksham/Documents/notes.md"


def test_spotlight_query_returns_empty_on_nonzero_exit():
    with patch(
        "opencomputer.profile_bootstrap.spotlight.is_spotlight_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.spotlight.subprocess.run",
    ) as run:
        run.return_value.stdout = ""
        run.return_value.returncode = 1
        hits = spotlight_query("anything")
    assert hits == []


def test_spotlight_query_caps_results():
    paths = "\n".join(f"/path/{i}" for i in range(500))
    with patch(
        "opencomputer.profile_bootstrap.spotlight.is_spotlight_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.spotlight.subprocess.run",
    ) as run:
        run.return_value.stdout = paths
        run.return_value.returncode = 0
        hits = spotlight_query("x", max_results=50)
    assert len(hits) == 50
