from opencomputer.awareness.personas.classifier import (
    ClassificationContext,
    classify,
)


def test_cursor_app_classifies_coding():
    ctx = ClassificationContext(foreground_app="Cursor", time_of_day_hour=10)
    result = classify(ctx)
    assert result.persona_id == "coding"
    assert result.confidence >= 0.8


def test_zerodha_app_classifies_trading():
    ctx = ClassificationContext(foreground_app="Zerodha Kite", time_of_day_hour=10)
    result = classify(ctx)
    assert result.persona_id == "trading"


def test_animepahe_classifies_relaxed():
    ctx = ClassificationContext(foreground_app="animepahe.com", time_of_day_hour=22)
    result = classify(ctx)
    assert result.persona_id == "relaxed"


def test_files_fallback_when_app_unknown():
    ctx = ClassificationContext(
        foreground_app="UnknownApp",
        time_of_day_hour=14,
        recent_file_paths=("a.py", "b.py", "c.py", "d.py"),
    )
    result = classify(ctx)
    assert result.persona_id == "coding"


def test_late_night_default_relaxed():
    ctx = ClassificationContext(foreground_app="X", time_of_day_hour=23)
    result = classify(ctx)
    assert result.persona_id == "relaxed"


def test_no_signal_defaults_companion():
    """Path A.1 (2026-04-27) deliberately changed the no-signal default
    from 'admin' to 'companion'. The companion overlay is warm-but-honest
    about state-queries; admin was action-only and produced robotic
    answers to social openers like 'how are you?'. Companion is the
    better default for unspecified contexts."""
    ctx = ClassificationContext(foreground_app="", time_of_day_hour=14)
    result = classify(ctx)
    assert result.persona_id == "companion"


# ── Persona-uplift 2026-04-29 — Task 1: per-line state-query check ───


def test_multi_line_first_message_state_query_matches():
    """Greeting on a non-first line should still match. Real-world bug:
    user pastes ``source .venv/bin/activate`` then types ``hi`` on the
    next line — the message reaches the classifier as a single
    multi-line string and the start-anchored regex used to miss it."""
    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=("source /path/.venv/bin/activate\nhi\nhello",),
    )
    result = classify(ctx)
    assert result.persona_id == "companion"
    assert "state-query" in result.reason


def test_multi_line_greeting_on_third_line_matches():
    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=("ls -la\ncd /tmp\nhow are you?",),
    )
    result = classify(ctx)
    assert result.persona_id == "companion"


def test_single_line_state_query_still_matches_after_per_line_change():
    """Regression guard: the simple single-line case must keep working."""
    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=("hi",),
    )
    result = classify(ctx)
    assert result.persona_id == "companion"


def test_non_greeting_multi_line_does_not_match_state_query():
    """Regression guard: a multi-line message with no greeting line must
    NOT trigger the state-query rule."""
    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=("source .venv/bin/activate\npython main.py\npytest",),
    )
    result = classify(ctx)
    # Falls through to coding-app rule — NOT to companion.
    assert result.persona_id == "coding"


# ── Persona-uplift 2026-04-29 — Task 2: scan last 3 messages ─────────


def test_state_query_in_recent_messages_not_just_latest():
    """If any of the last 3 user messages is a state-query, the
    classifier should consider it. Conversation often opens with
    'hi' then continues with 'btw can you check this thing'."""
    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=(
            "hi",
            "how was your day",
            "ok cool",
        ),
    )
    result = classify(ctx)
    # 'hi' on the first message should keep us in companion territory.
    assert result.persona_id == "companion"


# ── Persona-uplift 2026-04-29 — Task 3: Hindi/Hinglish patterns ──────


def test_hindi_state_query_matches():
    ctx = ClassificationContext(
        foreground_app="iTerm2",
        time_of_day_hour=14,
        last_messages=("kaise ho",),
    )
    result = classify(ctx)
    assert result.persona_id == "companion"


def test_hinglish_state_query_matches():
    for opener in (
        "kya haal hai bhai",
        "theek ho?",
        "sab badhiya?",
        "kya chal raha hai",
    ):
        ctx = ClassificationContext(
            foreground_app="iTerm2",
            time_of_day_hour=14,
            last_messages=(opener,),
        )
        result = classify(ctx)
        assert result.persona_id == "companion", f"failed for {opener!r}"
