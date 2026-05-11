"""TS-T6 — title-generator tests.

Mirrors the test plan in
``docs/superpowers/plans/2026-04-27-tier-s-port.md`` Task 6. Mocks the
module-level ``call_llm`` shim so no real provider plugin is touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from opencomputer.agent.title_generator import (
    auto_title_session,
    generate_title,
    maybe_auto_title,
)


def _fake_llm_response(content: str) -> MagicMock:
    """Build an OpenAI-shaped response (``.choices[0].message.content``)."""
    fake = MagicMock()
    fake.choices = [MagicMock()]
    fake.choices[0].message.content = content
    return fake


def test_generate_title_returns_clean_string() -> None:
    fake_response = _fake_llm_response("Stock Trading Strategies")
    with patch(
        "opencomputer.agent.title_generator.call_llm",
        return_value=fake_response,
    ):
        title = generate_title("hi", "let's analyze stocks")
    assert title == "Stock Trading Strategies"


def test_generate_title_strips_quotes() -> None:
    fake_response = _fake_llm_response('"Quoted Title"')
    with patch(
        "opencomputer.agent.title_generator.call_llm",
        return_value=fake_response,
    ):
        title = generate_title("u", "a")
    assert title == "Quoted Title"


def test_generate_title_caps_length() -> None:
    fake_response = _fake_llm_response("x" * 200)
    with patch(
        "opencomputer.agent.title_generator.call_llm",
        return_value=fake_response,
    ):
        title = generate_title("u", "a")
    assert title is not None
    assert len(title) <= 80


def test_generate_title_returns_none_on_exception() -> None:
    with patch(
        "opencomputer.agent.title_generator.call_llm",
        side_effect=Exception("network"),
    ):
        title = generate_title("u", "a")
    assert title is None


def test_auto_title_skips_when_already_titled() -> None:
    db = MagicMock()
    db.get_session_title.return_value = "Existing Title"
    auto_title_session(db, "sid", "u", "a")
    db.set_session_title.assert_not_called()


def test_auto_title_sets_when_no_existing() -> None:
    db = MagicMock()
    db.get_session_title.return_value = None
    fake_response = _fake_llm_response("New Title")
    with patch(
        "opencomputer.agent.title_generator.call_llm",
        return_value=fake_response,
    ):
        auto_title_session(db, "sid", "u", "a")
    db.set_session_title.assert_called_once_with("sid", "New Title")


def test_maybe_auto_title_skips_after_third_exchange() -> None:
    """Doesn't title later in long conversations.

    With >2 user messages in the history, ``maybe_auto_title`` should
    return without spawning the daemon thread. We assert the negative:
    no thread → no LLM call → no DB write. We verify the no-crash
    contract (the thread spawn path is also not exercised).
    """
    db = MagicMock()
    history = [{"role": "user"} for _ in range(5)]
    with patch(
        "opencomputer.agent.title_generator.call_llm",
    ) as patched_llm:
        maybe_auto_title(db, "sid", "u", "a", history)
    # >2 user messages → early return, no LLM call, no DB write.
    patched_llm.assert_not_called()
    db.set_session_title.assert_not_called()


# ─── Validator: reject "LLM responded as assistant" failure mode ──────


def test_generate_title_rejects_i_appreciate_response() -> None:
    """The DB has 12+ rows with titles starting "I appreciate..." — those
    are the LLM responding AS the assistant instead of generating a title.
    Validator must catch and discard, returning None so the picker falls
    back to the first-user-message preview.
    """
    fake = _fake_llm_response(
        "I appreciate you testing my behavior, but I need to be direct"
    )
    with patch(
        "opencomputer.agent.title_generator.call_llm", return_value=fake
    ):
        title = generate_title("hi", "I appreciate you testing...")
    assert title is None


def test_generate_title_rejects_im_claude_response() -> None:
    fake = _fake_llm_response("I'm Claude, Anthropic's AI assistant")
    with patch(
        "opencomputer.agent.title_generator.call_llm", return_value=fake
    ):
        title = generate_title("hi", "I'm Claude...")
    assert title is None


def test_generate_title_rejects_multi_line_response() -> None:
    """Real titles are single-line. Numbered lists / paragraphs in the
    output are signals the LLM continued the conversation."""
    fake = _fake_llm_response("I understand:\n\n1. Use the blogwatcher")
    with patch(
        "opencomputer.agent.title_generator.call_llm", return_value=fake
    ):
        title = generate_title("u", "a")
    assert title is None


def test_generate_title_rejects_untitled_marker() -> None:
    """The prompt instructs the LLM to output "Untitled" when the topic
    is unclear — we treat that as "no title, fall back to preview".
    """
    fake = _fake_llm_response("Untitled")
    with patch(
        "opencomputer.agent.title_generator.call_llm", return_value=fake
    ):
        title = generate_title("hi", "hello")
    assert title is None


def test_generate_title_strips_output_prefix() -> None:
    """LLMs sometimes prepend "Output:" because the prompt ends with it."""
    fake = _fake_llm_response("Output: Stock trading review")
    with patch(
        "opencomputer.agent.title_generator.call_llm", return_value=fake
    ):
        title = generate_title("u", "a")
    assert title == "Stock trading review"


def test_generate_title_strips_trailing_period() -> None:
    fake = _fake_llm_response("SQL query debugging.")
    with patch(
        "opencomputer.agent.title_generator.call_llm", return_value=fake
    ):
        title = generate_title("u", "a")
    assert title == "SQL query debugging"


def test_generate_title_accepts_valid_short_titles() -> None:
    """Sanity check: valid topic-style titles pass through unchanged."""
    fake = _fake_llm_response("OAuth flow walkthrough")
    with patch(
        "opencomputer.agent.title_generator.call_llm", return_value=fake
    ):
        title = generate_title("u", "a")
    assert title == "OAuth flow walkthrough"
