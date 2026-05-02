"""Verify the synthesis prompt teaches Anthropic-spec-compliant voice and naming."""
from pathlib import Path

import jinja2


PROMPT_PATH = (
    Path(__file__).parent.parent
    / "opencomputer" / "evolution" / "prompts" / "synthesis_request.j2"
)


def _render() -> str:
    text = PROMPT_PATH.read_text()
    template = jinja2.Template(text)
    return template.render(
        proposal=type("Proposal", (), {
            "pattern_summary": "user repeatedly runs grep then opens matches in editor",
            "pattern_key": "bash:grep:success",
            "sample_arguments": ["grep -r 'foo' .", "grep -i 'bar' src/"],
        })(),
        existing_names=["read-then-edit", "grep-then-read"],
        max_chars=8000,
    )


def test_prompt_teaches_third_person_voice():
    text = _render()
    assert "third-person" in text.lower() or "3rd-person" in text.lower()
    assert "Processes" in text or "Synthesizes" in text or "Generates" in text


def test_prompt_forbids_first_and_second_person():
    text = _render()
    # Must explicitly tell the LLM not to use 1st/2nd person.
    forbidden_markers = ["never start with", "i", "you", "let me"]
    text_lower = text.lower()
    assert all(m in text_lower for m in forbidden_markers), \
        f"prompt missing forbidden-person markers: {forbidden_markers}"


def test_prompt_requires_what_and_when():
    text = _render()
    text_lower = text.lower()
    assert "what" in text_lower and "when" in text_lower
    assert "use when" in text_lower


def test_prompt_recommends_gerund_naming():
    text = _render()
    text_lower = text.lower()
    assert "gerund" in text_lower
    # Example pair must be in the prompt for the LLM to learn from.
    assert "processing-pdfs" in text or "analyzing-spreadsheets" in text


def test_prompt_forbids_time_sensitive_content():
    text = _render()
    text_lower = text.lower()
    # The phrase "time-sensitive" or a similar marker must appear.
    assert "time-sensitive" in text_lower or "after august" in text_lower


def test_prompt_description_length_cap_280():
    text = _render()
    assert "280" in text
