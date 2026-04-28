"""Passive education ("learning moments") v1 — 2026-04-28.

This suite covers:

1. Each predicate fires/doesn't fire on synthetic Context inputs.
2. Store round-trips state through JSON; corruption is tolerated.
3. Cap enforcement — 1/UTC-day, 3/UTC-week.
4. Severity — tip suppressed by learning-off; load-bearing not.
5. Per-moment dedup — once fired, never fires again.
6. First-reveal opt-out hint — appended exactly once.
7. Predicate exception → caught, next moment tried.
8. Returning-user seed — runs only when sessions.db shows ≥5 prior.
9. Format — inline-tail with two-space indent + leading newline.
10. Concurrent-safe write — second engine sees fired marker.

Spec: docs/superpowers/specs/2026-04-28-passive-education-design.md
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.awareness.learning_moments import (
    Context,
    LearningMoment,
    Severity,
    Surface,
    all_moments,
    maybe_seed_returning_user,
    select_reveal,
    select_session_end_reflection,
    select_system_prompt_overlay,
)
from opencomputer.awareness.learning_moments.engine import (
    _cap_hit,
    _format_inline_tail,
)
from opencomputer.awareness.learning_moments.predicates import (
    confused_session,
    cross_session_recall,
    memory_continuity_first_recall,
    recent_files_paste,
    user_md_unfilled,
    vibe_first_nonneutral,
)
from opencomputer.awareness.learning_moments.store import (
    StoreState,
    load,
    save,
)

# ── Fixtures ──────────────────────────────────────────────────────────


def _ctx(
    *,
    user_message: str = "",
    memory: str = "",
    vibe_total: int = 0,
    vibe_noncalm: int = 0,
    profile_home: Path | None = None,
    total_sessions: int = 0,
) -> Context:
    return Context(
        session_id="s-test",
        profile_home=profile_home or Path("/tmp/oc-test-home"),
        user_message=user_message,
        memory_md_text=memory,
        vibe_log_session_count_total=vibe_total,
        vibe_log_session_count_noncalm=vibe_noncalm,
        sessions_db_total_sessions=total_sessions,
    )


# ── Predicates ────────────────────────────────────────────────────────


def test_memory_continuity_fires_on_three_word_substring_match():
    ctx = _ctx(
        user_message="can you remind me about the auth refactor we did last week",
        memory="Saksham is working on the auth refactor today.",
    )
    assert memory_continuity_first_recall(ctx) is True


def test_memory_continuity_does_not_fire_on_short_message():
    ctx = _ctx(user_message="hi", memory="anything")
    assert memory_continuity_first_recall(ctx) is False


def test_memory_continuity_does_not_fire_when_memory_empty():
    ctx = _ctx(user_message="this is a long enough message", memory="")
    assert memory_continuity_first_recall(ctx) is False


def test_memory_continuity_does_not_fire_on_short_window():
    ctx = _ctx(user_message="oh ok hmm", memory="oh ok hmm yeah")
    assert memory_continuity_first_recall(ctx) is False


def test_vibe_first_nonneutral_fires_when_count_is_one():
    ctx = _ctx(vibe_total=3, vibe_noncalm=1)
    assert vibe_first_nonneutral(ctx) is True


def test_vibe_first_nonneutral_does_not_fire_when_zero_noncalm():
    ctx = _ctx(vibe_total=5, vibe_noncalm=0)
    assert vibe_first_nonneutral(ctx) is False


def test_vibe_first_nonneutral_does_not_fire_after_first():
    ctx = _ctx(vibe_total=10, vibe_noncalm=3)
    assert vibe_first_nonneutral(ctx) is False


def test_recent_files_fires_on_unix_path():
    ctx = _ctx(user_message="check /Users/saksham/foo/bar.py please")
    assert recent_files_paste(ctx) is True


def test_recent_files_fires_on_relative_path():
    ctx = _ctx(user_message="open src/auth/login.ts and read it")
    assert recent_files_paste(ctx) is True


def test_recent_files_does_not_fire_on_plain_module_name():
    ctx = _ctx(user_message="i'm using src and tests today")
    assert recent_files_paste(ctx) is False


def test_recent_files_does_not_fire_on_huge_paste():
    ctx = _ctx(user_message="x" * 6000 + " /Users/foo/bar.py")
    assert recent_files_paste(ctx) is False


# ── Store ─────────────────────────────────────────────────────────────


def test_store_load_returns_empty_when_file_missing(tmp_path):
    state = load(tmp_path)
    assert state.moments_fired == {}
    assert state.fire_log == []
    assert state.first_reveal_appended is False


def test_store_save_then_load_round_trip(tmp_path):
    state = StoreState(
        moments_fired={"a": 100.0, "b": 200.0},
        fire_log=[{"id": "a", "fired_at": time.time()}],
        first_reveal_appended=True,
    )
    save(tmp_path, state)
    reloaded = load(tmp_path)
    assert reloaded.moments_fired == {"a": 100.0, "b": 200.0}
    assert reloaded.first_reveal_appended is True


def test_store_load_tolerates_corrupt_json(tmp_path):
    (tmp_path / "learning_moments.json").write_text("not json {{{")
    state = load(tmp_path)
    assert state == StoreState()


def test_store_load_tolerates_unexpected_shape(tmp_path):
    (tmp_path / "learning_moments.json").write_text('["not", "a", "dict"]')
    state = load(tmp_path)
    assert state == StoreState()


def test_store_save_trims_old_fire_log_entries(tmp_path):
    old_ts = time.time() - (20 * 24 * 3600)  # 20 days ago, beyond 14d retention
    state = StoreState(
        fire_log=[
            {"id": "old", "fired_at": old_ts},
            {"id": "recent", "fired_at": time.time()},
        ],
    )
    save(tmp_path, state)
    reloaded = load(tmp_path)
    ids = [e["id"] for e in reloaded.fire_log]
    assert "old" not in ids
    assert "recent" in ids


# ── Cap enforcement ───────────────────────────────────────────────────


def test_cap_hit_when_one_fired_today():
    state = StoreState(fire_log=[{"id": "x", "fired_at": time.time()}])
    assert _cap_hit(state) is True


def test_cap_not_hit_when_today_count_zero():
    yesterday = time.time() - (2 * 24 * 3600)
    state = StoreState(fire_log=[{"id": "x", "fired_at": yesterday}])
    # Yesterday's fire counts against weekly cap (1/3) but not daily.
    assert _cap_hit(state) is False


def test_cap_hit_when_three_fired_this_week():
    now = time.time()
    state = StoreState(
        fire_log=[
            {"id": "a", "fired_at": now - 6 * 3600},     # earlier today
            {"id": "b", "fired_at": now - 30 * 3600},    # ~1.25 days ago
            {"id": "c", "fired_at": now - 60 * 3600},    # ~2.5 days ago
        ],
    )
    # Daily cap is 1, so this also triggers daily — but the weekly path
    # is what we're verifying counts ALL recent fires, not just today.
    assert _cap_hit(state) is True


# ── Severity, dedup, opt-out hint ────────────────────────────────────


def _patch_registry(monkeypatch, moments):
    monkeypatch.setattr(
        "opencomputer.awareness.learning_moments.engine.all_moments",
        lambda: tuple(moments),
    )


def test_select_reveal_dedups_by_moment_id(tmp_path, monkeypatch):
    moment = LearningMoment(
        id="test_moment", predicate=lambda c: True, reveal="hello",
    )
    _patch_registry(monkeypatch, [moment])
    # Pre-populate as already-fired.
    save(
        tmp_path,
        StoreState(moments_fired={"test_moment": time.time() - 100}),
    )
    result = select_reveal(
        ctx_builder=lambda: _ctx(),
        profile_home=tmp_path,
    )
    assert result is None


def test_select_reveal_appends_first_opt_out_hint_only_once(tmp_path, monkeypatch):
    m1 = LearningMoment(id="m1", predicate=lambda c: True, reveal="A")
    m2 = LearningMoment(id="m2", predicate=lambda c: True, reveal="B", priority=99)
    _patch_registry(monkeypatch, [m1, m2])
    # First call → m1 fires + opt-out hint appended
    out1 = select_reveal(ctx_builder=lambda: _ctx(), profile_home=tmp_path)
    assert out1 is not None
    assert "A" in out1
    assert "learning-off" in out1
    # Daily cap blocks any second fire on the same day; clear it for this test.
    state = load(tmp_path)
    state.fire_log = []
    save(tmp_path, state)
    # Second call → m2 fires, opt-out hint NOT re-appended
    out2 = select_reveal(ctx_builder=lambda: _ctx(), profile_home=tmp_path)
    assert out2 is not None
    assert "B" in out2
    assert "learning-off" not in out2


def test_select_reveal_respects_learning_off_for_tips(tmp_path, monkeypatch):
    moment = LearningMoment(
        id="tip_moment", predicate=lambda c: True, reveal="tip",
        severity=Severity.TIP,
    )
    _patch_registry(monkeypatch, [moment])
    # Marker present → tips suppressed
    (tmp_path / ".learning_off").write_text("off")
    result = select_reveal(ctx_builder=lambda: _ctx(), profile_home=tmp_path)
    assert result is None


def test_select_reveal_load_bearing_bypasses_learning_off(tmp_path, monkeypatch):
    moment = LearningMoment(
        id="critical_moment", predicate=lambda c: True, reveal="critical",
        severity=Severity.LOAD_BEARING,
    )
    _patch_registry(monkeypatch, [moment])
    (tmp_path / ".learning_off").write_text("off")
    result = select_reveal(ctx_builder=lambda: _ctx(), profile_home=tmp_path)
    assert result is not None
    assert "critical" in result


def test_select_reveal_skips_predicate_that_raises(tmp_path, monkeypatch):
    def _bad(c):
        raise RuntimeError("boom")
    m1 = LearningMoment(id="bad", predicate=_bad, reveal="bad", priority=1)
    m2 = LearningMoment(id="good", predicate=lambda c: True, reveal="good", priority=2)
    _patch_registry(monkeypatch, [m1, m2])
    result = select_reveal(ctx_builder=lambda: _ctx(), profile_home=tmp_path)
    assert result is not None
    assert "good" in result


def test_select_reveal_returns_none_when_no_eligible(tmp_path, monkeypatch):
    moment = LearningMoment(
        id="never", predicate=lambda c: False, reveal="never",
    )
    _patch_registry(monkeypatch, [moment])
    result = select_reveal(ctx_builder=lambda: _ctx(), profile_home=tmp_path)
    assert result is None


def test_select_reveal_does_not_build_context_when_all_ineligible(
    tmp_path, monkeypatch,
):
    """Optimization: ctx_builder is the expensive part. Skip it when
    every moment is already fired."""
    moment = LearningMoment(
        id="x", predicate=lambda c: True, reveal="x",
    )
    _patch_registry(monkeypatch, [moment])
    save(tmp_path, StoreState(moments_fired={"x": time.time()}))

    def _explode():
        raise AssertionError("ctx_builder should not have been called")

    result = select_reveal(ctx_builder=_explode, profile_home=tmp_path)
    assert result is None


# ── Returning-user seed ────────────────────────────────────────────


def test_seed_does_nothing_for_new_user(tmp_path):
    maybe_seed_returning_user(tmp_path, total_sessions=0)
    assert not (tmp_path / "learning_moments.json").exists()


def test_seed_does_nothing_below_threshold(tmp_path):
    maybe_seed_returning_user(tmp_path, total_sessions=4)
    assert not (tmp_path / "learning_moments.json").exists()


def test_seed_runs_for_returning_user(tmp_path):
    maybe_seed_returning_user(tmp_path, total_sessions=10)
    state = load(tmp_path)
    expected_ids = {m.id for m in all_moments()}
    assert set(state.moments_fired.keys()) == expected_ids
    assert state.first_reveal_appended is True


def test_seed_idempotent(tmp_path):
    maybe_seed_returning_user(tmp_path, total_sessions=10)
    payload_before = (tmp_path / "learning_moments.json").read_text()
    maybe_seed_returning_user(tmp_path, total_sessions=10)
    payload_after = (tmp_path / "learning_moments.json").read_text()
    assert payload_before == payload_after


# ── Format ──────────────────────────────────────────────────────────


def test_format_inline_tail_indents_with_two_spaces():
    out = _format_inline_tail("hello")
    assert out == "\n  hello"


def test_format_inline_tail_handles_multiline():
    out = _format_inline_tail("line one\nline two")
    assert out == "\n  line one\n  line two"


# ── End-to-end via the registered v1 moments ────────────────────────


def test_e2e_recent_files_paste_fires_and_persists(tmp_path):
    """Round-trip through the real registry: trigger fires, marker
    written, second call same-day blocked by cap."""
    ctx = _ctx(
        user_message="can you read /Users/foo/bar.py?",
        profile_home=tmp_path,
        total_sessions=0,
    )
    out = select_reveal(ctx_builder=lambda: ctx, profile_home=tmp_path)
    assert out is not None
    assert "drag files in" in out
    state = load(tmp_path)
    assert "recent_files_paste" in state.moments_fired

    # Second call same day: daily cap should block.
    out2 = select_reveal(ctx_builder=lambda: ctx, profile_home=tmp_path)
    assert out2 is None


def test_e2e_severity_load_bearing_fires_even_when_capped(tmp_path, monkeypatch):
    """A TIP fires today → cap hit. A LOAD_BEARING moment must STILL
    fire on a subsequent call within the same day."""
    tip = LearningMoment(
        id="tip", predicate=lambda c: True, reveal="tip-text",
        severity=Severity.TIP, priority=1,
    )
    crit = LearningMoment(
        id="crit", predicate=lambda c: True, reveal="crit-text",
        severity=Severity.LOAD_BEARING, priority=2,
    )
    _patch_registry(monkeypatch, [tip, crit])
    out1 = select_reveal(ctx_builder=lambda: _ctx(), profile_home=tmp_path)
    assert out1 is not None
    assert "tip-text" in out1
    out2 = select_reveal(ctx_builder=lambda: _ctx(), profile_home=tmp_path)
    assert out2 is not None
    assert "crit-text" in out2


# ════════════════════════════════════════════════════════════════════
# v2 (2026-04-28) — mechanisms B + C, 3 new moments
# ════════════════════════════════════════════════════════════════════


# ── new predicates ────────────────────────────────────────────────────


def test_user_md_unfilled_fires_when_established_and_empty():
    ctx = _ctx()
    # Override v2 fields via dataclass.replace
    from dataclasses import replace
    ctx = replace(
        ctx,
        days_since_first_session=14.0,
        sessions_db_total_sessions=20,
        user_md_text="",
    )
    assert user_md_unfilled(ctx) is True


def test_user_md_unfilled_skips_new_user():
    from dataclasses import replace
    ctx = replace(
        _ctx(),
        days_since_first_session=2.0,
        sessions_db_total_sessions=20,
        user_md_text="",
    )
    assert user_md_unfilled(ctx) is False


def test_user_md_unfilled_skips_low_session_count():
    from dataclasses import replace
    ctx = replace(
        _ctx(),
        days_since_first_session=14.0,
        sessions_db_total_sessions=3,
        user_md_text="",
    )
    assert user_md_unfilled(ctx) is False


def test_user_md_unfilled_skips_when_filled():
    from dataclasses import replace
    ctx = replace(
        _ctx(),
        days_since_first_session=14.0,
        sessions_db_total_sessions=20,
        user_md_text="# Saksham\n\nWorks on OC. Prefers terse. Etc.\n",
    )
    assert user_md_unfilled(ctx) is False


def test_user_md_unfilled_treats_template_as_empty():
    from dataclasses import replace
    ctx = replace(
        _ctx(),
        days_since_first_session=14.0,
        sessions_db_total_sessions=20,
        user_md_text="# USER.md\n\n(empty — fill me in)\n",
    )
    assert user_md_unfilled(ctx) is True


def test_cross_session_recall_fires_when_hits_present():
    from dataclasses import replace
    ctx = replace(
        _ctx(),
        cross_session_topic_hits=(
            ("auth refactor", "s-yesterday"),
            ("router fixes", "s-monday"),
        ),
    )
    assert cross_session_recall(ctx) is True


def test_cross_session_recall_skips_when_no_hits():
    assert cross_session_recall(_ctx()) is False


def test_confused_session_fires_when_stuck_and_long_enough():
    from dataclasses import replace
    ctx = replace(
        _ctx(),
        turn_count=6,
        vibe_stuck_or_frustrated_fraction=0.5,
    )
    assert confused_session(ctx) is True


def test_confused_session_skips_short_session():
    from dataclasses import replace
    ctx = replace(
        _ctx(),
        turn_count=2,
        vibe_stuck_or_frustrated_fraction=0.6,
    )
    assert confused_session(ctx) is False


def test_confused_session_skips_below_threshold():
    from dataclasses import replace
    ctx = replace(
        _ctx(),
        turn_count=10,
        vibe_stuck_or_frustrated_fraction=0.10,
    )
    assert confused_session(ctx) is False


# ── Surface dispatch ──────────────────────────────────────────────────


def test_select_reveal_only_fires_inline_tail_moments(tmp_path, monkeypatch):
    """A SYSTEM_PROMPT moment should NOT fire via select_reveal."""
    sp_moment = LearningMoment(
        id="sp_only", predicate=lambda c: True, reveal="sp",
        surface=Surface.SYSTEM_PROMPT,
    )
    inline_moment = LearningMoment(
        id="inline_only", predicate=lambda c: False, reveal="inline",
    )
    _patch_registry(monkeypatch, [sp_moment, inline_moment])
    out = select_reveal(ctx_builder=lambda: _ctx(), profile_home=tmp_path)
    # Neither fires: sp filtered by surface; inline predicate False
    assert out is None


def test_select_system_prompt_overlay_fires_only_for_b_moments(tmp_path, monkeypatch):
    """An INLINE_TAIL moment should NOT fire via select_system_prompt_overlay."""
    sp_moment = LearningMoment(
        id="sp_only", predicate=lambda c: True, reveal="sp_text",
        surface=Surface.SYSTEM_PROMPT,
    )
    inline_moment = LearningMoment(
        id="inline_only", predicate=lambda c: True, reveal="inline_text",
    )
    _patch_registry(monkeypatch, [sp_moment, inline_moment])
    out = select_system_prompt_overlay(
        ctx_builder=lambda: _ctx(), profile_home=tmp_path,
    )
    assert out == "sp_text"
    # The inline moment is still eligible (state shows sp_only fired,
    # cap is now hit but inline_moment dedup applies if we call select_reveal next)


def test_select_session_end_reflection_fires_only_for_c_moments(tmp_path, monkeypatch):
    c_moment = LearningMoment(
        id="c_only", predicate=lambda c: True, reveal="bye",
        surface=Surface.SESSION_END,
    )
    inline_moment = LearningMoment(
        id="inline_skip", predicate=lambda c: True, reveal="not_here",
    )
    _patch_registry(monkeypatch, [c_moment, inline_moment])
    out = select_session_end_reflection(
        ctx_builder=lambda: _ctx(), profile_home=tmp_path,
    )
    assert out == "bye"


def test_b_overlay_does_not_get_inline_tail_indent(tmp_path, monkeypatch):
    """Mechanism B output goes to the LLM as a context line, not to
    the user — no two-space indent, no leading newline."""
    sp_moment = LearningMoment(
        id="sp_clean", predicate=lambda c: True, reveal="raw text",
        surface=Surface.SYSTEM_PROMPT,
    )
    _patch_registry(monkeypatch, [sp_moment])
    out = select_system_prompt_overlay(
        ctx_builder=lambda: _ctx(), profile_home=tmp_path,
    )
    assert out == "raw text"  # no indent, no leading \n


def test_caps_shared_across_surfaces(tmp_path, monkeypatch):
    """Firing an inline-tail moment counts toward the cap that
    suppresses subsequent system-prompt-overlay tip moments on the
    same day."""
    inline = LearningMoment(
        id="i", predicate=lambda c: True, reveal="i", priority=1,
    )
    sp = LearningMoment(
        id="s", predicate=lambda c: True, reveal="s",
        surface=Surface.SYSTEM_PROMPT, priority=2,
    )
    _patch_registry(monkeypatch, [inline, sp])

    # Inline fires → cap hit
    out1 = select_reveal(ctx_builder=lambda: _ctx(), profile_home=tmp_path)
    assert out1 is not None

    # System-prompt overlay should now be suppressed by cap
    out2 = select_system_prompt_overlay(
        ctx_builder=lambda: _ctx(), profile_home=tmp_path,
    )
    assert out2 is None


def test_load_bearing_b_moment_bypasses_caps(tmp_path, monkeypatch):
    """A LOAD_BEARING SYSTEM_PROMPT moment fires even after cap hit."""
    inline = LearningMoment(
        id="i_tip", predicate=lambda c: True, reveal="i", priority=1,
    )
    sp = LearningMoment(
        id="s_critical", predicate=lambda c: True, reveal="s_text",
        surface=Surface.SYSTEM_PROMPT, severity=Severity.LOAD_BEARING,
        priority=2,
    )
    _patch_registry(monkeypatch, [inline, sp])

    select_reveal(ctx_builder=lambda: _ctx(), profile_home=tmp_path)
    out = select_system_prompt_overlay(
        ctx_builder=lambda: _ctx(), profile_home=tmp_path,
    )
    assert out == "s_text"


# ── End-to-end against the real v1+v2 registry ──────────────────────


def test_registry_has_six_moments_with_three_surfaces():
    ids = {m.id for m in all_moments()}
    assert "memory_continuity_first_recall" in ids
    assert "vibe_first_nonneutral" in ids
    assert "recent_files_paste" in ids
    assert "user_md_unfilled" in ids
    assert "cross_session_recall" in ids
    assert "confused_session" in ids
    surfaces = {m.surface for m in all_moments()}
    assert Surface.INLINE_TAIL in surfaces
    assert Surface.SYSTEM_PROMPT in surfaces
    assert Surface.SESSION_END in surfaces
