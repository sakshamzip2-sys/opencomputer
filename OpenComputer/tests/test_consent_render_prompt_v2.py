"""Hermes parity: render_prompt_message includes session verb in prompt."""
from __future__ import annotations

from opencomputer.agent.consent.gate import render_prompt_message
from plugin_sdk import CapabilityClaim, ConsentTier


def _claim() -> CapabilityClaim:
    return CapabilityClaim(
        "execute_code.run", ConsentTier.PER_ACTION, "run user code",
    )


def test_render_includes_session_no_scope():
    msg = render_prompt_message(_claim(), None)
    assert "[y/N/session/always]" in msg
    assert "execute_code.run" in msg


def test_render_includes_session_with_scope():
    msg = render_prompt_message(_claim(), "/tmp/foo.py")
    assert "[y/N/session/always]" in msg
    assert "/tmp/foo.py" in msg
