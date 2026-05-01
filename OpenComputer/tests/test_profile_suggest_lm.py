"""Plan 3 Task 7 — LM predicate cache-read tests."""
from __future__ import annotations

import time as _time
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_predicate_fires_on_fresh_cache_suggestion(tmp_path, monkeypatch):
    """Fresh non-dismissed suggestion in cache → predicate True."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import DailySuggestion, save_cache
    save_cache(
        suggestions=[DailySuggestion(
            kind="create",
            name="work",
            persona="coding",
            rationale="r",
            command="c",
        )],
        dismissed=[],
    )
    from opencomputer.awareness.learning_moments.predicates import (
        suggest_profile_suggest_command,
    )
    ctx = SimpleNamespace(persona_flips_in_session=0, current_profile_name="default")
    assert suggest_profile_suggest_command(ctx) is True


def test_predicate_silent_when_cache_empty(tmp_path, monkeypatch):
    """No cache + no persona flips → predicate False (no signal)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.awareness.learning_moments.predicates import (
        suggest_profile_suggest_command,
    )
    ctx = SimpleNamespace(persona_flips_in_session=0, current_profile_name="default")
    assert suggest_profile_suggest_command(ctx) is False


def test_predicate_silent_for_dismissed_suggestion(tmp_path, monkeypatch):
    """Cache has only dismissed suggestions → predicate False."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import DailySuggestion, save_cache
    save_cache(
        suggestions=[DailySuggestion(
            kind="create",
            name="work",
            persona="coding",
            rationale="r",
            command="c",
        )],
        dismissed=[{"name": "work", "until": _time.time() + 86400}],
    )
    from opencomputer.awareness.learning_moments.predicates import (
        suggest_profile_suggest_command,
    )
    ctx = SimpleNamespace(persona_flips_in_session=0, current_profile_name="default")
    assert suggest_profile_suggest_command(ctx) is False


def test_predicate_silent_on_non_default_profile(tmp_path, monkeypatch):
    """User on a named profile → predicate False (don't re-teach)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import DailySuggestion, save_cache
    save_cache(
        suggestions=[DailySuggestion(
            kind="create",
            name="work",
            persona="coding",
            rationale="r",
            command="c",
        )],
        dismissed=[],
    )
    from opencomputer.awareness.learning_moments.predicates import (
        suggest_profile_suggest_command,
    )
    ctx = SimpleNamespace(persona_flips_in_session=0, current_profile_name="work")
    assert suggest_profile_suggest_command(ctx) is False


def test_predicate_existing_trigger_still_works(tmp_path, monkeypatch):
    """Existing trigger A: ≥3 persona flips in default → True."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.awareness.learning_moments.predicates import (
        suggest_profile_suggest_command,
    )
    ctx = SimpleNamespace(persona_flips_in_session=3, current_profile_name="default")
    assert suggest_profile_suggest_command(ctx) is True


def test_predicate_existing_trigger_blocked_on_named_profile(tmp_path, monkeypatch):
    """Trigger A only fires on default profile."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.awareness.learning_moments.predicates import (
        suggest_profile_suggest_command,
    )
    ctx = SimpleNamespace(persona_flips_in_session=5, current_profile_name="work")
    assert suggest_profile_suggest_command(ctx) is False
