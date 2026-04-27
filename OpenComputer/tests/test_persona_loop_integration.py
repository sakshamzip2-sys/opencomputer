"""V2.C-T5 — persona auto-classifier wiring into PromptBuilder + AgentLoop.

Three layers of coverage:

1. PromptBuilder rendering: Active persona section appears iff overlay
   is non-empty, and lands between user_facts and skills.
2. Foreground-app detector: graceful "" return when osascript missing.
3. Loop helper: AgentLoop._build_persona_overlay returns a non-empty
   overlay when classifier matches a known persona.
"""
from __future__ import annotations

from unittest.mock import patch

from opencomputer.agent.prompt_builder import PromptBuilder


def test_persona_overlay_rendered_in_prompt():
    """When persona_overlay is set, base.j2 renders an 'Active persona' section."""
    pb = PromptBuilder()
    rendered = pb.build(persona_overlay="User is in coding mode. Be concise.")
    assert "coding mode" in rendered
    assert "Active persona" in rendered


def test_no_overlay_when_empty():
    pb = PromptBuilder()
    rendered = pb.build()
    assert "Active persona" not in rendered


def test_persona_overlay_appears_between_user_facts_and_skills():
    pb = PromptBuilder()
    rendered = pb.build(
        user_facts="- name: Test\n",
        persona_overlay="Persona prompt here.",
    )
    uf_idx = rendered.find("What I know about you")
    po_idx = rendered.find("Active persona")
    sk_idx = rendered.find("Skills available")
    assert uf_idx >= 0 and po_idx >= 0
    assert uf_idx < po_idx
    if sk_idx >= 0:
        assert po_idx < sk_idx


def test_detect_frontmost_app_handles_missing_osascript():
    with patch(
        "opencomputer.awareness.personas._foreground.shutil.which",
        return_value=None,
    ):
        from opencomputer.awareness.personas._foreground import detect_frontmost_app

        assert detect_frontmost_app() == ""


def test_detect_frontmost_app_handles_subprocess_failure():
    """When osascript exists but exits non-zero, return ''."""
    from subprocess import CompletedProcess

    fake_result = CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
    with (
        patch(
            "opencomputer.awareness.personas._foreground.shutil.which",
            return_value="/usr/bin/osascript",
        ),
        patch(
            "opencomputer.awareness.personas._foreground.subprocess.run",
            return_value=fake_result,
        ),
    ):
        from opencomputer.awareness.personas._foreground import detect_frontmost_app

        assert detect_frontmost_app() == ""


def test_loop_helper_returns_overlay_for_classified_persona(tmp_path, monkeypatch):
    """AgentLoop._build_persona_overlay calls classifier + registry and returns overlay text."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import AgentLoop

    # Stub-class instance: skip __init__, attach only the attributes
    # _build_persona_overlay touches (just self.db).
    class _StubDB:
        def get_messages(self, sid: str):
            return []

    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _StubDB()

    # Force the classifier to return "coding" by mocking the detector
    # to a known coding-app name, and let the classifier + registry
    # produce a non-empty overlay.
    with patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="Cursor",
    ):
        overlay = loop._build_persona_overlay("test-session")
    assert isinstance(overlay, str)
    assert "coding mode" in overlay.lower()


def test_loop_helper_degrades_to_empty_on_classifier_failure(tmp_path, monkeypatch):
    """A classifier exception must NOT break agent startup — return ''."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.loop import AgentLoop

    class _StubDB:
        def get_messages(self, sid: str):
            return []

    loop = AgentLoop.__new__(AgentLoop)
    loop.db = _StubDB()

    def _boom(*_a, **_kw):
        raise RuntimeError("classifier exploded")

    with patch(
        "opencomputer.awareness.personas.classifier.classify",
        side_effect=_boom,
    ):
        overlay = loop._build_persona_overlay("test-session")
    assert overlay == ""
