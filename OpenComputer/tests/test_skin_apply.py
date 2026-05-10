"""apply_skin swaps Rich theme + spinner + branding state."""
from __future__ import annotations

from rich.console import Console

from opencomputer.cli_ui.skin import apply_skin, load_skin
from opencomputer.cli_ui.skin.apply import (
    current_branding,
    current_spinner_verbs,
    current_spinner_wings,
    current_tool_emojis,
    current_tool_prefix,
)


def test_apply_changes_console_theme():
    """ares.yaml says agent_text = #FFD7B8 — must show up in pushed theme."""
    console = Console(width=80)
    spec = load_skin("ares")

    apply_skin(spec, console)

    style = console.get_style("agent_text", default=None)
    assert style is not None
    assert "ffd7b8" in str(style).lower() or "#ffd7b8" in str(style).lower()


def test_apply_is_idempotent():
    """Calling apply_skin twice with different skins ends with the second."""
    console = Console(width=80)

    apply_skin(load_skin("default"), console)
    apply_skin(load_skin("ares"), console)
    apply_skin(load_skin("default"), console)

    style = console.get_style("agent_text", default=None)
    assert style is not None
    # default's agent_text = #FFE08A
    assert "ffe08a" in str(style).lower()


def test_apply_with_invalid_color_does_not_crash():
    """Invalid hex skips that key; everything else continues to apply."""
    from opencomputer.cli_ui.skin.spec import SkinSpec

    bogus = SkinSpec(
        name="bogus",
        description="x",
        colors={"agent_text": "not-a-color"},
        spinner_thinking_verbs=("x",),
        spinner_wings=(("[", "]"),),
        agent_name="X",
        response_label="X",
        prompt_symbol="X",
        banner_logo="",
        banner_hero="",
    )

    console = Console(width=80)
    apply_skin(bogus, console)  # must not raise


def test_spinner_verbs_observable():
    apply_skin(load_skin("ares"), Console(width=80))
    verbs = current_spinner_verbs()
    assert "strategizing" in verbs


def test_spinner_wings_observable():
    apply_skin(load_skin("default"), Console(width=80))
    wings = current_spinner_wings()
    assert len(wings) >= 1
    assert all(isinstance(pair, tuple) and len(pair) == 2 for pair in wings)


def test_branding_observable():
    apply_skin(load_skin("poseidon"), Console(width=80))
    b = current_branding()
    assert b["agent_name"] == "Poseidon"
    assert "🔱" in b["prompt_symbol"]


def test_tool_emojis_observable():
    apply_skin(load_skin("default"), Console(width=80))
    emojis = current_tool_emojis()
    assert emojis.get("Read") == "📖"


def test_tool_prefix_observable():
    apply_skin(load_skin("default"), Console(width=80))
    assert current_tool_prefix() == "┊"
