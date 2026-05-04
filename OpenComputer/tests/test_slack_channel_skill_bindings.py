"""Tests for Slack channel_skill_bindings config (Wave 6.A — Hermes 8fb861ea6).

Per-channel skill scoping. Adapter exposes
``skills_for_channel(chat_id) -> list[str]`` for the gateway / agent
loop to pull per-turn skill sets.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_adapter():
    p = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "slack"
        / "adapter.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_slack_channel_skill_bindings", str(p)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def SlackAdapter():  # noqa: N802
    return _load_adapter().SlackAdapter


def _make(SlackAdapter, *, bindings=None):  # noqa: N803 — SlackAdapter is a class fixture
    cfg = {"bot_token": "xoxb-fake"}
    if bindings is not None:
        cfg["channel_skill_bindings"] = bindings
    return SlackAdapter(cfg)


def test_no_bindings_returns_empty_list(SlackAdapter):  # noqa: N803
    a = _make(SlackAdapter)
    assert a.skills_for_channel("C123") == []
    assert a.skills_for_channel("any") == []


def test_bound_channel_returns_skills(SlackAdapter):  # noqa: N803
    a = _make(SlackAdapter, bindings={"C123": ["review", "tdd"]})
    assert a.skills_for_channel("C123") == ["review", "tdd"]


def test_unbound_channel_returns_empty(SlackAdapter):  # noqa: N803
    a = _make(SlackAdapter, bindings={"C123": ["review"]})
    assert a.skills_for_channel("C999") == []


def test_multiple_channels_each_have_their_own(SlackAdapter):  # noqa: N803
    a = _make(SlackAdapter, bindings={
        "C-eng": ["code-review", "tdd"],
        "C-design": ["frontend-design"],
        "D-saksham": ["companion-voice"],
    })
    assert a.skills_for_channel("C-eng") == ["code-review", "tdd"]
    assert a.skills_for_channel("C-design") == ["frontend-design"]
    assert a.skills_for_channel("D-saksham") == ["companion-voice"]


def test_lookup_returns_a_copy_not_internal_list(SlackAdapter):  # noqa: N803
    """Mutating the returned list must NOT affect the adapter's stored
    config (defence against accidental scope leaks across channels)."""
    a = _make(SlackAdapter, bindings={"C123": ["review"]})
    out = a.skills_for_channel("C123")
    out.append("malicious")
    assert a.skills_for_channel("C123") == ["review"]


def test_non_list_value_skipped(SlackAdapter):  # noqa: N803
    """Bad config (non-list value) is silently skipped — adapter still
    constructs cleanly with the well-formed entries."""
    a = _make(SlackAdapter, bindings={
        "C-good": ["skill-a"],
        "C-bad": "not-a-list",  # ignored
        "C-also-good": ["skill-b"],
    })
    assert a.skills_for_channel("C-good") == ["skill-a"]
    assert a.skills_for_channel("C-also-good") == ["skill-b"]
    assert a.skills_for_channel("C-bad") == []


def test_int_channel_id_normalised_to_string(SlackAdapter):  # noqa: N803
    """Channel ids in config are stored as strings even if author passed ints."""
    a = _make(SlackAdapter, bindings={42: ["review"]})
    assert a.skills_for_channel("42") == ["review"]
    assert a.skills_for_channel(42) == ["review"]
