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
    suggest_auto_mode_for_long_task,
    suggest_btw_for_aside,
    suggest_checkpoint_before_rewrite,
    suggest_diff_for_silent_edits,
    suggest_history_for_lookback,
    suggest_persona_for_companion_signals,
    suggest_personality_after_friction,
    suggest_plan_for_complex_task,
    suggest_scrape_for_url,
    suggest_skill_save_after_long_session,
    suggest_undo_after_unwanted_edits,
    suggest_usage_at_token_milestone,
    suggest_voice_for_voice_user,
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
    permission_mode_str: str = "",
    recent_edit_count_this_turn: int = 0,
    checkpoint_count_session: int = 0,
    session_token_total: int = 0,
    has_openai_key: bool = False,
    turn_count: int = 0,
) -> Context:
    return Context(
        session_id="s-test",
        profile_home=profile_home or Path("/tmp/oc-test-home"),
        user_message=user_message,
        memory_md_text=memory,
        vibe_log_session_count_total=vibe_total,
        vibe_log_session_count_noncalm=vibe_noncalm,
        sessions_db_total_sessions=total_sessions,
        permission_mode_str=permission_mode_str,
        recent_edit_count_this_turn=recent_edit_count_this_turn,
        checkpoint_count_session=checkpoint_count_session,
        session_token_total=session_token_total,
        has_openai_key=has_openai_key,
        turn_count=turn_count,
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
    """v1+v2 IDs must always be present; v3 adds 13 more on top."""
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


# ── v3 predicates (2026-04-30) ───────────────────────────────────────


def test_suggest_plan_fires_on_long_multistep_request_outside_plan_mode():
    msg = (
        "Let's build the new authentication flow step by step — first "
        "the login page with email and password, then the session "
        "middleware that validates JWT tokens, then the password reset "
        "flow with email verification, and finally the rate-limiting "
        "and brute-force protection. Plan it out carefully first."
    )
    assert len(msg) >= 200, f"test fixture too short: {len(msg)}"
    ctx = _ctx(user_message=msg, permission_mode_str="DEFAULT")
    assert suggest_plan_for_complex_task(ctx) is True


def test_suggest_plan_silent_in_plan_mode():
    msg = "Let's plan this step by step across three phases of work " * 5
    ctx = _ctx(user_message=msg, permission_mode_str="PLAN")
    assert suggest_plan_for_complex_task(ctx) is False


def test_suggest_plan_silent_for_short_messages():
    ctx = _ctx(user_message="step by step please", permission_mode_str="DEFAULT")
    assert suggest_plan_for_complex_task(ctx) is False


def test_suggest_auto_fires_on_build_request_in_default_mode():
    msg = "Can you build a new feature for managing user notifications?"
    ctx = _ctx(user_message=msg, permission_mode_str="DEFAULT")
    assert suggest_auto_mode_for_long_task(ctx) is True


def test_suggest_auto_silent_when_already_in_auto():
    msg = "Build a new feature for managing user notifications"
    ctx = _ctx(user_message=msg, permission_mode_str="AUTO")
    assert suggest_auto_mode_for_long_task(ctx) is False


def test_suggest_auto_silent_in_plan_or_accept_edits():
    msg = "Implement the new authentication module from scratch"
    for mode in ("PLAN", "ACCEPT_EDITS"):
        ctx = _ctx(user_message=msg, permission_mode_str=mode)
        assert suggest_auto_mode_for_long_task(ctx) is False, mode


def test_suggest_checkpoint_fires_on_rewrite_with_no_checkpoints():
    ctx = _ctx(
        user_message="Let's refactor the entire database layer from scratch",
        checkpoint_count_session=0,
    )
    assert suggest_checkpoint_before_rewrite(ctx) is True


def test_suggest_checkpoint_silent_when_checkpoints_exist():
    ctx = _ctx(
        user_message="Let's rewrite this whole module",
        checkpoint_count_session=2,
    )
    assert suggest_checkpoint_before_rewrite(ctx) is False


def test_suggest_undo_fires_after_three_edits_with_undo_keyword():
    ctx = _ctx(
        user_message="That's wrong, please revert",
        recent_edit_count_this_turn=4,
    )
    assert suggest_undo_after_unwanted_edits(ctx) is True


def test_suggest_undo_silent_with_few_edits():
    ctx = _ctx(
        user_message="That's wrong, please revert",
        recent_edit_count_this_turn=1,
    )
    assert suggest_undo_after_unwanted_edits(ctx) is False


def test_suggest_undo_silent_without_undo_keywords():
    ctx = _ctx(
        user_message="Looks great, thanks!",
        recent_edit_count_this_turn=5,
    )
    assert suggest_undo_after_unwanted_edits(ctx) is False


def test_suggest_diff_fires_on_what_changed_after_edits():
    ctx = _ctx(
        user_message="What did you change in there?",
        recent_edit_count_this_turn=3,
    )
    assert suggest_diff_for_silent_edits(ctx) is True


def test_suggest_diff_silent_with_no_recent_edits():
    ctx = _ctx(user_message="what changed", recent_edit_count_this_turn=0)
    assert suggest_diff_for_silent_edits(ctx) is False


def test_suggest_usage_fires_above_100k_tokens():
    ctx = _ctx(session_token_total=120_000)
    assert suggest_usage_at_token_milestone(ctx) is True


def test_suggest_usage_silent_below_100k():
    ctx = _ctx(session_token_total=42_000)
    assert suggest_usage_at_token_milestone(ctx) is False


def test_suggest_history_fires_on_lookback_question():
    ctx = _ctx(user_message="Earlier we were talking about authentication")
    assert suggest_history_for_lookback(ctx) is True


def test_suggest_history_silent_on_unrelated_message():
    ctx = _ctx(user_message="Add a button to the page")
    assert suggest_history_for_lookback(ctx) is False


def test_suggest_btw_fires_on_aside_marker():
    msg = (
        "Can you finish the auth feature first. By the way, "
        "remember we still need to update the docs."
    )
    ctx = _ctx(user_message=msg)
    assert suggest_btw_for_aside(ctx) is True


def test_suggest_btw_silent_on_too_short_message():
    ctx = _ctx(user_message="btw hi")
    assert suggest_btw_for_aside(ctx) is False


def test_suggest_scrape_fires_on_bare_url():
    ctx = _ctx(user_message="check this out https://example.com/docs/page")
    assert suggest_scrape_for_url(ctx) is True


def test_suggest_scrape_silent_when_user_already_says_scrape():
    ctx = _ctx(
        user_message="please scrape this https://example.com/docs/page for me",
    )
    assert suggest_scrape_for_url(ctx) is False


def test_suggest_scrape_silent_when_no_url():
    ctx = _ctx(user_message="just a normal message with no URL inside it")
    assert suggest_scrape_for_url(ctx) is False


def test_suggest_voice_fires_with_keyword_and_openai_key():
    ctx = _ctx(
        user_message="I want to talk to you in voice mode",
        has_openai_key=True,
    )
    assert suggest_voice_for_voice_user(ctx) is True


def test_suggest_voice_silent_without_openai_key():
    ctx = _ctx(
        user_message="I want to talk to you in voice mode",
        has_openai_key=False,
    )
    assert suggest_voice_for_voice_user(ctx) is False


def test_suggest_personality_fires_after_three_noncalm_vibes():
    ctx = _ctx(vibe_total=10, vibe_noncalm=4)
    assert suggest_personality_after_friction(ctx) is True


def test_suggest_personality_silent_with_two_or_fewer_noncalm():
    ctx = _ctx(vibe_total=10, vibe_noncalm=2)
    assert suggest_personality_after_friction(ctx) is False


def test_suggest_persona_companion_fires_on_emotion_anchor():
    ctx = _ctx(user_message="rough day today, feeling overwhelmed by work")
    assert suggest_persona_for_companion_signals(ctx) is True


def test_suggest_persona_companion_silent_on_neutral_message():
    ctx = _ctx(user_message="please add a new feature to the codebase")
    assert suggest_persona_for_companion_signals(ctx) is False


def test_suggest_skill_save_fires_at_twenty_turns():
    ctx = _ctx(turn_count=20)
    assert suggest_skill_save_after_long_session(ctx) is True


def test_suggest_skill_save_silent_below_twenty_turns():
    ctx = _ctx(turn_count=15)
    assert suggest_skill_save_after_long_session(ctx) is False


# ── v3 integration tests via select_reveal ───────────────────────────


def test_v3_plan_suggestion_fires_via_select_reveal(tmp_path):
    long_msg = (
        "Let me build this step by step: first refactor the schema, "
        "then migrate data, then update the API, then rewrite the UI."
    )
    ctx = _ctx(
        profile_home=tmp_path,
        user_message=long_msg * 3,
        permission_mode_str="DEFAULT",
    )
    out = select_reveal(ctx_builder=lambda: ctx, profile_home=tmp_path)
    assert out is not None
    assert "/plan" in out


def test_v3_skill_save_fires_via_session_end(tmp_path):
    ctx = _ctx(profile_home=tmp_path, turn_count=25)
    out = select_session_end_reflection(
        ctx_builder=lambda: ctx, profile_home=tmp_path,
    )
    assert out is not None
    assert "skills new" in out


def test_v3_voice_fires_via_system_prompt_overlay(tmp_path):
    ctx = _ctx(
        profile_home=tmp_path,
        user_message="I'd love to speak to me out loud, voice mode would be cool",
        has_openai_key=True,
    )
    out = select_system_prompt_overlay(
        ctx_builder=lambda: ctx, profile_home=tmp_path,
    )
    assert out is not None
    assert "oc voice realtime" in out


def test_registry_has_v3_moments_registered():
    moments = all_moments()
    ids = [m.id for m in moments]
    assert len(moments) >= 19, f"expected ≥19 moments, got {len(moments)}"
    assert len(set(ids)) == len(ids), "duplicate ids in registry"
    v3_ids = {
        "suggest_plan_for_complex_task",
        "suggest_auto_mode_for_long_task",
        "suggest_checkpoint_before_rewrite",
        "suggest_undo_after_unwanted_edits",
        "suggest_diff_for_silent_edits",
        "suggest_usage_at_token_milestone",
        "suggest_history_for_lookback",
        "suggest_btw_for_aside",
        "suggest_scrape_for_url",
        "suggest_voice_for_voice_user",
        "suggest_personality_after_friction",
        "suggest_persona_for_companion_signals",
        "suggest_skill_save_after_long_session",
    }
    assert v3_ids.issubset(set(ids))
