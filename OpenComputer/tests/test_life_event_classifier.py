"""Tests for the heuristic post-turn life-event response classifier.

The ``classify_response`` tests are pure-function tests — no monkeypatch, no
profile isolation. The classifier judges whether the user's reply to a gentle
life-event hint REFUTES the inference (cancel the follow-up cron), CONFIRMS
it, or is UNCLEAR.

The conservatism bias is the load-bearing property: an ambiguous reply must
return ``"unclear"`` — never ``"refuted"`` — because a false ``"refuted"``
wrongly cancels a wanted check-in, while a missed refutation merely leaves a
gentle one-shot cron.

The ``on_stop_hook`` tests (bottom of file) DO isolate state — they
monkey-patch ``OPENCOMPUTER_HOME`` and stub the cron backend, mirroring
``tests/test_life_event_actions.py`` — because the STOP hook reads and
mutates ``life_event_state.json`` and may cancel a cron.
"""
import logging

import pytest

from opencomputer.awareness.life_events import actions, state
from opencomputer.awareness.life_events import classifier as _classifier_mod
from opencomputer.awareness.life_events.classifier import (
    classify_response,
    on_stop_hook,
)
from plugin_sdk.core import Message
from plugin_sdk.hooks import HookContext, HookEvent

# ── The three plan cases ──────────────────────────────────────────────


def test_plan_case_clear_refutation():
    assert classify_response("I'm totally fine, not stressed", "burnout") == "refuted"


def test_plan_case_rough_reply_is_not_refuted():
    # "yeah it's been rough" must NOT be classified as a refutation.
    assert classify_response("yeah it's been rough", "burnout") in {
        "confirmed",
        "unclear",
    }


def test_plan_case_empty_reply_is_unclear():
    assert classify_response("", "burnout") == "unclear"


# ── Clear refutations — one assertion per refutation phrase ────────────


def test_refutation_im_fine():
    assert classify_response("I'm fine, really", "burnout") == "refuted"


def test_refutation_im_ok():
    assert classify_response("honestly I'm ok", "exam_prep") == "refuted"


def test_refutation_not_stressed():
    assert classify_response("I'm not stressed at all", "burnout") == "refuted"


def test_refutation_nothings_wrong():
    assert classify_response("nothing's wrong, why do you ask", "health_event") == "refuted"


def test_refutation_all_good():
    assert classify_response("all good here", "travel") == "refuted"


def test_refutation_doing_well():
    assert classify_response("I'm doing well, thanks", "job_change") == "refuted"


def test_refutation_youre_wrong():
    assert classify_response("you're wrong about that", "relationship_shift") == "refuted"


def test_refutation_no_reason_to_worry():
    assert classify_response("no reason to worry", "burnout") == "refuted"


def test_refutation_is_case_insensitive():
    assert classify_response("I'M TOTALLY FINE", "burnout") == "refuted"


# ── The negation pitfall — distressed replies that CONTAIN a positive
#    refutation phrase must NOT be classified "refuted" ─────────────────


def test_negation_pitfall_not_doing_well():
    # "doing well" is a refutation phrase, but this reply is clearly
    # distressed. A naive `if phrase in text` would mis-flag it.
    assert classify_response("honestly i'm not doing well at all", "burnout") != "refuted"


def test_negation_pitfall_not_all_good():
    assert classify_response("things are not all good right now", "burnout") != "refuted"


def test_negation_pitfall_dont_feel_fine():
    assert classify_response("i don't feel fine honestly", "burnout") != "refuted"


def test_negation_pitfall_never_been_okay():
    assert classify_response("i've never been okay with this", "relationship_shift") != "refuted"


def test_negation_pitfall_isnt_all_good():
    assert classify_response("it isn't all good", "burnout") != "refuted"


# ── The negator-window pitfall — a negator separated from the positive
#    phrase by an intervening adverb / filler still inverts it ──────────


def test_negation_window_not_really_doing_well():
    # "really" sits between "not" and "doing well"; the negator must
    # still flip the positive phrase.
    assert classify_response("i'm not really doing well", "burnout") != "refuted"


def test_negation_window_not_currently_doing_well():
    assert classify_response("not currently doing well", "burnout") != "refuted"


def test_negation_window_not_like_doing_well():
    # Filler-word case: "not, like, doing well".
    assert classify_response("i'm not, like, doing well", "burnout") != "refuted"


# ── Clear confirmations ───────────────────────────────────────────────


def test_confirmation_yeah_struggling():
    assert classify_response("yeah, I've been struggling lately", "burnout") == "confirmed"


def test_confirmation_been_hard():
    assert classify_response("it's been hard, honestly", "burnout") == "confirmed"


def test_confirmation_overwhelmed():
    assert classify_response("I feel really overwhelmed", "exam_prep") == "confirmed"


def test_confirmation_burnt_out():
    assert classify_response("I'm pretty burnt out", "burnout") == "confirmed"


# ── Unclear — empty / whitespace / unrelated / ambiguous ──────────────


def test_whitespace_only_reply_is_unclear():
    assert classify_response("   \n\t  ", "burnout") == "unclear"


def test_unrelated_reply_is_unclear():
    assert classify_response("what's the weather", "burnout") == "unclear"


def test_off_topic_reply_is_unclear():
    assert classify_response("can you open the file in src/main.py", "exam_prep") == "unclear"


def test_ambiguous_reply_is_unclear_not_refuted():
    # No refutation phrase, no confirmation phrase → must be "unclear",
    # the conservative default — NOT "refuted".
    assert classify_response("hmm, maybe", "burnout") == "unclear"


def test_return_value_is_always_one_of_three():
    for text in ["I'm fine", "it's been rough", "", "what's the weather", "maybe"]:
        assert classify_response(text, "burnout") in {"refuted", "confirmed", "unclear"}


# ── Deferred Task-6 review item — confirmation vetoes refutation ───────
#
# A reply that contains BOTH a refutation phrase and a confirmation phrase
# is genuine ambiguity; conservatism says it must NOT cancel the check-in.
# The Task-6 reviewer asked for this explicit assertion to ride along with
# the STOP-hook work, since the veto path is what keeps a half-refutation
# from wrongly cancelling a wanted cron.


def test_confirmation_vetoes_refutation():
    assert classify_response("I'm fine but it's been rough", "burnout") == "confirmed"


# ══════════════════════════════════════════════════════════════════════
#  on_stop_hook — the post-turn orchestrator (Task 7)
# ══════════════════════════════════════════════════════════════════════
#
# on_stop_hook fires at the end of EVERY turn. It must judge the user's
# reply that comes AFTER the surfacing turn — not the surfacing turn's own
# message (which the user typed BEFORE seeing any hint-influenced reply).
# The turn-index mechanism: state records ``surfaced_turn``; on_stop_hook
# only classifies a verdict-pending pattern when the current turn index is
# STRICTLY LATER than that recorded surfaced_turn.


def _seed_pending(pattern_id: str, *, cron_id: str, surfaced_turn: int) -> None:
    """Write a verdict-pending state entry with a recorded surfaced_turn."""
    state.save_state(
        {
            pattern_id: {
                "firing_ts": 100.0,
                "cron_id": cron_id,
                "surfaced": True,
                "verdict_pending": True,
                "surfaced_turn": surfaced_turn,
            }
        }
    )


def _stop_ctx(*, turn_index: int, last_user_text: str) -> HookContext:
    """A STOP HookContext whose message history ends with a user reply.

    Mirrors the real loop: ``messages`` is the conversation so far and the
    final user message is the reply on_stop_hook must classify.
    """
    return HookContext(
        event=HookEvent.STOP,
        session_id="sess-1",
        messages=[
            Message(role="user", content="(the surfacing-turn message)"),
            Message(role="assistant", content="(hint-influenced reply)"),
            Message(role="user", content=last_user_text),
        ],
        turn_index=turn_index,
    )


@pytest.mark.asyncio
async def test_on_stop_hook_refuting_reply_cancels_cron(tmp_path, monkeypatch):
    """A refuting reply AFTER the surfacing turn cancels the follow-up cron."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed_pending("burnout", cron_id="cron-r", surfaced_turn=2)

    removed: list[str] = []
    monkeypatch.setattr(
        actions, "remove_job", lambda job_id: removed.append(job_id) or True
    )

    # Current turn (3) is AFTER the surfaced turn (2): the reply is judged.
    await on_stop_hook(_stop_ctx(turn_index=3, last_user_text="I'm totally fine, not stressed"))

    assert removed == ["cron-r"], "the recorded cron_id must be deleted"
    assert "burnout" not in state.load_state(), "the state entry must be cleared"


@pytest.mark.asyncio
async def test_on_stop_hook_confirming_reply_keeps_cron(tmp_path, monkeypatch):
    """A confirming reply keeps the cron + entry, only clears verdict_pending."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed_pending("burnout", cron_id="cron-k", surfaced_turn=2)

    removed: list[str] = []
    monkeypatch.setattr(
        actions, "remove_job", lambda job_id: removed.append(job_id) or True
    )

    await on_stop_hook(_stop_ctx(turn_index=3, last_user_text="yeah I'm really burnt out"))

    assert removed == [], "a confirming reply must NOT delete the cron"
    entry = state.load_state().get("burnout")
    assert entry is not None, "the entry must survive a confirming reply"
    assert entry["cron_id"] == "cron-k", "the cron_id must be retained"
    assert entry["verdict_pending"] is False, "verdict_pending must be cleared"


@pytest.mark.asyncio
async def test_on_stop_hook_unclear_reply_keeps_cron(tmp_path, monkeypatch):
    """An unclear reply keeps the cron + entry, clears verdict_pending."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed_pending("travel", cron_id="cron-u", surfaced_turn=1)

    removed: list[str] = []
    monkeypatch.setattr(
        actions, "remove_job", lambda job_id: removed.append(job_id) or True
    )

    await on_stop_hook(_stop_ctx(turn_index=2, last_user_text="what's the weather"))

    assert removed == [], "an unclear reply must NOT delete the cron"
    entry = state.load_state().get("travel")
    assert entry is not None and entry["cron_id"] == "cron-u"
    assert entry["verdict_pending"] is False


@pytest.mark.asyncio
async def test_on_stop_hook_skips_the_surfacing_turn(tmp_path, monkeypatch):
    """On the surfacing turn's own STOP, on_stop_hook does NOTHING.

    The user's turn-N message predates the hint — classifying it would clear
    verdict_pending before the actual reply-to-the-hint (turn N+1) is judged.
    Even though the seeded message REFUTES, the cron must survive and the
    pattern must remain verdict-pending.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed_pending("burnout", cron_id="cron-same", surfaced_turn=4)

    removed: list[str] = []
    monkeypatch.setattr(
        actions, "remove_job", lambda job_id: removed.append(job_id) or True
    )

    classify_calls: list[str] = []
    real_classify = _classifier_mod.classify_response

    def _spy(text, pattern_id):
        classify_calls.append(text)
        return real_classify(text, pattern_id)

    monkeypatch.setattr(_classifier_mod, "classify_response", _spy)

    # current turn == surfaced turn → the surfacing turn itself.
    await on_stop_hook(_stop_ctx(turn_index=4, last_user_text="I'm totally fine, not stressed"))

    assert classify_calls == [], "the surfacing turn must NOT be classified"
    assert removed == [], "the cron must NOT be cancelled on the surfacing turn"
    entry = state.load_state().get("burnout")
    assert entry is not None, "the entry must survive the surfacing turn"
    assert entry["verdict_pending"] is True, "still verdict-pending after surfacing turn"


@pytest.mark.asyncio
async def test_on_stop_hook_empty_state_is_noop(tmp_path, monkeypatch):
    """No verdict-pending patterns → a clean no-op (the common case)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    removed: list[str] = []
    monkeypatch.setattr(
        actions, "remove_job", lambda job_id: removed.append(job_id) or True
    )

    await on_stop_hook(_stop_ctx(turn_index=5, last_user_text="I'm fine"))

    assert removed == []
    assert state.load_state() == {}


@pytest.mark.asyncio
async def test_on_stop_hook_is_fail_open_when_classifier_raises(
    tmp_path, monkeypatch, caplog
):
    """A classifier error must NOT wedge the turn — log WARNING, LEAVE the cron.

    On error the conservative outcome is to keep the cron (never mis-cancel),
    so the entry and its cron_id must both survive.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed_pending("burnout", cron_id="cron-boom", surfaced_turn=1)

    removed: list[str] = []
    monkeypatch.setattr(
        actions, "remove_job", lambda job_id: removed.append(job_id) or True
    )

    def boom(*_args, **_kwargs):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(_classifier_mod, "classify_response", boom)

    with caplog.at_level(logging.WARNING):
        # Must not raise.
        await on_stop_hook(_stop_ctx(turn_index=2, last_user_text="anything"))

    assert removed == [], "on error the cron must be LEFT (never mis-cancelled)"
    assert "burnout" in state.load_state(), "the entry must survive a classifier error"
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "a WARNING must be logged on the fail-open path"
    )


@pytest.mark.asyncio
async def test_on_stop_hook_missing_surfaced_turn_classifies(tmp_path, monkeypatch):
    """A legacy entry with no ``surfaced_turn`` is still judged (not skipped).

    Pre-Task-7 entries lack ``surfaced_turn``. Treating an absent value as
    turn 0 means the current turn is always strictly later, so the reply is
    classified rather than silently ignored forever.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    state.save_state(
        {
            "burnout": {
                "firing_ts": 100.0,
                "cron_id": "cron-legacy",
                "surfaced": True,
                "verdict_pending": True,
                # no surfaced_turn key
            }
        }
    )

    removed: list[str] = []
    monkeypatch.setattr(
        actions, "remove_job", lambda job_id: removed.append(job_id) or True
    )

    await on_stop_hook(_stop_ctx(turn_index=1, last_user_text="I'm totally fine, not stressed"))

    assert removed == ["cron-legacy"], "a legacy entry's reply must still be judged"
    assert "burnout" not in state.load_state()


@pytest.mark.asyncio
async def test_on_stop_hook_no_message_history_is_unclear_noop(tmp_path, monkeypatch):
    """A STOP context with no message history → an empty reply → unclear.

    An empty reply classifies as ``"unclear"`` (the conservative default),
    so the cron is kept and only verdict_pending is cleared — never a
    spurious cancel.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed_pending("burnout", cron_id="cron-nohist", surfaced_turn=1)

    removed: list[str] = []
    monkeypatch.setattr(
        actions, "remove_job", lambda job_id: removed.append(job_id) or True
    )

    ctx = HookContext(event=HookEvent.STOP, session_id="s", turn_index=2)
    await on_stop_hook(ctx)  # messages is None

    assert removed == [], "no history → unclear → cron kept (never cancelled)"
    entry = state.load_state().get("burnout")
    assert entry is not None and entry["verdict_pending"] is False


# ── tool-using turn — tool_result trailing message must not blind the
#    classifier to the user's real text reply ──────────────────────────


class _ToolResultMessage:
    """A tool_result message stand-in: ``role == "user"`` with LIST content.

    On any turn where the model called a tool, the conversation appends a
    ``tool_result`` message — Anthropic's wire shape gives it ``role ==
    "user"`` and a *list* of result blocks as content, and it lands AFTER
    the user's text reply. ``plugin_sdk.core.Message`` is frozen with
    ``content: str`` and cannot model that, so this minimal stand-in does
    (``on_stop_hook`` reads ``role`` / ``content`` duck-typed via
    ``getattr``).
    """

    def __init__(self, content: list) -> None:
        self.role = "user"
        self.content = content


@pytest.mark.asyncio
async def test_on_stop_hook_classifies_real_reply_past_trailing_tool_result(
    tmp_path, monkeypatch
):
    """A tool_result message (``role == "user"``, LIST content) trailing the
    conversation must NOT blind the classifier.

    On a tool-using turn the conversation ends with a tool_result message —
    ``role == "user"`` but list content — placed AFTER the user's real text
    reply. ``_last_user_text`` must skip the tool_result and classify the
    user's actual typed reply, so a refuting reply still cancels the cron
    even on a turn where the model used a tool.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed_pending("burnout", cron_id="cron-tool", surfaced_turn=2)

    removed: list[str] = []
    monkeypatch.setattr(
        actions, "remove_job", lambda job_id: removed.append(job_id) or True
    )

    # Conversation: user's REAL refuting reply, then the model's
    # tool-call turn, then the trailing tool_result (role="user", list).
    ctx = HookContext(
        event=HookEvent.STOP,
        session_id="sess-tool",
        messages=[
            Message(role="user", content="(the surfacing-turn message)"),
            Message(role="assistant", content="(hint-influenced reply)"),
            Message(role="user", content="I'm totally fine, not stressed"),
            Message(role="assistant", content="(model calls a tool)"),
            _ToolResultMessage(
                content=[{"type": "tool_result", "content": "tool output"}]
            ),
        ],
        turn_index=3,
    )

    await on_stop_hook(ctx)

    # The real text reply ("I'm totally fine, not stressed") was classified,
    # not the trailing tool_result — so the refutation cancelled the cron.
    assert removed == ["cron-tool"], (
        "the user's real refuting reply must be classified past the "
        "trailing tool_result, cancelling the cron"
    )
    assert "burnout" not in state.load_state(), "the state entry must be cleared"


# ── Registration + fire-site wiring ───────────────────────────────────


@pytest.fixture
def _isolate_stop_registration():
    """Make the STOP-hook registration tests hermetic.

    ``register_life_event_stop_hook`` is idempotent via the module-global
    ``classifier._STOP_HOOK_REGISTERED`` flag, and registers ``on_stop_hook``
    on the process-wide singleton hook ``engine``. In the full suite an
    earlier ``AgentLoop`` construction flips that flag ``True`` and registers
    the handler; a later test then calls ``engine.unregister_all`` and strips
    the STOP bucket — but the flag stays ``True``. The two register-tests
    below would then no-op (flag ``True``) and never re-register the handler,
    so their assertions fail. Production is unaffected — the real engine is a
    never-cleared singleton, so flag and engine never desync.

    This fixture saves the flag and the ``engine`` STOP bucket, resets both to
    a known state (flag ``False``, STOP bucket empty), yields, then restores
    exactly what it found — so each register-test starts hermetic and leaves
    no side effect on other tests.
    """
    from opencomputer.hooks.engine import engine

    saved_flag = _classifier_mod._STOP_HOOK_REGISTERED
    # engine._hooks is the raw defaultdict — copy the STOP bucket's list.
    saved_bucket = list(engine._hooks[HookEvent.STOP])

    _classifier_mod._STOP_HOOK_REGISTERED = False
    engine.unregister_all(HookEvent.STOP)
    try:
        yield
    finally:
        _classifier_mod._STOP_HOOK_REGISTERED = saved_flag
        engine._hooks[HookEvent.STOP] = saved_bucket


def test_register_life_event_stop_hook_registers_against_stop(
    _isolate_stop_registration,
):
    """``register_life_event_stop_hook`` registers ``on_stop_hook`` for STOP."""
    from opencomputer.awareness.life_events.classifier import (
        register_life_event_stop_hook,
    )
    from opencomputer.hooks.engine import engine

    register_life_event_stop_hook()  # idempotent — safe even if already done

    stop_specs = engine._ordered_specs(HookEvent.STOP)
    assert any(spec.handler is on_stop_hook for spec in stop_specs), (
        "on_stop_hook must be registered against HookEvent.STOP"
    )


def test_register_life_event_stop_hook_is_idempotent(_isolate_stop_registration):
    """Repeated registration calls register the handler exactly once.

    AgentLoop is constructed per session; the registration call runs once
    per construction. The process-wide guard must prevent N duplicate
    handlers (which would classify the reply — and possibly cancel — N
    times per turn).
    """
    from opencomputer.awareness.life_events.classifier import (
        register_life_event_stop_hook,
    )
    from opencomputer.hooks.engine import engine

    register_life_event_stop_hook()
    register_life_event_stop_hook()
    register_life_event_stop_hook()

    stop_specs = engine._ordered_specs(HookEvent.STOP)
    matches = [spec for spec in stop_specs if spec.handler is on_stop_hook]
    assert len(matches) == 1, "on_stop_hook must be registered exactly once"


def test_loop_fires_stop_hook_emit_site_present():
    """Source-level guard: the AgentLoop must fire HookEvent.STOP.

    on_stop_hook only runs because the loop fires STOP at the END_TURN
    return path, threading ``turn_index`` so the surfacing-turn skip works.
    A refactor that drops the emit site would silently disable the entire
    life-event self-correction feature — this grep catches that.
    """
    import pathlib

    loop_src = (
        pathlib.Path(__file__).parent.parent
        / "opencomputer"
        / "agent"
        / "loop.py"
    ).read_text(encoding="utf-8")
    assert "_HookEventStop.STOP" in loop_src, "loop must fire HookEvent.STOP"
    assert "turn_index=turn_index" in loop_src, (
        "the STOP fire must thread turn_index for the surfacing-turn skip"
    )
