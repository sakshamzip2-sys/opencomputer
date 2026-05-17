"""``/indicator`` busy-spinner face override (best-of-three Recipe 7).

Covers the override store in ``busy_indicator``, the streaming spinner's
honouring of it, and the ``IndicatorCommand`` slash command.
"""
from __future__ import annotations

import pytest

from opencomputer.agent.slash_commands_impl.indicator_cmd import (
    IndicatorCommand,
)
from opencomputer.cli_ui.busy_indicator import (
    current_indicator_face,
    current_indicator_style,
    set_indicator_style,
)
from opencomputer.cli_ui.streaming import _skin_spinner_text
from plugin_sdk.runtime_context import RuntimeContext


@pytest.fixture(autouse=True)
def _reset_override():
    """The override is a module global — reset around every test."""
    set_indicator_style("")
    yield
    set_indicator_style("")


# ── override store ───────────────────────────────────────────────────


def test_default_is_no_override() -> None:
    assert current_indicator_style() == ""
    assert current_indicator_face() == ""


def test_set_known_style() -> None:
    assert set_indicator_style("minimal") is True
    assert current_indicator_style() == "minimal"
    assert current_indicator_face() != ""


def test_set_unknown_style_rejected() -> None:
    assert set_indicator_style("bogus") is False
    assert current_indicator_style() == ""


def test_skin_token_clears_override() -> None:
    set_indicator_style("dots")
    assert set_indicator_style("skin") is True
    assert current_indicator_style() == ""


def test_none_style_yields_empty_face() -> None:
    """'none' is an active override but renders no face — verb-only."""
    assert set_indicator_style("none") is True
    assert current_indicator_style() == "none"
    assert current_indicator_face() == ""


# ── streaming spinner honours the override ───────────────────────────


def test_spinner_uses_override_face() -> None:
    set_indicator_style("wings")
    text = _skin_spinner_text(phase="thinking")
    wings_face = current_indicator_face()
    assert wings_face and wings_face in text


def test_spinner_none_override_is_verb_only() -> None:
    set_indicator_style("none")
    text = _skin_spinner_text(phase="thinking")
    # verb-only — ends with an ellipsis, no face glyph prefix
    assert text.endswith("…")


# ── /indicator slash command ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_command_no_args_shows_current() -> None:
    result = await IndicatorCommand().execute("", RuntimeContext())
    assert "Current indicator" in result.output
    assert "minimal" in result.output


@pytest.mark.asyncio
async def test_command_sets_known_style() -> None:
    rt = RuntimeContext()
    result = await IndicatorCommand().execute("minimal", rt)
    assert "set to minimal" in result.output
    assert current_indicator_style() == "minimal"
    assert rt.custom["indicator"] == "minimal"


@pytest.mark.asyncio
async def test_command_rejects_unknown_style() -> None:
    result = await IndicatorCommand().execute("nonsense", RuntimeContext())
    assert "Unknown indicator" in result.output
    assert current_indicator_style() == ""
