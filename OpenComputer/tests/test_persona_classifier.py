from opencomputer.awareness.personas.classifier import (
    ClassificationContext, classify,
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


def test_no_signal_defaults_admin():
    ctx = ClassificationContext(foreground_app="", time_of_day_hour=14)
    result = classify(ctx)
    assert result.persona_id == "admin"
