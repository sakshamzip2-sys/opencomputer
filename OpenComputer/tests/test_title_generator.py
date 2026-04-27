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
